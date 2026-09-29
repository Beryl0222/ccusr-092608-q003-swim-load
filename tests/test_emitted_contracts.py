import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from swim_load.contracts import validate_event
from swim_load.planning import SubstitutionSpec

from scenario import build_service, HEAD, MEDICAL, STAFF


class EmittedEventsConformToContractTests(unittest.TestCase):
    def test_full_scenario_events_validate(self) -> None:
        schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
        svc = build_service()

        svc.set_medical_hold(
            MEDICAL, "a1", True, "肩部不适", occurred_at="2026-09-26T12:00:00+08:00"
        )
        svc.propose_plan(
            HEAD, "plan-1",
            SubstitutionSpec("100FR", "final", "a1", "a2", entry_id="e-100f-a1"),
            occurred_at="2026-09-26T12:05:00+08:00",
        )
        svc.confirm_plan(HEAD, "plan-1", occurred_at="2026-09-26T12:10:00+08:00")
        lock = svc.lock_checkin(
            HEAD, "100FR", "final", occurred_at="2026-09-26T17:30:00+08:00"
        )
        svc.certify_result(
            STAFF, "sheet-1", lock["revision_id"],
            [{"entry_id": "e-100f-a1", "mark": "53.21"}],
            occurred_at="2026-09-26T18:30:00+08:00",
        )
        svc.declare_record(
            HEAD, "rec-1", "100FR", "final", "meet_record",
            occurred_at="2026-09-26T18:35:00+08:00",
        )

        types_seen = set()
        for event in svc.store.all_events():
            data = event.to_dict()
            issues = validate_event(data, schema)
            self.assertEqual([], issues, f"{data['event_type']} 契约问题: {issues}")
            types_seen.add(data["event_type"])
        # 关键事件类型都真实发出过
        self.assertIn("MEDICAL_HOLD_SET", types_seen)
        self.assertIn("PLAN_PROPOSED", types_seen)
        self.assertIn("PLAN_CONFIRMED", types_seen)
        self.assertIn("ROSTER_SUBSTITUTED", types_seen)
        self.assertIn("CHECKIN_LOCKED", types_seen)
        self.assertIn("RESULT_CERTIFIED", types_seen)
        self.assertIn("RECORD_DECLARED", types_seen)

    def test_sample_file_validates(self) -> None:
        schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
        sample = json.loads((ROOT / "data/sample.json").read_text(encoding="utf-8"))
        self.assertEqual([], validate_event(sample, schema))

    def test_relay_schedule_datetime_requires_timezone(self) -> None:
        schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
        sample = json.loads((ROOT / "data/sample.json").read_text(encoding="utf-8"))
        event = dict(sample)
        event.update(
            {
                "event_id": "relay_roster:r1:v1",
                "event_type": "RELAY_CANDIDATES_LISTED",
                "aggregate_type": "relay_roster",
                "aggregate_id": "r1",
                "version": 1,
                "payload": {
                    "relay_id": "r1",
                    "candidate_ids": ["a1"],
                    "event_code": "4x100FR",
                    "round": "heat",
                    "scheduled_start": "2026-09-27T10:00:00",
                    "scheduled_end": "2026-09-27T10:10:00+08:00",
                },
            }
        )
        issues = validate_event(event, schema)
        self.assertIn(
            ("payload.scheduled_start", "timezone_required"),
            [(i.field, i.code) for i in issues],
        )


if __name__ == "__main__":
    unittest.main()
