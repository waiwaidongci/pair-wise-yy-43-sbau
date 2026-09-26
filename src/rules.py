from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='溢油应急响应与任务追踪'; ENTITY='溢油事件'; ID_PREFIX='OS'
SEVERITIES=['minor', 'moderate', 'major', 'catastrophic']; STATES=['reported', 'assessing', 'containing', 'recovering', 'monitoring', 'closed']; TRANSITIONS={'reported': ['assessing'], 'assessing': ['containing'], 'containing': ['recovering'], 'recovering': ['monitoring'], 'monitoring': ['closed'], 'closed': []}; TRANSITION_ROLES={'assessing': ['response_commander'], 'containing': ['response_commander'], 'recovering': ['operations'], 'monitoring': ['operations'], 'closed': ['response_commander']}
CREATE_ROLES=set(['observer', 'response_commander']); RECORD_ROLES=set(['response_commander', 'operations']); AUDIT_ROLES=set(['response_commander', 'viewer']); VIEW_ROLES=set(['observer', 'response_commander', 'operations', 'viewer'])
SEVERITY_WEIGHT={'minor': 1.0, 'moderate': 3.0, 'major': 6.0, 'catastrophic': 9.0}; DEADLINE_HOURS={'minor': 72, 'moderate': 24, 'major': 8, 'catastrophic': 4}; TERMINAL_STATES=set(['closed'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
WINDOW_ENTITY='航窗口'; WINDOW_STATES=['scheduled', 'pending_reschedule', 'completed']; WINDOW_ITEM_STATES=['containing', 'recovering', 'monitoring']
SEA_STATE_LIMIT=4; SEA_STATE_MAX=9; MIN_RECOVERY_RATIO=0.1
WINDOW_ROLES=set(['response_commander', 'operations'])
def validate_sea_state(value):
    if isinstance(value,bool): raise ValidationError("sea_state必须是整数")
    if isinstance(value,float) and value.is_integer(): value=int(value)
    if not isinstance(value,int) or value<0 or value>SEA_STATE_MAX: raise ValidationError(f"sea_state必须是0到{SEA_STATE_MAX}的整数")
    return value
def validate_window_item_state(status):
    if status not in WINDOW_ITEM_STATES: raise ConflictError(f"事件状态为{status}，需进入围控且未关闭才能安排航窗口")
def evaluate_window(sea_state,estimated_recovery,event_quantity):
    reasons=[]
    if sea_state>SEA_STATE_LIMIT: reasons.append(f"海况{sea_state}级超过{SEA_STATE_LIMIT}级上限")
    if event_quantity>0 and estimated_recovery<event_quantity*MIN_RECOVERY_RATIO: reasons.append(f"预计回收量{estimated_recovery}不足事件油量{event_quantity}的一成")
    return reasons
def windows_overlap(a_depart,a_return,b_depart,b_return): return a_depart<b_return and b_depart<a_return
def find_window_conflicts(windows,ship_name,depart_at,return_at,exclude_id=None):
    conflicts=[]
    for window in windows:
        if exclude_id is not None and window["id"]==exclude_id: continue
        if window["ship_name"]!=ship_name or window["status"]!="scheduled": continue
        if windows_overlap(depart_at,return_at,window["depart_at"],window["return_at"]): conflicts.append(window)
    return conflicts
