import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from swim_load.history import (
    adopted_plans,
    impact_trace,
    reconstruct_day,
    verify_record_evidence,
)
from swim_load.planning import SubstitutionSpec

from scenario import build_service, HEAD, MEDICAL, STAFF


class HistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_service()
        # 方案一：医疗触发 a1 -> a2，确认采用
        self.svc.set_medical_hold(
            MEDICAL, "a1", True, "肩部不适", occurred_at="2026-09-26T12:00:00+08:00"
        )
        self.svc.propose_plan(
            HEAD, "plan-adopt",
            SubstitutionSpec("100FR", "final", "a1", "a2", entry_id="e-100f-a1"),
            occurred_at="2026-09-26T12:05:00+08:00",
        )
        # 方案二：让 a3 顶上（非登记替补，评分更低），最终放弃
        self.svc.propose_plan(
            HEAD, "plan-drop",
            SubstitutionSpec("100FR", "final", "a1", "a3", entry_id="e-100f-a1"),
            occurred_at="2026-09-26T12:06:00+08:00",
        )
        self.svc.discard_plan(
            HEAD, "plan-drop", "a3 非该项目登记替补且需临赛授权，改用 a2",
            occurred_at="2026-09-26T12:08:00+08:00",
        )
        self.svc.confirm_plan(
            HEAD, "plan-adopt", occurred_at="2026-09-26T12:10:00+08:00"
        )
        self.lock = self.svc.lock_checkin(
            HEAD, "100FR", "final", occurred_at="2026-09-26T17:30:00+08:00"
        )
        self.svc.certify_result(
            STAFF, "sheet-1", self.lock["revision_id"],
            [{"entry_id": "e-100f-a1", "mark": "53.21"}],
            occurred_at="2026-09-26T18:30:00+08:00",
        )
        self.svc.declare_record(
            HEAD, "rec-1", "100FR", "final", "meet_record",
            occurred_at="2026-09-26T18:35:00+08:00",
        )

    def test_reconstruct_day_shows_roster_and_abandoned_reason(self) -> None:
        day = reconstruct_day(self.svc.store, "2026-09-26").to_dict()
        self.assertEqual(1, len(day["adopted_roster"]))
        adopted = day["adopted_roster"][0]
        self.assertEqual("100FR", adopted["event_code"])
        self.assertEqual(["a2"], [line["athlete_id"] for line in adopted["entries"]])
        self.assertEqual(1, len(day["abandoned_plans"]))
        dropped = day["abandoned_plans"][0]
        self.assertEqual("plan-drop", dropped["plan_id"])
        self.assertIn("a2", dropped["reason"])

    def test_adopted_plans_lists_only_confirmed(self) -> None:
        plans = adopted_plans(self.svc.store)
        self.assertEqual(["plan-adopt"], [p["plan_id"] for p in plans])

    def test_impact_trace_links_other_events(self) -> None:
        trace = impact_trace(self.svc.store, "plan-adopt")
        # a2 替补出战影响其 200混预赛：新占热身窗口压缩准备时间
        chain = trace["chain"]
        self.assertTrue(any(
            item["subject"] == "a2" and item["event_code"] == "200IM"
            for item in chain
        ))
        replacement_impact = next(i for i in chain if i["relation"] == "replacement")
        self.assertIsNotNone(replacement_impact["before_minutes"])
        self.assertLess(replacement_impact["after_minutes"], replacement_impact["before_minutes"])
        # 占窗与释放都可追溯
        self.assertTrue(trace["occupied_windows"])

    def test_unknown_plan_raises(self) -> None:
        with self.assertRaises(KeyError):
            impact_trace(self.svc.store, "nope")

    def test_evidence_chain_references_locked_roster(self) -> None:
        verdict = verify_record_evidence(self.svc.store, "rec-1")
        self.assertTrue(verdict["verified"])
        self.assertEqual("rev-100FR-final-v1", verdict["checkin_revision_id"])
        self.assertEqual("sheet-1", verdict["result_sheet_id"])
        self.assertLess(verdict["locked_at"], verdict["certified_at"])
        self.assertLess(verdict["certified_at"], verdict["declared_at"])

    def test_tampering_with_history_breaks_verification(self) -> None:
        # 直接在事件存储里篡改锁定名单（模拟有人赛后改名单）：证据核验必须失败
        for event in self.svc.store.all_events():
            if event.event_type == "CHECKIN_LOCKED":
                event.payload["entries"][0]["athlete_id"] = "a1"
                break
        verdict = verify_record_evidence(self.svc.store, "rec-1")
        self.assertFalse(verdict["verified"])

    def test_relay_only_day_reconstructs_from_relay_schedule(self) -> None:
        from swim_load.model import Role
        from swim_load.service import Actor

        relay_coach = Actor("relay-1", Role.RELAY_COACH)
        svc = build_service()
        svc.list_relay_candidates(
            relay_coach, "relay-9", ["a1", "a2", "a3", "a4"], "4x100FR", "heat",
            "2026-09-27T10:00:00+08:00", "2026-09-27T10:10:00+08:00",
            initial_slots=["a1", "a2", "a3", "a4"],
            occurred_at="2026-09-27T08:10:00+08:00",
        )
        svc.lock_checkin(HEAD, "4x100FR", "heat", occurred_at="2026-09-27T09:20:00+08:00")
        day = reconstruct_day(svc.store, "2026-09-27").to_dict()
        self.assertEqual(["4x100FR"], [r["event_code"] for r in day["adopted_roster"]])


if __name__ == "__main__":
    unittest.main()
