import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service


def iso(dt):
    return dt.replace(microsecond=0).isoformat()


class VoyageWindowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        # 事件油量 100
        self.item = self.service.create_item(
            {"title": "spill", "description": "containment handed over",
             "severity": "major", "quantity": 100, "threshold": 10},
            "creator", "observer")
        self.t0 = datetime(2026, 9, 26, 2, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def schedule(self, vessel="回收船1", hours=2, estimated=20, sea=3, role="operations"):
        return self.service.schedule_voyage_window(
            self.item["id"],
            {"vessel": vessel, "departure_at": iso(self.t0),
             "return_at": iso(self.t0 + timedelta(hours=hours)),
             "estimated_qty": estimated, "sea_state": sea},
            "dispatcher", role)

    def test_confirmed_window_counts_progress_only_after_return(self):
        window = self.schedule()
        self.assertEqual(window["status"], "confirmed")
        progress = self.service.voyage_progress(self.item["id"], "viewer")
        self.assertEqual(progress["recovered_qty"], 0.0)
        self.assertEqual(progress["completed_count"], 0.0)
        returned = self.service.register_voyage_return(
            window["id"],
            {"recovered_qty": 18, "separated_oil_qty": 15,
             "separated_water_qty": 3, "separation_result": "分离正常，油15水3"},
            "captain", "operations")
        self.assertEqual(returned["status"], "completed")
        self.assertTrue(returned["counted_in_progress"])
        progress = self.service.voyage_progress(self.item["id"], "viewer")
        # 详情给三类数量：预计回收量 / 实际回收量 / 分离纯油量
        self.assertEqual(progress["estimated_qty"], 20.0)
        self.assertEqual(progress["recovered_qty"], 18.0)
        self.assertEqual(progress["separated_oil_qty"], 15.0)
        self.assertAlmostEqual(progress["recovered_ratio"], 0.18)
        records = self.service.list_records(self.item["id"], "viewer")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "closed")

    def test_schedule_overlap_returns_conflict_arrangement(self):
        self.schedule(vessel="回收船A", hours=4)
        payload = {"vessel": "回收船A",
                   "departure_at": iso(self.t0 + timedelta(hours=2)),
                   "return_at": iso(self.t0 + timedelta(hours=6)),
                   "estimated_qty": 30, "sea_state": 2}
        with self.assertRaises(ConflictError) as ctx:
            self.service.schedule_voyage_window(
                self.item["id"], payload, "dispatcher", "operations")
        conflicts = ctx.exception.details["conflicts"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["vessel"], "回收船A")
        # 首尾相接不算重叠（前船回港即可离港）
        payload["departure_at"] = iso(self.t0 + timedelta(hours=4))
        payload["return_at"] = iso(self.t0 + timedelta(hours=6))
        second = self.service.schedule_voyage_window(
            self.item["id"], payload, "dispatcher", "operations")
        self.assertEqual(second["status"], "confirmed")
        # 不同回收船互不冲突
        other = self.schedule(vessel="回收船B", hours=4)
        self.assertEqual(other["status"], "confirmed")

    def test_sea_state_and_ratio_hold_then_reconfirm(self):
        held = self.schedule(sea=5)
        self.assertEqual(held["status"], "pending")
        self.assertIn("海况", held["hold_reason"])
        self.assertTrue(held["can_reconfirm"])
        # 留待重排的窗口仍占船期，重叠安排返回冲突
        with self.assertRaises(ConflictError):
            self.schedule(vessel="回收船1", hours=1, sea=2)
        # 预计回收量不足事件油量一成（<10）同样留待重排
        low = self.schedule(vessel="回收船B", estimated=9, sea=2)
        self.assertEqual(low["status"], "pending")
        self.assertIn("一成", low["hold_reason"])
        # 复测海况仍超标：保持留待重排并说明原因
        still = self.service.reconfirm_voyage_window(
            held["id"], {"vessel": "回收船1", "departure_at": iso(self.t0),
                         "return_at": iso(self.t0 + timedelta(hours=2)),
                         "estimated_qty": 20, "sea_state": 6},
            "dispatcher", "operations")
        self.assertEqual(still["status"], "pending")
        self.assertIn("海况6级", still["hold_reason"])
        # 复测达标后重新确认
        confirmed = self.service.reconfirm_voyage_window(
            held["id"], {"vessel": "回收船1", "departure_at": iso(self.t0),
                         "return_at": iso(self.t0 + timedelta(hours=2)),
                         "estimated_qty": 20, "sea_state": 3,
                         "expected_version": still["version"]},
            "dispatcher", "operations")
        self.assertEqual(confirmed["status"], "confirmed")
        # 版本号错误返回冲突（在仍留待重排的窗口上验证）
        with self.assertRaises(ConflictError):
            self.service.reconfirm_voyage_window(
                low["id"], {"vessel": "回收船B", "departure_at": iso(self.t0),
                            "return_at": iso(self.t0 + timedelta(hours=2)),
                            "estimated_qty": 20, "sea_state": 3,
                            "expected_version": 99},
                "dispatcher", "operations")

    def test_state_actions_and_list_filters(self):
        window = self.schedule()
        # 未确认窗口不能回港登记
        held = self.schedule(vessel="回收船B", sea=5)
        with self.assertRaises(ValidationError):
            self.service.register_voyage_return(
                held["id"], {"recovered_qty": 1, "separated_oil_qty": 1,
                             "separation_result": "ok"}, "captain", "operations")
        # 已确认窗口不能重复确认
        with self.assertRaises(ValidationError):
            self.service.reconfirm_voyage_window(
                window["id"], {"vessel": "回收船1", "departure_at": iso(self.t0),
                               "return_at": iso(self.t0 + timedelta(hours=2)),
                               "estimated_qty": 20, "sea_state": 3},
                "dispatcher", "operations")
        self.service.register_voyage_return(
            window["id"], {"recovered_qty": 18, "separated_oil_qty": 15,
                           "separation_result": "ok"}, "captain", "operations")
        # 已回港窗口不能重复登记
        with self.assertRaises(ValidationError):
            self.service.register_voyage_return(
                window["id"], {"recovered_qty": 18, "separated_oil_qty": 15,
                               "separation_result": "ok"}, "captain", "operations")
        pending = self.service.list_voyage_windows("viewer", status="pending")
        confirmed = self.service.list_voyage_windows("viewer", status="confirmed")
        completed = self.service.list_voyage_windows("viewer", status="completed")
        self.assertEqual([w["id"] for w in pending], [held["id"]])
        self.assertEqual(confirmed, [])
        self.assertEqual(len(completed), 1)
        with self.assertRaises(ValidationError):
            self.service.list_voyage_windows("viewer", status="bogus")

    def test_validation_and_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.schedule(role="viewer")
        with self.assertRaises(ValidationError):
            self.service.schedule_voyage_window(
                self.item["id"],
                {"vessel": "回收船X", "departure_at": iso(self.t0),
                 "return_at": iso(self.t0 - timedelta(hours=1)),
                 "estimated_qty": 20, "sea_state": 3},
                "dispatcher", "operations")
        window = self.schedule(vessel="回收船C")
        with self.assertRaises(ValidationError):
            self.service.register_voyage_return(
                window["id"], {"recovered_qty": 10, "separated_oil_qty": 9,
                               "separated_water_qty": 5,
                               "separation_result": "水油合计超回收量"},
                "captain", "operations")
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
