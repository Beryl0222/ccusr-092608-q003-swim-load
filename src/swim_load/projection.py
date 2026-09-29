"""事件回放：把追加日志折叠为当前世界状态。

回放是纯函数，不修改存储；方案推演在分支存储上回放同一套折叠逻辑。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Mapping

from .model import canonical_hash
from .store import EventStore, RecordedEvent


@dataclass
class Athlete:
    id: str
    name: str
    medical_held: bool = False
    medical_reason: str | None = None
    medical_since: str | None = None


@dataclass
class Entry:
    entry_id: str
    athlete_id: str
    event_code: str
    round: str
    scheduled_start: str
    scheduled_end: str
    role: str = "seed"  # seed / alternate
    released: bool = False
    released_by_plan: str | None = None


@dataclass
class LoadWindow:
    source_ref: str
    athlete_id: str
    load_type: str
    window_start: str
    window_end: str
    load_value: float
    allocated_by: str | None = None
    released: bool = False


@dataclass
class RelayRoster:
    relay_id: str
    event_code: str | None
    round: str | None
    candidates: list[str] = field(default_factory=list)
    slots: list[str] = field(default_factory=list)
    substitutions: list[dict[str, Any]] = field(default_factory=list)
    scheduled_start: str | None = None
    scheduled_end: str | None = None


@dataclass
class CheckinRevision:
    revision_id: str
    event_code: str
    round: str
    roster_version: int
    locked_at: str
    entries: list[dict[str, Any]]
    snapshot_hash: str


@dataclass
class ResultSheet:
    sheet_id: str
    checkin_revision_id: str
    event_code: str
    round: str
    roster_version: int
    results: list[dict[str, Any]]
    snapshot_hash: str
    certified_at: str


@dataclass
class Plan:
    plan_id: str
    status: str  # proposed / confirmed / discarded
    basis: tuple[str, ...]
    target: dict[str, Any]
    changes: list[dict[str, Any]]
    windows: list[dict[str, Any]]
    evaluation: dict[str, Any]
    reason: str | None = None
    occupied_windows: list[dict[str, Any]] = field(default_factory=list)
    released_arrangements: list[dict[str, Any]] = field(default_factory=list)
    impacts: list[dict[str, Any]] = field(default_factory=list)
    proposed_at: str | None = None
    decided_at: str | None = None


@dataclass
class RecordDeclaration:
    declaration_id: str
    event_code: str
    round: str
    record_type: str
    result_sheet_id: str
    checkin_revision_id: str
    evidence_hash: str
    declared_at: str


@dataclass
class World:
    athletes: dict[str, Athlete] = field(default_factory=dict)
    entries: dict[str, Entry] = field(default_factory=dict)
    windows: dict[tuple[str, str], LoadWindow] = field(default_factory=dict)
    relays: dict[str, RelayRoster] = field(default_factory=dict)
    revisions: dict[tuple[str, str], CheckinRevision] = field(default_factory=dict)
    sheets: dict[str, ResultSheet] = field(default_factory=dict)
    plans: dict[str, Plan] = field(default_factory=dict)
    declarations: dict[str, RecordDeclaration] = field(default_factory=dict)

    def is_locked(self, event_code: str, round: str) -> bool:
        return (event_code, round) in self.revisions

    def locked_revision(self, event_code: str, round: str) -> CheckinRevision | None:
        return self.revisions.get((event_code, round))

    def revision_by_id(self, revision_id: str) -> CheckinRevision | None:
        for revision in self.revisions.values():
            if revision.revision_id == revision_id:
                return revision
        return None

    def sheet_for(self, event_code: str, round: str) -> ResultSheet | None:
        revision = self.revisions.get((event_code, round))
        if revision is None:
            return None
        for sheet in self.sheets.values():
            if sheet.checkin_revision_id == revision.revision_id:
                return sheet
        return None

    def active_entries(self, event_code: str, round: str) -> list[Entry]:
        return [
            e
            for e in self.entries.values()
            if e.event_code == event_code and e.round == round and not e.released
        ]

    def athlete_windows(self, athlete_id: str, include_released: bool = False) -> list[LoadWindow]:
        return [
            w
            for w in self.windows.values()
            if w.athlete_id == athlete_id and (include_released or not w.released)
        ]

    def snapshot(self) -> "World":
        return copy.deepcopy(self)


def fold(store: EventStore) -> World:
    return fold_events(store.all_events())


def fold_events(events: list[RecordedEvent]) -> World:
    world = World()
    for event in events:
        apply_event(world, event)
    return world


def apply_event(world: World, event: RecordedEvent) -> None:
    p = event.payload
    etype = event.event_type
    if etype == "ATHLETE_REGISTERED":
        world.athletes[event.aggregate_id] = Athlete(id=event.aggregate_id, name=p["name"])
    elif etype == "ENTRY_REGISTERED":
        world.entries[event.aggregate_id] = Entry(
            entry_id=event.aggregate_id,
            athlete_id=p["athlete_id"],
            event_code=p["event_code"],
            round=p["round"],
            scheduled_start=p["scheduled_start"],
            scheduled_end=p["scheduled_end"],
            role=p.get("entry_role", "seed"),
        )
    elif etype == "LOAD_RECORDED":
        key = (p["athlete_id"], p["source_ref"])
        world.windows[key] = LoadWindow(
            source_ref=p["source_ref"],
            athlete_id=p["athlete_id"],
            load_type=p["load_type"],
            window_start=p["window_start"],
            window_end=p["window_end"],
            load_value=float(p["load_value"]),
            allocated_by=p.get("allocated_by"),
        )
    elif etype == "MEDICAL_HOLD_SET":
        athlete = world.athletes[p["athlete_id"]]
        athlete.medical_held = bool(p["held"])
        athlete.medical_reason = p["reason"] if p["held"] else None
        athlete.medical_since = event.occurred_at if p["held"] else None
    elif etype == "RELAY_CANDIDATES_LISTED":
        relay = RelayRoster(
            relay_id=event.aggregate_id,
            event_code=p.get("event_code"),
            round=p.get("round"),
            candidates=list(p["candidate_ids"]),
            slots=list(p.get("initial_slots", p["candidate_ids"])),
            scheduled_start=p.get("scheduled_start"),
            scheduled_end=p.get("scheduled_end"),
        )
        world.relays[event.aggregate_id] = relay
    elif etype == "ROSTER_SUBSTITUTED":
        relay_id = p.get("relay_id")
        if relay_id is None:
            entry = _find_entry(world, event.aggregate_id)
            replacement = p.get("replacement")
            if replacement is None:
                entry.released = True
            else:
                entry.athlete_id = replacement
        else:
            relay = world.relays[relay_id]
            slot = int(p["slot"])
            relay.slots[slot] = p["replacement"]
            relay.substitutions.append(
                {
                    "replaced_athlete": p["replaced_athlete"],
                    "replacement": p["replacement"],
                    "slot": slot,
                    "at": event.occurred_at,
                }
            )
    elif etype == "PLAN_PROPOSED":
        world.plans[p["plan_id"]] = Plan(
            plan_id=p["plan_id"],
            status="proposed",
            basis=tuple(p["basis"]),
            target=dict(p["target"]),
            changes=copy.deepcopy(p["changes"]),
            windows=copy.deepcopy(p["windows"]),
            evaluation=copy.deepcopy(p["evaluation"]),
            impacts=copy.deepcopy(p.get("impacts", [])),
            proposed_at=event.occurred_at,
        )
    elif etype == "PLAN_CONFIRMED":
        plan = world.plans[p["plan_id"]]
        plan.status = "confirmed"
        plan.occupied_windows = copy.deepcopy(p.get("occupied_windows", []))
        plan.released_arrangements = copy.deepcopy(p.get("released_arrangements", []))
        plan.decided_at = event.occurred_at
        _apply_releases(world, plan)
    elif etype == "PLAN_DISCARDED":
        plan = world.plans[p["plan_id"]]
        plan.status = "discarded"
        plan.reason = p["reason"]
        plan.decided_at = event.occurred_at
    elif etype == "CHECKIN_LOCKED":
        rev = CheckinRevision(
            revision_id=event.aggregate_id,
            event_code=p["event_code"],
            round=p["round"],
            roster_version=int(p["roster_version"]),
            locked_at=p["locked_at"],
            entries=copy.deepcopy(p["entries"]),
            snapshot_hash=p["snapshot_hash"],
        )
        world.revisions[(p["event_code"], p["round"])] = rev
    elif etype == "RESULT_CERTIFIED":
        revision = world.revision_by_id(p["checkin_revision_id"])
        if revision is None:
            raise KeyError(f"成绩单引用了未知的检录版本: {p['checkin_revision_id']}")
        sheet = ResultSheet(
            sheet_id=event.aggregate_id,
            checkin_revision_id=p["checkin_revision_id"],
            event_code=revision.event_code,
            round=revision.round,
            roster_version=int(p["roster_version"]),
            results=copy.deepcopy(p["results"]),
            snapshot_hash=p["snapshot_hash"],
            certified_at=event.occurred_at,
        )
        world.sheets[event.aggregate_id] = sheet
    elif etype == "RECORD_DECLARED":
        world.declarations[event.aggregate_id] = RecordDeclaration(
            declaration_id=event.aggregate_id,
            event_code=p["event_code"],
            round=p["round"],
            record_type=p["record_type"],
            result_sheet_id=p["result_sheet_id"],
            checkin_revision_id=p["checkin_revision_id"],
            evidence_hash=p["evidence_hash"],
            declared_at=event.occurred_at,
        )


def _find_entry(world: World, aggregate_id: str) -> Entry:
    if aggregate_id in world.entries:
        return world.entries[aggregate_id]
    raise KeyError(f"未知的个人项目报名聚合: {aggregate_id}")


def _apply_releases(world: World, plan: Plan) -> None:
    for item in plan.released_arrangements:
        kind = item.get("kind")
        if kind == "window":
            window = world.windows.get((item["athlete_id"], item["source_ref"]))
            if window is not None:
                window.released = True
        elif kind == "entry":
            entry = world.entries.get(item["entry_id"])
            if entry is not None:
                entry.released = True
                entry.released_by_plan = plan.plan_id
        elif kind == "relay_slot_assignment":
            # 棒次让渡由紧随其后的 ROSTER_SUBSTITUTED 体现，这里仅记账
            continue


def revision_snapshot_hash(entries: list[Mapping[str, Any]]) -> str:
    """检录名单的内容哈希：只与当时锁定的名单有关。"""
    return canonical_hash(entries)


def result_snapshot_hash(revision: CheckinRevision, results: list[Mapping[str, Any]]) -> str:
    """成绩单哈希：绑定检录版本哈希与当场成绩，赛后追加数据不会改变它。"""
    return canonical_hash(
        {
            "roster_version": revision.roster_version,
            "revision_id": revision.revision_id,
            "roster_snapshot_hash": revision.snapshot_hash,
            "results": results,
        }
    )


def evidence_hash_for(revision: CheckinRevision, sheet: ResultSheet) -> str:
    return canonical_hash(
        {
            "event_code": sheet.event_code or revision.event_code,
            "round": sheet.round or revision.round,
            "roster_version": revision.roster_version,
            "roster_snapshot_hash": revision.snapshot_hash,
            "result_sheet_id": sheet.sheet_id,
            "result_snapshot_hash": sheet.snapshot_hash,
        }
    )
