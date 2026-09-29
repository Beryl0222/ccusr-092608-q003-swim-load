import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from swim_load.store import EventStore, EventStoreError


def make_event(event_id, aggregate_type, aggregate_id, version, **extra):
    event = {
        "event_id": event_id,
        "event_type": "ATHLETE_REGISTERED",
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "occurred_at": "2026-09-26T08:00:00+08:00",
        "version": version,
        "payload": {"name": "队员甲"},
    }
    event.update(extra)
    return event


class EventStoreTests(unittest.TestCase):
    def test_versions_increment_per_aggregate(self) -> None:
        store = EventStore()
        store.append(make_event("e1", "athlete", "a1", 1))
        store.append(make_event("e2", "athlete", "a1", 2))
        store.append(make_event("e3", "athlete", "a2", 1))
        self.assertEqual(store.version_of("athlete", "a1"), 2)
        self.assertEqual(store.version_of("athlete", "a2"), 1)

    def test_version_conflict_rejected(self) -> None:
        store = EventStore()
        store.append(make_event("e1", "athlete", "a1", 1))
        with self.assertRaises(EventStoreError):
            store.append(make_event("e2", "athlete", "a1", 1))

    def test_idempotent_replay_same_event(self) -> None:
        store = EventStore()
        event = make_event("e1", "athlete", "a1", 1)
        first = store.append(event)
        again = store.append(event)
        self.assertEqual(first, again)
        self.assertEqual(1, len(store.all_events()))

    def test_same_id_different_content_rejected(self) -> None:
        store = EventStore()
        store.append(make_event("e1", "athlete", "a1", 1))
        with self.assertRaises(EventStoreError):
            store.append(make_event("e1", "athlete", "a1", 2))

    def test_batch_is_atomic(self) -> None:
        store = EventStore()
        store.append(make_event("e1", "athlete", "a1", 1))
        good = make_event("e2", "athlete", "a1", 2)
        bad = make_event("e3", "athlete", "a2", 5)  # 版本跳号
        with self.assertRaises(EventStoreError):
            store.append_batch([good, bad])
        self.assertEqual(1, len(store.all_events()))

    def test_branch_uses_prefix_basis(self) -> None:
        store = EventStore()
        store.append(make_event("e1", "athlete", "a1", 1))
        store.append(make_event("e2", "athlete", "a1", 2))
        clone = store.branch(["e1"])
        self.assertEqual(["e1"], list(clone.basis()))
        with self.assertRaises(EventStoreError):
            store.branch(["e2"])  # 非前缀


if __name__ == "__main__":
    unittest.main()
