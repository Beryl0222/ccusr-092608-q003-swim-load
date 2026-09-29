import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from swim_load.planning import SubstitutionSpec, rank
from swim_load.service import DomainError, PermissionDenied

from scenario import build_service, HEAD, MEDICAL, RELAY, STAFF


def individual_plan(plan_id="plan-1", replaced="a1", replacement="a2", entry="e-100f-a1"):
    return SubstitutionSpec("100FR", "final", replaced, replacement, entry_id=entry)


class PermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_service()

    def test_medical_can_hold_but_not_pick_replacement(self) -> None:
        # 队医可以停赛
        self.svc.set_medical_hold(
            MEDICAL, "a1", True, "肩部不适", occurred_at="2026-09-26T12:00:00+08:00"
        )
        self.assertTrue(self.svc.world().athletes["a1"].medical_held)
        # 但队医不能提议换人
        with self.assertRaises(PermissionDenied):
            self.svc.propose_plan(
                MEDICAL, "plan-x", individual_plan(),
                occurred_at="2026-09-26T12:05:00+08:00",
            )

    def test_relay_coach_cannot_set_or_lift_medical(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.svc.set_medical_hold(
                RELAY, "a1", True, "肩", occurred_at="2026-09-26T12:00:00+08:00"
            )
        with self.assertRaises(PermissionDenied):
            self.svc.set_medical_hold(
                RELAY, "a1", False, "解除", occurred_at="2026-09-26T12:00:00+08:00"
            )

    def test_relay_coach_cannot_touch_individual_plan(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.svc.propose_plan(
                RELAY, "plan-x", individual_plan(),
                occurred_at="2026-09-26T12:00:00+08:00",
            )

    def test_staff_cannot_lock_checkin(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.svc.lock_checkin(
                STAFF, "100FR", "final", occurred_at="2026-09-26T17:30:00+08:00"
            )


class PlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_service()

    def test_medical_hold_blocks_substitution_into_held_athlete(self) -> None:
        self.svc.set_medical_hold(
            MEDICAL, "a1", True, "肩部不适", occurred_at="2026-09-26T12:00:00+08:00"
        )
        # a2 顶上 a1：a1 被停赛不影响 a2 出场，方案可行
        sim = self.svc.propose_plan(
            HEAD, "plan-ok", individual_plan(),
            occurred_at="2026-09-26T12:05:00+08:00",
        )
        self.assertTrue(sim["evaluation"]["feasible"])
        # 反过来把停赛的 a1 排进任何席位：阻断
        bad = self.svc.propose_plan(
            HEAD, "plan-bad",
            SubstitutionSpec("100FR", "final", "a2", "a1", entry_id="e-100f-a2"),
            occurred_at="2026-09-26T12:06:00+08:00",
        )
        self.assertFalse(bad["evaluation"]["feasible"])
        self.assertTrue(
            any(f["severity"] == "blocker" for f in bad["evaluation"]["findings"])
        )

    def test_four_dimensions_are_scored(self) -> None:
        sim = self.svc.propose_plan(
            HEAD, "plan-1", individual_plan(),
            occurred_at="2026-09-26T12:05:00+08:00",
        )
        scores = sim["evaluation"]["scores"]
        self.assertEqual(
            {"prep_gap", "recovery_load", "availability", "record_material"}, set(scores)
        )
        # a2 距 200混预赛结束 52 分钟，准备时间充足
        self.assertEqual(100.0, scores["prep_gap"])
        # a2 是登记替补
        self.assertEqual(100.0, scores["availability"])
        # 200混分段 17:20 结束，落在决赛前 60 分钟恢复窗内（负荷 2.0 → 扣 16）
        self.assertEqual(84.0, scores["recovery_load"])

    def test_short_prep_gap_is_penalized(self) -> None:
        # 给 a2 加一个 17:50 才结束的相邻项目占用 → 间隔 10 分钟
        self.svc.register_entry(
            STAFF, "e-50f-a2", "a2", "50BK", "heat",
            "2026-09-26T17:45:00+08:00", "2026-09-26T17:50:00+08:00",
            occurred_at="2026-09-26T08:09:00+08:00",
        )
        sim = self.svc.propose_plan(
            HEAD, "plan-1", individual_plan(),
            occurred_at="2026-09-26T12:05:00+08:00",
        )
        self.assertLess(sim["evaluation"]["scores"]["prep_gap"], 100.0)
        self.assertTrue(
            any(f["dimension"] == "prep_gap" and f["severity"] == "penalty"
                for f in sim["evaluation"]["findings"])
        )

    def test_direct_overlap_is_blocker(self) -> None:
        self.svc.register_entry(
            STAFF, "e-x-a3", "a3", "50BK", "heat",
            "2026-09-26T17:55:00+08:00", "2026-09-26T18:10:00+08:00",
            occurred_at="2026-09-26T08:09:00+08:00",
        )
        sim = self.svc.propose_plan(
            HEAD, "plan-x",
            SubstitutionSpec("100FR", "final", "a1", "a3", entry_id="e-100f-a1"),
            occurred_at="2026-09-26T12:05:00+08:00",
        )
        self.assertFalse(sim["evaluation"]["feasible"])

    def test_plan_is_simulated_on_frozen_basis_only(self) -> None:
        # 提议时记录 basis；之后事实变化不回写方案评估
        sim = self.svc.propose_plan(
            HEAD, "plan-1", individual_plan(),
            occurred_at="2026-09-26T12:05:00+08:00",
        )
        basis_before = list(self.svc.world().plans["plan-1"].basis)
        self.svc.set_medical_hold(
            MEDICAL, "a2", True, "突发", occurred_at="2026-09-26T13:00:00+08:00"
        )
        plan = self.svc.world().plans["plan-1"]
        self.assertEqual(basis_before, list(plan.basis))
        # 确认时以最新事实重算 → a2 被停赛，确认被拒
        with self.assertRaises(DomainError):
            self.svc.confirm_plan(
                HEAD, "plan-1", occurred_at="2026-09-26T13:05:00+08:00"
            )


class ConfirmationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_service()
        self.svc.set_medical_hold(
            MEDICAL, "a1", True, "肩部不适", occurred_at="2026-09-26T12:00:00+08:00"
        )
        self.svc.propose_plan(
            HEAD, "plan-1", individual_plan(),
            occurred_at="2026-09-26T12:05:00+08:00",
        )

    def test_confirm_atomically_occupies_and_releases(self) -> None:
        before = len(self.svc.store.all_events())
        self.svc.confirm_plan(HEAD, "plan-1", occurred_at="2026-09-26T12:10:00+08:00")
        world = self.svc.world()
        # 换人生效
        self.assertEqual("a2", world.entries["e-100f-a1"].athlete_id)
        # a2 的替补席位被释放
        self.assertTrue(world.entries["e-100f-a2"].released)
        # 替代者热身/恢复窗口被一次性占用
        a2_windows = world.athlete_windows("a2")
        types = {w.load_type for w in a2_windows}
        self.assertIn("warmup", types)
        self.assertIn("recovery", types)
        # 一次性提交：占窗2 + 换人1 + 确认1
        self.assertEqual(4, len(self.svc.store.all_events()) - before)

    def test_cannot_confirm_twice(self) -> None:
        self.svc.confirm_plan(HEAD, "plan-1", occurred_at="2026-09-26T12:10:00+08:00")
        with self.assertRaises(DomainError):
            self.svc.confirm_plan(HEAD, "plan-1", occurred_at="2026-09-26T12:11:00+08:00")

    def test_discard_requires_reason(self) -> None:
        with self.assertRaises(DomainError):
            self.svc.discard_plan(HEAD, "plan-1", "  ", occurred_at="2026-09-26T12:10:00+08:00")

    def test_withdraw_without_replacement_releases_entry(self) -> None:
        svc = build_service()
        svc.propose_plan(
            HEAD, "plan-w",
            SubstitutionSpec("100FR", "final", "a1", None, entry_id="e-100f-a1"),
            occurred_at="2026-09-26T12:05:00+08:00",
        )
        svc.confirm_plan(HEAD, "plan-w", occurred_at="2026-09-26T12:10:00+08:00")
        self.assertTrue(svc.world().entries["e-100f-a1"].released)


class CheckinLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_service()
        self.svc.propose_plan(
            HEAD, "plan-1", individual_plan(),
            occurred_at="2026-09-26T12:05:00+08:00",
        )
        self.svc.confirm_plan(HEAD, "plan-1", occurred_at="2026-09-26T12:10:00+08:00")
        self.lock = self.svc.lock_checkin(
            HEAD, "100FR", "final", occurred_at="2026-09-26T17:30:00+08:00"
        )

    def test_lock_creates_independent_version(self) -> None:
        self.assertEqual("rev-100FR-final-v1", self.lock["revision_id"])
        world = self.svc.world()
        revision = world.locked_revision("100FR", "final")
        self.assertIsNotNone(revision)
        self.assertEqual(1, revision.roster_version)
        # 只有升任正选的 a2，替补席位已释放
        self.assertEqual(["a2"], [line["athlete_id"] for line in revision.entries])

    def test_locked_round_rejects_late_substitution(self) -> None:
        sim = self.svc.propose_plan(
            HEAD, "plan-late",
            SubstitutionSpec("100FR", "final", "a2", "a3", entry_id="e-100f-a1"),
            occurred_at="2026-09-26T17:35:00+08:00",
        )
        self.assertFalse(sim["evaluation"]["feasible"])
        with self.assertRaises(DomainError):
            self.svc.confirm_plan(
                HEAD, "plan-late", occurred_at="2026-09-26T17:36:00+08:00"
            )

    def test_cannot_lock_twice(self) -> None:
        with self.assertRaises(DomainError):
            self.svc.lock_checkin(
                HEAD, "100FR", "final", occurred_at="2026-09-26T17:40:00+08:00"
            )

    def test_late_result_does_not_change_locked_roster(self) -> None:
        # 成绩/观察在检录后到达：记录允许，锁定名单不变
        self.svc.record_load(
            STAFF, "a2", "late-split", "split", 4.0,
            "2026-09-26T18:05:00+08:00", "2026-09-26T18:15:00+08:00",
            occurred_at="2026-09-26T18:16:00+08:00",
        )
        revision = self.svc.world().locked_revision("100FR", "final")
        self.assertEqual(self.lock["snapshot_hash"], revision.snapshot_hash)


class ResultAndRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_service()
        self.svc.propose_plan(
            HEAD, "plan-1", individual_plan(),
            occurred_at="2026-09-26T12:05:00+08:00",
        )
        self.svc.confirm_plan(HEAD, "plan-1", occurred_at="2026-09-26T12:10:00+08:00")
        self.lock = self.svc.lock_checkin(
            HEAD, "100FR", "final", occurred_at="2026-09-26T17:30:00+08:00"
        )

    def test_certified_result_must_belong_to_locked_roster(self) -> None:
        with self.assertRaises(DomainError):
            self.svc.certify_result(
                STAFF, "sheet-bad", self.lock["revision_id"],
                [{"entry_id": "e-100f-a2", "mark": "99.99"}],  # 该席位已释放，不在锁定名单
                occurred_at="2026-09-26T18:20:00+08:00",
            )

    def test_record_evidence_ignores_post_race_data(self) -> None:
        self.svc.certify_result(
            STAFF, "sheet-1", self.lock["revision_id"],
            [{"entry_id": "e-100f-a1", "mark": "53.21"}],
            occurred_at="2026-09-26T18:30:00+08:00",
        )
        decl = self.svc.declare_record(
            HEAD, "rec-1", "100FR", "final", "meet_record",
            occurred_at="2026-09-26T18:35:00+08:00",
        )
        # 赛后追加成绩/观察
        self.svc.record_load(
            STAFF, "a1", "post-race", "split", 9.0,
            "2026-09-26T20:00:00+08:00", "2026-09-26T20:10:00+08:00",
            occurred_at="2026-09-26T20:11:00+08:00",
        )
        from swim_load.history import verify_record_evidence

        verdict = verify_record_evidence(self.svc.store, "rec-1")
        self.assertTrue(verdict["verified"])
        self.assertEqual(decl["evidence_hash"], verdict["recomputed_hash"])

    def test_cannot_declare_without_locked_sheet(self) -> None:
        with self.assertRaises(DomainError):
            self.svc.declare_record(
                HEAD, "rec-x", "200IM", "heat", "pb",
                occurred_at="2026-09-26T18:35:00+08:00",
            )


class RelayFlowTests(unittest.TestCase):
    def test_relay_substitution_flow(self) -> None:
        svc = build_service()
        svc.register_athlete(
            STAFF, "a5", "五姐", occurred_at="2026-09-27T07:50:00+08:00"
        )
        svc.list_relay_candidates(
            RELAY, "relay-1", ["a1", "a2", "a3", "a4", "a5"], "4x100FR", "heat",
            "2026-09-27T10:00:00+08:00", "2026-09-27T10:10:00+08:00",
            initial_slots=["a1", "a2", "a3", "a4"],
            occurred_at="2026-09-27T08:10:00+08:00",
        )
        spec = SubstitutionSpec("4x100FR", "heat", "a1", "a5", relay_id="relay-1", slot=0)
        svc.propose_plan(RELAY, "plan-r", spec, occurred_at="2026-09-27T08:20:00+08:00")
        svc.confirm_plan(RELAY, "plan-r", occurred_at="2026-09-27T08:25:00+08:00")
        relay = svc.world().relays["relay-1"]
        self.assertEqual("a5", relay.slots[0])
        self.assertEqual(1, len(relay.substitutions))
        lock = svc.lock_checkin(
            HEAD, "4x100FR", "heat", occurred_at="2026-09-27T09:20:00+08:00"
        )
        self.assertEqual(4, len(lock["entries"]))

    def test_relay_rejects_athlete_holding_another_slot(self) -> None:
        svc = build_service()
        svc.list_relay_candidates(
            RELAY, "relay-1", ["a1", "a2", "a3", "a4"], "4x100FR", "heat",
            "2026-09-27T10:00:00+08:00", "2026-09-27T10:10:00+08:00",
            initial_slots=["a1", "a2", "a3", "a4"],
            occurred_at="2026-09-27T08:10:00+08:00",
        )
        # a4 已在第 4 棒，不能再顶第 1 棒
        sim = svc.propose_plan(
            RELAY, "plan-r",
            SubstitutionSpec("4x100FR", "heat", "a1", "a4", relay_id="relay-1", slot=0),
            occurred_at="2026-09-27T08:20:00+08:00",
        )
        self.assertFalse(sim["evaluation"]["feasible"])


class RankingTests(unittest.TestCase):
    def test_rank_puts_feasible_higher(self) -> None:
        from swim_load.planning import simulate

        svc = build_service()
        svc.set_medical_hold(
            MEDICAL, "a3", True, "停赛", occurred_at="2026-09-26T12:00:00+08:00"
        )
        world = svc.world()
        good = simulate(world, individual_plan(replacement="a2"))
        bad = simulate(world, SubstitutionSpec("100FR", "final", "a1", "a3", entry_id="e-100f-a1"))
        ordered = rank([bad, good])
        self.assertIs(good, ordered[0])
        self.assertTrue(ordered[0].evaluation.feasible)
        self.assertFalse(ordered[1].evaluation.feasible)


if __name__ == "__main__":
    unittest.main()
