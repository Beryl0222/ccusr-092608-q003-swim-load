"""读模型：按比赛日还原阵容、方案取舍原因与纪录证据链。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from .model import canonical_hash, parse_dt
from .projection import evidence_hash_for, fold
from .store import EventStore


@dataclass
class DayReconstruction:
    day: str
    adopted: list[dict[str, Any]]
    alternatives: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "adopted_roster": self.adopted,
            "abandoned_plans": self.alternatives,
        }


def _day_of(iso: str) -> str:
    return parse_dt(iso).date().isoformat()


def reconstruct_day(store: EventStore, day: str | date) -> DayReconstruction:
    """还原某一比赛日实际采用的阵容，以及当天被放弃方案及其原因。

    实际阵容以该日涉及项目轮次的检录锁定版本为准；未检录项目回退到当前报名状态。
    """
    day = day.isoformat() if isinstance(day, date) else day
    world = fold(store)

    def revision_day(key) -> str:
        event_code, round_ = key
        # 个人项目：取该轮次任一报名的比赛日
        for entry in world.entries.values():
            if entry.event_code == event_code and entry.round == round_:
                return _day_of(entry.scheduled_start)
        # 接力：取接力赛程日期
        for relay in world.relays.values():
            if relay.event_code == event_code and relay.round == round_ and relay.scheduled_start:
                return _day_of(relay.scheduled_start)
        # 回退：以检录锁定时间归属
        return _day_of(world.revisions[key].locked_at)

    day_keys = [key for key in world.revisions if revision_day(key) == day]

    adopted: list[dict[str, Any]] = []
    for (event_code, round_) in sorted(day_keys):
        revision = world.revisions[(event_code, round_)]
        adopted.append(
            {
                "event_code": event_code,
                "round": round_,
                "checkin_revision_id": revision.revision_id,
                "roster_version": revision.roster_version,
                "locked_at": revision.locked_at,
                "entries": [dict(line) for line in revision.entries],
            }
        )

    alternatives = []
    for plan in sorted(world.plans.values(), key=lambda p: p.proposed_at or ""):
        if not plan.proposed_at or _day_of(plan.proposed_at) != day:
            continue
        if plan.status != "discarded":
            continue
        alternatives.append(
            {
                "plan_id": plan.plan_id,
                "target": dict(plan.target),
                "reason": plan.reason,
                "evaluation": dict(plan.evaluation),
                "discarded_at": plan.decided_at,
            }
        )
    return DayReconstruction(day=day, adopted=adopted, alternatives=alternatives)


def impact_trace(store: EventStore, plan_id: str) -> dict[str, Any]:
    """展示一次让位（已确认或仅提议）怎样影响其他项目。"""
    world = fold(store)
    plan = world.plans.get(plan_id)
    if plan is None:
        raise KeyError(f"方案 {plan_id} 不存在")
    change = plan.changes[0]
    return {
        "plan_id": plan_id,
        "status": plan.status,
        "change": dict(change),
        "evaluation": dict(plan.evaluation),
        "chain": [dict(impact) for impact in plan.impacts],
        "occupied_windows": [dict(w) for w in plan.occupied_windows],
        "released_arrangements": [dict(r) for r in plan.released_arrangements],
    }


def adopted_plans(store: EventStore) -> list[dict[str, Any]]:
    """列出所有实际采用（确认）的让渡方案。"""
    world = fold(store)
    out = []
    for plan in sorted(
        (p for p in world.plans.values() if p.status == "confirmed"),
        key=lambda p: p.decided_at or "",
    ):
        out.append(
            {
                "plan_id": plan.plan_id,
                "target": dict(plan.target),
                "changes": [dict(c) for c in plan.changes],
                "decided_at": plan.decided_at,
                "evaluation": dict(plan.evaluation),
            }
        )
    return out


def verify_record_evidence(store: EventStore, declaration_id: str) -> dict[str, Any]:
    """重算纪录申报证据，证明它引用的是锁定名单与当时封存成绩。

    核验通过返回 ``verified: true``；赛后新增任何成绩/换人事件都不会改变结果，
    因为证据哈希只绑定检录版本哈希与该版本绑定的成绩单。
    """
    world = fold(store)
    declaration = world.declarations.get(declaration_id)
    if declaration is None:
        raise KeyError(f"纪录申报 {declaration_id} 不存在")
    revision = world.revision_by_id(declaration.checkin_revision_id)
    sheet = world.sheets.get(declaration.result_sheet_id)
    if revision is None or sheet is None:
        return {"declaration_id": declaration_id, "verified": False, "reason": "引用的版本或成绩单缺失"}

    roster_hash_now = canonical_hash(revision.entries)
    recomputed = evidence_hash_for(revision, sheet)
    return {
        "declaration_id": declaration_id,
        "verified": recomputed == declaration.evidence_hash
        and roster_hash_now == revision.snapshot_hash
        and sheet.snapshot_hash
        == canonical_hash(
            {
                "roster_version": revision.roster_version,
                "revision_id": revision.revision_id,
                "roster_snapshot_hash": revision.snapshot_hash,
                "results": sheet.results,
            }
        ),
        "evidence_hash": declaration.evidence_hash,
        "recomputed_hash": recomputed,
        "roster_version": revision.roster_version,
        "checkin_revision_id": revision.revision_id,
        "locked_at": revision.locked_at,
        "result_sheet_id": sheet.sheet_id,
        "certified_at": sheet.certified_at,
        "declared_at": declaration.declared_at,
    }
