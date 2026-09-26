from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Sequence, Tuple

from .domain import ValidationError

# 出航窗口状态：留待重排 / 已确认 / 已回港计入进度
PENDING = "pending"
CONFIRMED = "confirmed"
COMPLETED = "completed"
WINDOW_STATES = [PENDING, CONFIRMED, COMPLETED]
# 未回港、仍占用船期的状态（含留待重排，避免复测期间被二次排船）
ACTIVE_STATES = (PENDING, CONFIRMED)

MAX_SEA_STATE = 4          # 海况超过四级必须留待重排
MIN_RECOVERY_RATIO = 0.1   # 预计回收量不得低于事件油量一成
SEA_STATE_MIN, SEA_STATE_MAX = 0, 12

WINDOW_ENTITY = "VoyageWindow"
RECORD_KIND = "voyage_recovery"
SCHEDULE_ROLES = ("response_commander", "operations")
RETURN_ROLES = ("operations", "response_commander")


def parse_dt(value, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field}必须是ISO时间字符串")
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"{field}时间格式无效") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def validate_window_times(departure_at: datetime, return_at: datetime) -> None:
    # 离港到回港必须覆盖往返航程与卸油时间
    if not departure_at < return_at:
        raise ValidationError("离港时刻必须早于回港时刻")


def hold_reasons(sea_state: int, estimated_qty: float, event_qty: float) -> List[str]:
    reasons: List[str] = []
    if sea_state > MAX_SEA_STATE:
        reasons.append(f"海况{sea_state}级超过四级，留待海况复测后重排")
    if event_qty > 0 and estimated_qty < MIN_RECOVERY_RATIO * event_qty:
        reasons.append(
            f"预计回收量{estimated_qty}不到事件油量{event_qty}的一成，留待复测后重排")
    return reasons


def judge_window(sea_state: int, estimated_qty: float,
                 event_qty: float) -> Tuple[str, List[str]]:
    reasons = hold_reasons(sea_state, estimated_qty, event_qty)
    return (PENDING if reasons else CONFIRMED), reasons


def windows_overlap(start_a: datetime, end_a: datetime,
                    start_b: datetime, end_b: datetime) -> bool:
    # 半开区间：前一窗回港时刻等于下一窗离港时刻不算重叠
    return start_a < end_b and end_a > start_b


def find_conflicts(windows: Sequence[Dict], departure_at: datetime,
                   return_at: datetime, exclude_id: int = 0) -> List[Dict]:
    conflicts = []
    for window in windows:
        if window.get("id") == exclude_id or window.get("status") not in ACTIVE_STATES:
            continue
        if windows_overlap(departure_at, return_at,
                           parse_dt(window["departure_at"], "departure_at"),
                           parse_dt(window["return_at"], "return_at")):
            conflicts.append(window)
    return conflicts


def ensure_status(window: Dict, allowed: Sequence[str], action: str) -> None:
    if window["status"] not in allowed:
        raise ValidationError(
            f"窗口当前状态为{window['status']}，无法{action}")
