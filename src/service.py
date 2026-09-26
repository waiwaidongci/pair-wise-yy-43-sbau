from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_severity, require_moment, require_number,
                     require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES, TITLE,
                    VIEW_ROLES, WINDOW_ENTITY, WINDOW_ROLES, WINDOW_STATES,
                    completion_blockers, escalation_required, evaluate_window,
                    find_window_conflicts, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_sea_state, validate_transition,
                    validate_window_item_state)


def _window_ref(window: Dict[str, Any]) -> Dict[str, Any]:
    return {"window_id": window["id"], "item_id": window["item_id"],
            "ship_name": window["ship_name"], "depart_at": window["depart_at"],
            "return_at": window["return_at"]}


def _conflict_error(conflicts: list) -> ConflictError:
    return ConflictError(
        f"船期与{len(conflicts)}个已排期航窗口重叠",
        details={"conflicting_windows": [_window_ref(w) for w in conflicts]})


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        item = self.enrich(self.repository.get_item(item_id))
        item["recovery_progress"] = self.repository.recovery_progress(item_id)
        return item

    @staticmethod
    def _window_plan(payload: Dict[str, Any]) -> tuple:
        ship_name = require_text(payload.get("ship_name"), "ship_name", 100)
        depart_at = require_moment(payload.get("depart_at"), "depart_at")
        return_at = require_moment(payload.get("return_at"), "return_at")
        if not depart_at < return_at:
            raise ValidationError("return_at必须晚于depart_at")
        estimated = require_number(payload.get("estimated_recovery"), "estimated_recovery")
        sea_state = validate_sea_state(payload.get("sea_state"))
        return ship_name, depart_at, return_at, estimated, sea_state

    def create_window(self, item_id: int, payload: Dict[str, Any], actor: str,
                      role: str) -> Dict[str, Any]:
        ensure_role(role, WINDOW_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_window_item_state(item["status"])
        ship_name, depart_at, return_at, estimated, sea_state = self._window_plan(payload)
        conflicts = find_window_conflicts(
            self.repository.list_windows(status="scheduled", ship_name=ship_name),
            ship_name, depart_at, return_at)
        if conflicts:
            raise _conflict_error(conflicts)
        reasons = evaluate_window(sea_state, estimated, item["quantity"])
        status = "pending_reschedule" if reasons else "scheduled"
        window = self.repository.create_window(
            item_id, ship_name, depart_at, return_at, estimated, sea_state,
            status, reasons, actor)
        self.repository.append_audit("window_create", WINDOW_ENTITY, window["id"],
                                     actor, {"item_id": item_id,
                                             "ship_name": ship_name,
                                             "status": status, "reasons": reasons})
        return window

    def confirm_window(self, window_id: int, payload: Dict[str, Any], actor: str,
                       role: str) -> Dict[str, Any]:
        ensure_role(role, WINDOW_ROLES)
        actor = require_text(actor, "actor", 100)
        window = self.repository.get_window(window_id)
        if window["status"] != "pending_reschedule":
            raise ConflictError("仅留待重排的航窗口可重新确认")
        item = self.repository.get_item(window["item_id"])
        validate_window_item_state(item["status"])
        plan = dict(payload, ship_name=window["ship_name"])
        ship_name, depart_at, return_at, estimated, sea_state = self._window_plan(plan)
        conflicts = find_window_conflicts(
            self.repository.list_windows(status="scheduled", ship_name=ship_name),
            ship_name, depart_at, return_at, exclude_id=window_id)
        if conflicts:
            raise _conflict_error(conflicts)
        reasons = evaluate_window(sea_state, estimated, item["quantity"])
        status = "pending_reschedule" if reasons else "scheduled"
        updated = self.repository.confirm_window(
            window_id, depart_at, return_at, estimated, sea_state, status, reasons)
        self.repository.append_audit("window_confirm", WINDOW_ENTITY, window_id,
                                     actor, {"item_id": window["item_id"],
                                             "status": status, "reasons": reasons})
        return updated

    def register_window_return(self, window_id: int, payload: Dict[str, Any],
                               actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, WINDOW_ROLES)
        actor = require_text(actor, "actor", 100)
        actual = require_number(payload.get("actual_recovery"), "actual_recovery")
        separated = require_number(payload.get("separated_oil"), "separated_oil")
        if separated > actual:
            raise ValidationError("separated_oil不能大于actual_recovery")
        window = self.repository.register_window_return(window_id, actual,
                                                        separated, actor)
        self.repository.append_audit("window_return", WINDOW_ENTITY, window_id,
                                     actor, {"item_id": window["item_id"],
                                             "actual_recovery": actual,
                                             "separated_oil": separated})
        return window

    def list_windows(self, role: str, item_id: Optional[int] = None,
                     status: Optional[str] = None,
                     ship_name: Optional[str] = None) -> list:
        self._view(role)
        if status is not None and status not in WINDOW_STATES:
            raise ValidationError("status不在允许范围内")
        if item_id is not None:
            self.repository.get_item(item_id)
        return self.repository.list_windows(item_id=item_id, status=status,
                                            ship_name=ship_name)

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
