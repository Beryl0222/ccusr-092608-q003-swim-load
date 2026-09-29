import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from swim_load.contracts import validate_event


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
        cls.sample = json.loads((ROOT / "data/sample.json").read_text(encoding="utf-8"))

    def _issues(self, event):
        return {(x.field, x.code) for x in validate_event(event, self.schema)}

    def test_sample_is_valid(self) -> None:
        self.assertEqual([], validate_event(self.sample, self.schema))

    def test_missing_fields_are_stable(self) -> None:
        issues = validate_event({}, self.schema)
        self.assertEqual(sorted(x.field for x in issues), [x.field for x in issues])

    def test_time_and_version_boundaries(self) -> None:
        event = dict(self.sample, occurred_at="2026-09-25T10:00:00", version=0)
        codes = self._issues(event)
        self.assertIn(("occurred_at", "timezone_required"), codes)
        self.assertIn(("version", "positive_integer"), codes)

    def test_event_payload_is_required(self) -> None:
        event = dict(self.sample, event_type="LOAD_RECORDED", payload={})
        codes = self._issues(event)
        self.assertIn(("payload.source_ref", "required"), codes)
        self.assertIn(("payload.load_value", "required"), codes)

    def test_unknown_event_is_rejected(self) -> None:
        codes = self._issues(dict(self.sample, event_type="UNKNOWN"))
        self.assertIn(("event_type", "unsupported_value"), codes)

    def test_entry_payload_requires_session_context(self) -> None:
        event = dict(self.sample, payload={})
        codes = self._issues(event)
        for field in ("athlete_id", "event_code", "round", "session_id"):
            self.assertIn((f"payload.{field}", "required"), codes)

    def test_split_event_carries_measured_splits(self) -> None:
        event = {
            "event_id": "split-1",
            "event_type": "SPLIT_RECORDED",
            "aggregate_type": "event_entry",
            "aggregate_id": "entry-1",
            "occurred_at": "2026-09-26T10:30:00+08:00",
            "version": 1,
            "payload": {
                "athlete_id": "A01",
                "event_code": "200FR",
                "round": "heat",
                "session_id": "day2",
                "source_ref": "timing-2261",
                "splits": [{"distance": 50, "time": 26.1}],
            },
        }
        self.assertEqual(set(), self._issues(event))

    def test_splits_must_be_array(self) -> None:
        event = deepcopy(self.sample)
        event.update(event_type="SPLIT_RECORDED")
        event["payload"] = {
            "athlete_id": "A01",
            "event_code": "200FR",
            "round": "heat",
            "session_id": "day2",
            "source_ref": "timing-2261",
            "splits": "50:26.1",
        }
        self.assertIn(("payload.splits", "type_mismatch"), self._issues(event))

    def test_medical_hold_cannot_name_replacement(self) -> None:
        event = deepcopy(self.sample)
        event.update(event_type="MEDICAL_HOLD_SET", aggregate_type="event_entry")
        event["payload"] = {
            "athlete_id": "A02",
            "authority": "medical",
            "reason": "肩痛观察",
            "replacement": "A09",
        }
        self.assertIn(("payload.replacement", "forbidden"), self._issues(event))

    def test_medical_hold_authority_is_medical_only(self) -> None:
        event = deepcopy(self.sample)
        event.update(event_type="MEDICAL_HOLD_SET", aggregate_type="event_entry")
        event["payload"] = {"athlete_id": "A02", "authority": "relay_coach", "reason": "轮换"}
        self.assertIn(("payload.authority", "unsupported_value"), self._issues(event))

    def test_relay_substitution_cannot_overwrite_medical_conclusion(self) -> None:
        event = deepcopy(self.sample)
        event.update(event_type="ROSTER_SUBSTITUTED", aggregate_type="relay_roster")
        event["payload"] = {
            "replaced_athlete": "A02",
            "replacement": "A09",
            "relay_event_code": "4X100FR",
            "round": "final",
            "authority": "relay_coach",
            "medical_conclusion": "fit",
        }
        self.assertIn(("payload.medical_conclusion", "forbidden"), self._issues(event))

    def test_relay_coach_cannot_author_medical_hold(self) -> None:
        event = deepcopy(self.sample)
        event.update(event_type="MEDICAL_HOLD_SET", aggregate_type="event_entry")
        event["payload"] = {"athlete_id": "A02", "authority": "medical", "reason": "观察"}
        valid = deepcopy(event)
        self.assertEqual(set(), self._issues(valid))
        event["payload"]["authority"] = "coach"
        self.assertIn(("payload.authority", "unsupported_value"), self._issues(event))

    def test_plan_drafted_carries_four_cost_dimensions(self) -> None:
        event = deepcopy(self.sample)
        event.update(event_type="PLAN_DRAFTED", aggregate_type="lineup_plan")
        event["payload"] = {
            "snapshot_ref": "snap-day2-1",
            "session_id": "day2",
            "score_prep_time": 0.8,
            "score_recovery_load": 0.4,
            "score_substitute_availability": 0.6,
            "score_record_material": 1.0,
        }
        self.assertEqual(set(), self._issues(event))

    def test_plan_confirmation_reserves_and_releases_windows(self) -> None:
        event = deepcopy(self.sample)
        event.update(event_type="PLAN_CONFIRMED", aggregate_type="lineup_plan")
        event["payload"] = {
            "snapshot_ref": "snap-day2-1",
            "reserved_windows": [{"athlete_id": "A01", "window_kind": "recovery", "minutes": 30}],
            "released_arrangements": [{"arrangement_ref": "lineup-day2-4x100fr-v3"}],
        }
        self.assertEqual(set(), self._issues(event))

    def test_checkin_lock_references_roster_version(self) -> None:
        event = deepcopy(self.sample)
        event.update(event_type="CHECKIN_LOCKED", aggregate_type="checkin_revision")
        event["payload"] = {"roster_version": "three", "locked_at": "2026-09-26T15:50:00+08:00", "session_id": "day2"}
        self.assertIn(("payload.roster_version", "type_mismatch"), self._issues(event))

    def test_record_claim_cannot_cite_post_session_results(self) -> None:
        event = deepcopy(self.sample)
        event.update(event_type="RECORD_CLAIM_FILED", aggregate_type="record_claim")
        event["payload"] = {
            "event_code": "4X100FR",
            "session_id": "day2",
            "roster_version": 3,
            "locked_revision": "checkin-day2-4x100fr",
            "result_ref": "locked-result-003",
            "post_session_result_ref": "final-results-2026",
        }
        self.assertIn(("payload.post_session_result_ref", "forbidden"), self._issues(event))

    def test_unknown_aggregate_is_rejected(self) -> None:
        codes = self._issues(dict(self.sample, aggregate_type="guess"))
        self.assertIn(("aggregate_type", "unsupported_value"), codes)


if __name__ == "__main__":
    unittest.main()
