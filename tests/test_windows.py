import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import TRANSITION_ROLES
class VoyageWindowTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
        item=self.service.create_item({"title":"window item","description":"voyage window ledger","severity":"major","quantity":100,"threshold":50,"external_ref":"WIN-1"},"creator","observer")
        for target in ("assessing","containing"): item=self.service.transition(item["id"],target,item["version"],"reviewer",TRANSITION_ROLES[target][0])
        self.item=item
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def plan(self,**kw):
        payload={"ship_name":"海巡01","depart_at":"2026-09-26T08:00:00Z","return_at":"2026-09-26T18:00:00Z","estimated_recovery":30,"sea_state":3}
        payload.update(kw); return payload
    def test_scheduled_then_return_counts_progress(self):
        window=self.service.create_window(self.item["id"],self.plan(),"planner","operations")
        self.assertEqual(window["status"],"scheduled"); self.assertEqual(window["reasons"],[])
        detail=self.service.get_item(self.item["id"],"viewer")["recovery_progress"]
        self.assertEqual((detail["estimated_total"],detail["recovered_total"],detail["separated_oil_total"]),(30,0,0))
        done=self.service.register_window_return(window["id"],{"actual_recovery":28,"separated_oil":20},"crew","operations")
        self.assertEqual(done["status"],"completed"); self.assertIsNotNone(done["registered_at"])
        detail=self.service.get_item(self.item["id"],"viewer")["recovery_progress"]
        self.assertEqual((detail["estimated_total"],detail["recovered_total"],detail["separated_oil_total"]),(30,28,20))
        self.assertTrue(self.repo.verify_audit_chain())
    def test_sea_state_and_low_estimate_pending_reschedule(self):
        rough=self.service.create_window(self.item["id"],self.plan(sea_state=5),"planner","operations")
        self.assertEqual(rough["status"],"pending_reschedule"); self.assertTrue(any("海况" in r for r in rough["reasons"]))
        thin=self.service.create_window(self.item["id"],self.plan(ship_name="海巡02",estimated_recovery=9),"planner","operations")
        self.assertEqual(thin["status"],"pending_reschedule"); self.assertTrue(any("一成" in r for r in thin["reasons"]))
        both=self.service.create_window(self.item["id"],self.plan(ship_name="海巡03",sea_state=6,estimated_recovery=1),"planner","operations")
        self.assertEqual(len(both["reasons"]),2)
    def test_overlap_returns_conflicting_schedule(self):
        self.service.create_window(self.item["id"],self.plan(),"planner","operations")
        with self.assertRaises(ConflictError) as ctx:
            self.service.create_window(self.item["id"],self.plan(depart_at="2026-09-26T12:00:00Z",return_at="2026-09-26T20:00:00Z"),"planner","operations")
        self.assertEqual(len(ctx.exception.details["conflicting_windows"]),1)
        other_ship=self.service.create_window(self.item["id"],self.plan(ship_name="海巡02",depart_at="2026-09-26T12:00:00Z",return_at="2026-09-26T20:00:00Z"),"planner","operations")
        self.assertEqual(other_ship["status"],"scheduled")
        back_to_back=self.service.create_window(self.item["id"],self.plan(depart_at="2026-09-26T18:00:00Z",return_at="2026-09-26T23:00:00Z"),"planner","operations")
        self.assertEqual(back_to_back["status"],"scheduled")
    def test_remeasure_and_confirm(self):
        window=self.service.create_window(self.item["id"],self.plan(sea_state=5),"planner","operations")
        still_bad=self.service.confirm_window(window["id"],self.plan(sea_state=6),"planner","operations")
        self.assertEqual(still_bad["status"],"pending_reschedule")
        fixed=self.service.confirm_window(window["id"],self.plan(sea_state=3),"planner","operations")
        self.assertEqual(fixed["status"],"scheduled"); self.assertEqual(fixed["reasons"],[])
        with self.assertRaises(ConflictError): self.service.confirm_window(window["id"],self.plan(),"planner","operations")
    def test_confirm_conflict_with_other_window(self):
        self.service.create_window(self.item["id"],self.plan(),"planner","operations")
        pending=self.service.create_window(self.item["id"],self.plan(ship_name="海巡01",depart_at="2026-09-27T08:00:00Z",return_at="2026-09-27T18:00:00Z",sea_state=5),"planner","operations")
        with self.assertRaises(ConflictError):
            self.service.confirm_window(pending["id"],self.plan(sea_state=3),"planner","operations")
    def test_return_registration_guards(self):
        pending=self.service.create_window(self.item["id"],self.plan(sea_state=5),"planner","operations")
        with self.assertRaises(ConflictError): self.service.register_window_return(pending["id"],{"actual_recovery":10,"separated_oil":8},"crew","operations")
        scheduled=self.service.create_window(self.item["id"],self.plan(),"planner","operations")
        with self.assertRaises(ValidationError): self.service.register_window_return(scheduled["id"],{"actual_recovery":10,"separated_oil":12},"crew","operations")
        self.service.register_window_return(scheduled["id"],{"actual_recovery":10,"separated_oil":8},"crew","operations")
        with self.assertRaises(ConflictError): self.service.register_window_return(scheduled["id"],{"actual_recovery":10,"separated_oil":8},"crew","operations")
    def test_permission_and_item_state_guards(self):
        with self.assertRaises(PermissionDenied): self.service.create_window(self.item["id"],self.plan(),"intruder","viewer")
        fresh=self.service.create_item({"title":"fresh","description":"not containing yet","severity":"minor","quantity":5,"threshold":10,"external_ref":"WIN-2"},"creator","observer")
        with self.assertRaises(ConflictError): self.service.create_window(fresh["id"],self.plan(),"planner","operations")
        with self.assertRaises(ValidationError): self.service.create_window(self.item["id"],self.plan(return_at="2026-09-26T07:00:00Z"),"planner","operations")
        with self.assertRaises(ValidationError): self.service.create_window(self.item["id"],self.plan(sea_state=10),"planner","operations")
    def test_list_by_status(self):
        self.service.create_window(self.item["id"],self.plan(),"planner","operations")
        self.service.create_window(self.item["id"],self.plan(ship_name="海巡02",sea_state=5),"planner","operations")
        scheduled=self.service.list_windows("viewer",item_id=self.item["id"],status="scheduled")
        pending=self.service.list_windows("viewer",status="pending_reschedule")
        self.assertEqual(len(scheduled),1); self.assertEqual(len(pending),1)
        self.assertEqual(len(self.service.list_windows("viewer",ship_name="海巡02")),1)
        with self.assertRaises(ValidationError): self.service.list_windows("viewer",status="bogus")
if __name__=="__main__": unittest.main()
