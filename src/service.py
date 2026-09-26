from __future__ import annotations

from typing import Any, Dict, List, Optional

from .domain import (ConflictError, ensure_role, normalize_severity,
                     require_int, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES, TITLE,
                    VIEW_ROLES, completion_blockers, escalation_required,
                    priority_score, response_deadline_hours, role_for_transition,
                    validate_transition)
from .voyage_rules import (CONFIRMED, COMPLETED, MAX_SEA_STATE, MIN_RECOVERY_RATIO,
                           PENDING, RECORD_KIND, RETURN_ROLES, SCHEDULE_ROLES,
                           SEA_STATE_MAX, SEA_STATE_MIN, WINDOW_ENTITY, WINDOW_STATES,
                           ensure_status, find_conflicts, judge_window, parse_dt,
                           validate_window_times)


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
            from .domain import ConflictError
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
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ---- 出航窗口台账 ----

    @staticmethod
    def _window_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        vessel = require_text(payload.get("vessel"), "vessel", 100)
        departure = parse_dt(payload.get("departure_at"), "departure_at")
        returned = parse_dt(payload.get("return_at"), "return_at")
        validate_window_times(departure, returned)
        estimated_qty = require_number(payload.get("estimated_qty"), "estimated_qty")
        sea_state = require_int(payload.get("sea_state"), "sea_state",
                                SEA_STATE_MIN, SEA_STATE_MAX)
        return {"vessel": vessel, "departure": departure, "returned": returned,
                "estimated_qty": estimated_qty, "sea_state": sea_state}

    @staticmethod
    def _expected_version(payload: Dict[str, Any]) -> Optional[int]:
        expected = payload.get("expected_version")
        if expected is None:
            return None
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
            from .domain import ValidationError
            raise ValidationError("expected_version必须是正整数")
        return expected

    def _conflict_if_overlap(self, vessel: str, departure, returned,
                             exclude_id: int = 0) -> None:
        active = self.repository.list_voyage_windows(vessel=vessel)
        conflicts = find_conflicts(active, departure, returned, exclude_id)
        if conflicts:
            raise ConflictError(
                f"回收船{vessel}船期与已有窗口重叠，请返回冲突安排",
                {"conflicts": [self._window_brief(w) for w in conflicts]})

    @staticmethod
    def _window_brief(window: Dict[str, Any]) -> Dict[str, Any]:
        return {"id": window["id"], "item_id": window["item_id"],
                "vessel": window["vessel"], "status": window["status"],
                "departure_at": window["departure_at"],
                "return_at": window["return_at"]}

    def schedule_voyage_window(self, item_id: int, payload: Dict[str, Any],
                               actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, SCHEDULE_ROLES)
        actor = require_text(actor, "actor", 100)
        data = self._window_payload(payload)
        item = self.repository.get_item(item_id)
        # 海况超四级或预计回收量不足事件油量一成：留待重排
        status, reasons = judge_window(
            data["sea_state"], data["estimated_qty"], item["quantity"])
        self._conflict_if_overlap(data["vessel"], data["departure"], data["returned"])
        window = self.repository.create_voyage_window(
            item_id, data["vessel"], data["departure"].isoformat(),
            data["returned"].isoformat(), data["estimated_qty"], data["sea_state"],
            status, "；".join(reasons), actor)
        self.repository.append_audit("voyage_schedule", WINDOW_ENTITY, window["id"],
                                     actor, {"item_id": item_id, "vessel": data["vessel"],
                                             "status": status, "reasons": reasons})
        return self._enrich_window(window)

    def reconfirm_voyage_window(self, window_id: int, payload: Dict[str, Any],
                                actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, SCHEDULE_ROLES)
        actor = require_text(actor, "actor", 100)
        window = self.repository.get_voyage_window(window_id)
        ensure_status(window, [PENDING], "复测确认")
        data = self._window_payload(payload)
        item = self.repository.get_item(window["item_id"])
        status, reasons = judge_window(
            data["sea_state"], data["estimated_qty"], item["quantity"])
        values = {
            "vessel": data["vessel"],
            "departure_at": data["departure"].isoformat(),
            "return_at": data["returned"].isoformat(),
            "estimated_qty": data["estimated_qty"],
            "sea_state": data["sea_state"],
            "status": status,
            "hold_reason": "；".join(reasons),
        }
        if status == CONFIRMED:
            # 复测达标才重新检查船期并确认
            self._conflict_if_overlap(
                data["vessel"], data["departure"], data["returned"], window_id)
        updated = self.repository.update_voyage_window(
            window_id, values, self._expected_version(payload), actor)
        self.repository.append_audit("voyage_reconfirm", WINDOW_ENTITY, window_id,
                                     actor, {"status": status, "reasons": reasons})
        return self._enrich_window(updated)

    def register_voyage_return(self, window_id: int, payload: Dict[str, Any],
                               actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RETURN_ROLES)
        actor = require_text(actor, "actor", 100)
        window = self.repository.get_voyage_window(window_id)
        ensure_status(window, [CONFIRMED], "回港登记")
        recovered_qty = require_number(payload.get("recovered_qty"), "recovered_qty")
        separated_oil_qty = require_number(
            payload.get("separated_oil_qty"), "separated_oil_qty")
        separated_water_qty = require_number(
            payload.get("separated_water_qty", 0.0), "separated_water_qty")
        separation_result = require_text(
            payload.get("separation_result"), "separation_result", 1000)
        if separated_oil_qty + separated_water_qty > recovered_qty + 1e-6:
            from .domain import ValidationError
            raise ValidationError("油水分离结果合计不能超过回港回收量")
        values = {"status": COMPLETED, "recovered_qty": recovered_qty,
                  "separated_oil_qty": separated_oil_qty,
                  "separated_water_qty": separated_water_qty,
                  "separation_result": separation_result}
        updated = self.repository.update_voyage_window(
            window_id, values, self._expected_version(payload), actor)
        # 回港登记后自动补一条已关闭的回收记录，计入事件进度
        self.repository.add_record(
            window["item_id"], RECORD_KIND,
            (f"回收船{window['vessel']}回港：回收量{recovered_qty}，"
             f"分离纯油{separated_oil_qty}，分离水{separated_water_qty}，"
             f"油水分离结果：{separation_result}"),
            "closed", f"VOYAGE-{window_id}", actor)
        self.repository.append_audit("voyage_return", WINDOW_ENTITY, window_id, actor, {
            "item_id": window["item_id"], "recovered_qty": recovered_qty,
            "separated_oil_qty": separated_oil_qty,
            "separated_water_qty": separated_water_qty})
        return self._enrich_window(updated)

    def list_voyage_windows(self, role: str, status: Optional[str] = None,
                            item_id: Optional[int] = None) -> list:
        self._view(role)
        if status is not None and status not in WINDOW_STATES:
            from .domain import ValidationError
            raise ValidationError("status不在允许范围内")
        windows = self.repository.list_voyage_windows(status=status, item_id=item_id)
        return [self._enrich_window(w) for w in windows]

    def get_voyage_window(self, window_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self._enrich_window(self.repository.get_voyage_window(window_id))

    def voyage_progress(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        item = self.repository.get_item(item_id)
        progress = self.repository.recovery_progress(item_id)
        event_qty = float(item["quantity"])
        progress["event_qty"] = event_qty
        # 三类数量：预计回收量、实际回收量、油水分离后纯油量
        progress["recovered_ratio"] = (
            progress["recovered_qty"] / event_qty if event_qty > 0 else 0.0)
        return progress

    def _enrich_window(self, window: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(window)
        result["can_reconfirm"] = window["status"] == PENDING
        result["can_register_return"] = window["status"] == CONFIRMED
        result["counted_in_progress"] = window["status"] == COMPLETED
        result["rules"] = {
            "max_sea_state": MAX_SEA_STATE,
            "min_recovery_ratio": MIN_RECOVERY_RATIO}
        return result

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
