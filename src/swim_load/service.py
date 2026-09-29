"""多项负荷让渡应用服务。

职责边界：
- 命令以事件落库，事件按聚合乐观追加；确认方案是单批原子事件。
- 权限：staff 登记；medical 只管医疗停赛；relay_coach 只管接力方案；
  head_coach 决定个人/接力方案、检录与纪录申报。
- 已检录锁定的项目轮次拒绝一切换人改阵；新成绩/医疗观察只影响未锁定安排。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .model import Role, overlaps, parse_dt
from .planning import (
    DEFAULT_RECOVERY_HORIZON_MINUTES,
    DEFAULT_PREP_MINUTES,
    Finding,
    SubstitutionSpec,
    simulate,
)
from .projection import (
    World,
    evidence_hash_for,
    fold,
    result_snapshot_hash,
    revision_snapshot_hash,
)
from .store import EventStore


class DomainError(Exception):
    """业务规则被违反。"""


class PermissionDenied(DomainError):
    """角色无权执行该命令。"""


@dataclass(frozen=True)
class Actor:
    id: str
    role: Role

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "role": self.role.value}


class LoadRelayService:
    def __init__(self, store: EventStore):
        self.store = store

    # ------------------------------------------------------------------ world

    def world(self) -> World:
        return fold(self.store)

    # ------------------------------------------------------------- registration

    def register_athlete(self, actor: Actor, athlete_id: str, name: str, *, occurred_at: str) -> None:
        self._require(actor, {Role.STAFF, Role.HEAD_COACH}, "登记运动员")
        self._append(
            "ATHLETE_REGISTERED",
            "athlete",
            athlete_id,
            occurred_at,
            {"name": name},
            actor,
        )

    def register_entry(
        self,
        actor: Actor,
        entry_id: str,
        athlete_id: str,
        event_code: str,
        round: str,
        scheduled_start: str,
        scheduled_end: str,
        *,
        entry_role: str = "seed",
        occurred_at: str,
    ) -> None:
        self._require(actor, {Role.STAFF, Role.HEAD_COACH}, "登记报名")
        if entry_role not in ("seed", "alternate"):
            raise DomainError("entry_role 只能是 seed 或 alternate")
        world = self.world()
        if athlete_id not in world.athletes:
            raise DomainError(f"运动员 {athlete_id} 尚未登记")
        if parse_dt(scheduled_end) <= parse_dt(scheduled_start):
            raise DomainError("项目结束时间必须晚于开始时间")
        self._append(
            "ENTRY_REGISTERED",
            "event_entry",
            entry_id,
            occurred_at,
            {
                "athlete_id": athlete_id,
                "event_code": event_code,
                "round": round,
                "scheduled_start": scheduled_start,
                "scheduled_end": scheduled_end,
                "entry_role": entry_role,
            },
            actor,
        )

    def record_load(
        self,
        actor: Actor,
        athlete_id: str,
        source_ref: str,
        load_type: str,
        load_value: float,
        window_start: str,
        window_end: str,
        *,
        occurred_at: str,
    ) -> None:
        """登记实测分段/热身/恢复占用。

        成绩类观察（split/race）即使在项目检录后到达也允许记录，
        但不会改动锁定名单——它只进入未锁定安排的后续重算。
        """
        self._require(actor, {Role.STAFF, Role.HEAD_COACH}, "登记负荷")
        if parse_dt(window_end) <= parse_dt(window_start):
            raise DomainError("负荷窗口结束时间必须晚于开始时间")
        self._append(
            "LOAD_RECORDED",
            "load_window",
            f"{athlete_id}/{source_ref}",
            occurred_at,
            {
                "source_ref": source_ref,
                "load_value": float(load_value),
                "athlete_id": athlete_id,
                "load_type": load_type,
                "window_start": window_start,
                "window_end": window_end,
            },
            actor,
        )

    # ----------------------------------------------------------------- medical

    def set_medical_hold(
        self, actor: Actor, athlete_id: str, held: bool, reason: str, *, occurred_at: str
    ) -> None:
        """只有医疗角色可以给出或撤销医疗结论，其他任何人（含接力教练）都不行。"""
        self._require(actor, {Role.MEDICAL}, "出具或撤销医疗停赛结论")
        world = self.world()
        if athlete_id not in world.athletes:
            raise DomainError(f"运动员 {athlete_id} 尚未登记")
        if held and not reason.strip():
            raise DomainError("医疗停赛必须填写原因")
        self._append(
            "MEDICAL_HOLD_SET",
            "athlete",
            athlete_id,
            occurred_at,
            {"athlete_id": athlete_id, "held": bool(held), "reason": reason},
            actor,
        )

    # ------------------------------------------------------------------ relay

    def list_relay_candidates(
        self,
        actor: Actor,
        relay_id: str,
        candidate_ids: Sequence[str],
        event_code: str,
        round: str,
        scheduled_start: str,
        scheduled_end: str,
        *,
        occurred_at: str,
        initial_slots: Sequence[str] | None = None,
    ) -> None:
        self._require(actor, {Role.RELAY_COACH, Role.HEAD_COACH}, "编排接力候选")
        world = self.world()
        unknown = [a for a in candidate_ids if a not in world.athletes]
        if unknown:
            raise DomainError(f"候选中有未登记运动员: {', '.join(unknown)}")
        if parse_dt(scheduled_end) <= parse_dt(scheduled_start):
            raise DomainError("接力结束时间必须晚于开始时间")
        self._append(
            "RELAY_CANDIDATES_LISTED",
            "relay_roster",
            relay_id,
            occurred_at,
            {
                "relay_id": relay_id,
                "candidate_ids": list(candidate_ids),
                "event_code": event_code,
                "round": round,
                "scheduled_start": scheduled_start,
                "scheduled_end": scheduled_end,
                "initial_slots": list(initial_slots if initial_slots is not None else candidate_ids),
            },
            actor,
        )

    # ------------------------------------------------------------------ plans

    def propose_plan(
        self,
        actor: Actor,
        plan_id: str,
        spec: SubstitutionSpec,
        *,
        occurred_at: str,
        required_prep_minutes: float = DEFAULT_PREP_MINUTES,
        recovery_horizon_minutes: float = DEFAULT_RECOVERY_HORIZON_MINUTES,
    ) -> dict[str, Any]:
        """在冻结快照（当前事件序列）上推演并登记方案，不改变任何现有安排。"""
        allowed = (
            {Role.RELAY_COACH, Role.HEAD_COACH}
            if spec.kind == "relay"
            else {Role.HEAD_COACH}
        )
        self._require(actor, allowed, "提议让渡方案")
        world = self.world()
        self._validate_spec_references(world, spec)
        simulation = simulate(
            world,
            spec,
            required_prep_minutes=required_prep_minutes,
            recovery_horizon_minutes=recovery_horizon_minutes,
        )
        self._append(
            "PLAN_PROPOSED",
            "load_plan",
            plan_id,
            occurred_at,
            {
                "plan_id": plan_id,
                "basis": list(self.store.basis()),
                "target": {"event_code": spec.target_event_code, "round": spec.target_round},
                "changes": [spec.to_dict()],
                "windows": copy.deepcopy(simulation.occupied_windows),
                "evaluation": simulation.evaluation.to_dict(),
                "impacts": [i.to_dict() for i in simulation.impacts],
            },
            actor,
        )
        return simulation.to_dict()

    def confirm_plan(
        self,
        actor: Actor,
        plan_id: str,
        *,
        occurred_at: str,
        required_prep_minutes: float = DEFAULT_PREP_MINUTES,
        recovery_horizon_minutes: float = DEFAULT_RECOVERY_HORIZON_MINUTES,
    ) -> dict[str, Any]:
        """确认方案：在最新状态上重算，可行则单批原子占窗、换人、释放。"""
        world = self.world()
        plan = world.plans.get(plan_id)
        if plan is None:
            raise DomainError(f"方案 {plan_id} 不存在")
        if plan.status != "proposed":
            raise DomainError(f"方案处于 {plan.status} 状态，不能确认")
        spec = self._spec_from_plan(plan)
        allowed = (
            {Role.RELAY_COACH, Role.HEAD_COACH}
            if spec.kind == "relay"
            else {Role.HEAD_COACH}
        )
        self._require(actor, allowed, "确认让渡方案")

        # 确认时以当前事实重算：若期间项目被锁定或医疗状态变化，方案立即失效
        simulation = simulate(
            world,
            spec,
            required_prep_minutes=required_prep_minutes,
            recovery_horizon_minutes=recovery_horizon_minutes,
        )
        self._check_window_collisions(world, simulation)
        if not simulation.evaluation.feasible:
            blockers = [f.message for f in simulation.evaluation.findings if f.severity == "blocker"]
            raise DomainError("方案当前不可确认：" + "；".join(blockers))

        batch: list[dict[str, Any]] = []
        versions: dict[tuple[str, str], int] = {}

        def next_version(aggregate_type: str, aggregate_id: str) -> int:
            key = (aggregate_type, aggregate_id)
            versions[key] = versions.get(key, self.store.version_of(*key)) + 1
            return versions[key]

        # 1) 一次性占用替代者所需窗口
        for window in simulation.occupied_windows:
            source_ref = f"plan:{plan_id}:{window['athlete_id']}:{window['load_type']}"
            batch.append(
                self._event_dict(
                    "LOAD_RECORDED",
                    "load_window",
                    f"{window['athlete_id']}/{source_ref}",
                    next_version("load_window", f"{window['athlete_id']}/{source_ref}"),
                    occurred_at,
                    {
                        "source_ref": source_ref,
                        "load_value": window["load_value"],
                        "athlete_id": window["athlete_id"],
                        "load_type": window["load_type"],
                        "window_start": window["window_start"],
                        "window_end": window["window_end"],
                        "allocated_by": plan_id,
                    },
                    actor,
                )
            )

        # 2) 换人（被替代安排在第 3 步随方案确认事件释放）
        if spec.replacement is not None:
            if spec.kind == "relay":
                batch.append(
                    self._event_dict(
                        "ROSTER_SUBSTITUTED",
                        "relay_roster",
                        spec.relay_id,
                        next_version("relay_roster", spec.relay_id),
                        occurred_at,
                        {
                            "replaced_athlete": spec.replaced_athlete,
                            "replacement": spec.replacement,
                            "relay_id": spec.relay_id,
                            "slot": spec.slot,
                        },
                        actor,
                    )
                )
            else:
                batch.append(
                    self._event_dict(
                        "ROSTER_SUBSTITUTED",
                        "event_entry",
                        spec.entry_id,
                        next_version("event_entry", spec.entry_id),
                        occurred_at,
                        {
                            "replaced_athlete": spec.replaced_athlete,
                            "replacement": spec.replacement,
                            "relay_id": None,
                            "slot": None,
                        },
                        actor,
                    )
                )

        # 3) 方案确认：释放被替代安排（与上面的占用同一事务）
        batch.append(
            self._event_dict(
                "PLAN_CONFIRMED",
                "load_plan",
                plan_id,
                versions.get(("load_plan", plan_id), self.store.version_of("load_plan", plan_id)) + 1,
                occurred_at,
                {
                    "plan_id": plan_id,
                    "occupied_windows": copy.deepcopy(simulation.occupied_windows),
                    "released_arrangements": copy.deepcopy(simulation.released_arrangements),
                    "evaluation_at_confirmation": simulation.evaluation.to_dict(),
                },
                actor,
            )
        )
        self.store.append_batch(batch)
        return simulation.to_dict()

    def discard_plan(self, actor: Actor, plan_id: str, reason: str, *, occurred_at: str) -> None:
        world = self.world()
        plan = world.plans.get(plan_id)
        if plan is None:
            raise DomainError(f"方案 {plan_id} 不存在")
        if plan.status != "proposed":
            raise DomainError(f"方案处于 {plan.status} 状态，不能放弃")
        spec = self._spec_from_plan(plan)
        allowed = (
            {Role.RELAY_COACH, Role.HEAD_COACH}
            if spec.kind == "relay"
            else {Role.HEAD_COACH}
        )
        self._require(actor, allowed, "放弃方案")
        if not reason.strip():
            raise DomainError("放弃方案必须记录原因")
        self._append(
            "PLAN_DISCARDED",
            "load_plan",
            plan_id,
            occurred_at,
            {"plan_id": plan_id, "reason": reason},
            actor,
        )

    # ---------------------------------------------------------------- check-in

    def lock_checkin(
        self, actor: Actor, event_code: str, round: str, *, occurred_at: str
    ) -> dict[str, Any]:
        """完成检录：把当时的个人名单与接力棒次冻结为独立版本。"""
        self._require(actor, {Role.HEAD_COACH}, "锁定检录")
        world = self.world()
        if world.is_locked(event_code, round):
            raise DomainError(f"{event_code}/{round} 已完成检录锁定")
        if not world.active_entries(event_code, round) and not [
            r for r in world.relays.values() if r.event_code == event_code and r.round == round
        ]:
            raise DomainError(f"{event_code}/{round} 没有可锁定的报名安排")

        entries = []
        for entry in sorted(world.active_entries(event_code, round), key=lambda e: e.entry_id):
            athlete = world.athletes[entry.athlete_id]
            entries.append(
                {
                    "entry_id": entry.entry_id,
                    "athlete_id": entry.athlete_id,
                    "athlete_name": athlete.name,
                    "role": entry.role,
                }
            )
        for relay in sorted(
            (r for r in world.relays.values() if r.event_code == event_code and r.round == round),
            key=lambda r: r.relay_id,
        ):
            for slot, athlete_id in enumerate(relay.slots):
                entries.append(
                    {
                        "relay_id": relay.relay_id,
                        "slot": slot,
                        "athlete_id": athlete_id,
                        "athlete_name": world.athletes[athlete_id].name,
                        "role": "relay",
                    }
                )

        roster_version = self._next_roster_version(event_code)
        snapshot_hash = revision_snapshot_hash(entries)
        revision_id = f"rev-{event_code}-{round}-v{roster_version}"
        self._append(
            "CHECKIN_LOCKED",
            "checkin_revision",
            revision_id,
            occurred_at,
            {
                "roster_version": roster_version,
                "locked_at": occurred_at,
                "event_code": event_code,
                "round": round,
                "entries": entries,
                "snapshot_hash": snapshot_hash,
            },
            actor,
        )
        return {"revision_id": revision_id, "snapshot_hash": snapshot_hash, "entries": entries}

    def certify_result(
        self,
        actor: Actor,
        result_sheet_id: str,
        checkin_revision_id: str,
        results: Sequence[Mapping[str, Any]],
        *,
        occurred_at: str,
    ) -> dict[str, Any]:
        """封存当场成绩：成绩与锁定名单版本绑定，之后名单再怎么变都不影响它。"""
        self._require(actor, {Role.STAFF, Role.HEAD_COACH}, "封存成绩")
        world = self.world()
        revision = world.revision_by_id(checkin_revision_id)
        if revision is None:
            raise DomainError(f"检录版本 {checkin_revision_id} 不存在")
        if result_sheet_id in world.sheets:
            raise DomainError(f"成绩单 {result_sheet_id} 已存在")
        locked_ids = {
            line.get("entry_id") or f"{line.get('relay_id')}#{line.get('slot')}"
            for line in revision.entries
        }
        for line in results:
            marker = line.get("entry_id") or (
                f"{line.get('relay_id')}#{line.get('slot')}" if line.get("relay_id") is not None else None
            )
            if marker not in locked_ids:
                raise DomainError(
                    f"成绩行不属于检录版本 {checkin_revision_id} 的锁定名单：{line!r}"
                )
        snapshot_hash = result_snapshot_hash(revision, list(results))
        self._append(
            "RESULT_CERTIFIED",
            "result_sheet",
            result_sheet_id,
            occurred_at,
            {
                "checkin_revision_id": checkin_revision_id,
                "roster_version": revision.roster_version,
                "results": list(copy.deepcopy(dict(r)) for r in results),
                "snapshot_hash": snapshot_hash,
            },
            actor,
        )
        return {"result_sheet_id": result_sheet_id, "snapshot_hash": snapshot_hash}

    def declare_record(
        self,
        actor: Actor,
        declaration_id: str,
        event_code: str,
        round: str,
        record_type: str,
        *,
        occurred_at: str,
    ) -> dict[str, Any]:
        """申报纪录：证据只取锁定名单与封存成绩单，哈希入库存证。"""
        self._require(actor, {Role.HEAD_COACH, Role.STAFF}, "申报纪录")
        world = self.world()
        revision = world.locked_revision(event_code, round)
        if revision is None:
            raise DomainError(f"{event_code}/{round} 尚未锁定检录，无名单可引用")
        sheet = world.sheet_for(event_code, round)
        if sheet is None:
            raise DomainError(f"{event_code}/{round} 尚无封存成绩，不能申报纪录")
        evidence_hash = evidence_hash_for(revision, sheet)
        self._append(
            "RECORD_DECLARED",
            "result_sheet",
            declaration_id,
            occurred_at,
            {
                "event_code": event_code,
                "round": round,
                "record_type": record_type,
                "result_sheet_id": sheet.sheet_id,
                "checkin_revision_id": revision.revision_id,
                "evidence_hash": evidence_hash,
            },
            actor,
        )
        return {
            "declaration_id": declaration_id,
            "evidence_hash": evidence_hash,
            "checkin_revision_id": revision.revision_id,
            "result_sheet_id": sheet.sheet_id,
        }

    # -------------------------------------------------------------- internals

    def _append(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        occurred_at: str,
        payload: Mapping[str, Any],
        actor: Actor,
    ) -> None:
        version = self.store.version_of(aggregate_type, aggregate_id) + 1
        self.store.append(
            self._event_dict(
                event_type, aggregate_type, aggregate_id, version, occurred_at, payload, actor
            )
        )

    def _event_dict(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        version: int,
        occurred_at: str,
        payload: Mapping[str, Any],
        actor: Actor,
    ) -> dict[str, Any]:
        return {
            "event_id": f"{aggregate_type}:{aggregate_id}:v{version}",
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": occurred_at,
            "version": version,
            "actor": actor.as_dict(),
            "payload": copy.deepcopy(dict(payload)),
        }

    def _require(self, actor: Actor, allowed: set[Role], action: str) -> None:
        if actor.role not in allowed:
            raise PermissionDenied(f"{actor.role.value} 无权执行「{action}」")

    def _validate_spec_references(self, world: World, spec: SubstitutionSpec) -> None:
        if spec.replaced_athlete not in world.athletes:
            raise DomainError(f"被让渡队员 {spec.replaced_athlete} 未登记")
        if spec.replacement is not None and spec.replacement not in world.athletes:
            raise DomainError(f"替代者 {spec.replacement} 未登记")
        if spec.kind == "individual":
            entry = world.entries.get(spec.entry_id)
            if entry is None:
                raise DomainError(f"报名 {spec.entry_id} 不存在")
            if entry.athlete_id != spec.replaced_athlete:
                raise DomainError("被让渡队员与该报名当前持有人不一致")
        else:
            relay = world.relays.get(spec.relay_id)
            if relay is None:
                raise DomainError(f"接力 {spec.relay_id} 不存在")
            if spec.replacement is None:
                raise DomainError("接力棒次不能空缺：请指定替代者")
            if spec.slot is None or not 0 <= spec.slot < len(relay.slots):
                raise DomainError("接力棒次超出范围")
            if relay.slots[spec.slot] != spec.replaced_athlete:
                raise DomainError("被让渡队员与该棒次当前持有人不一致")

    def _spec_from_plan(self, plan) -> SubstitutionSpec:
        change = plan.changes[0]
        return SubstitutionSpec(
            target_event_code=change["target_event_code"],
            target_round=change["target_round"],
            replaced_athlete=change["replaced_athlete"],
            replacement=change.get("replacement"),
            entry_id=change.get("entry_id"),
            relay_id=change.get("relay_id"),
            slot=change.get("slot"),
        )

    def _check_window_collisions(self, world: World, simulation) -> None:
        """替代者新占窗口不得与其既有有效窗口重叠（一人一时段一占用）。"""
        for wanted in simulation.occupied_windows:
            w_start = parse_dt(wanted["window_start"])
            w_end = parse_dt(wanted["window_end"])
            for existing in world.athlete_windows(wanted["athlete_id"]):
                if overlaps(
                    parse_dt(existing.window_start),
                    parse_dt(existing.window_end),
                    w_start,
                    w_end,
                ):
                    simulation.evaluation.findings.append(
                        Finding(
                            "availability",
                            "blocker",
                            f"新窗口与既有占用 {existing.source_ref} 重叠",
                            subject=wanted["athlete_id"],
                        )
                    )
                    simulation.evaluation.feasible = False

    def _next_roster_version(self, event_code: str) -> int:
        return (
            max(
                (r.roster_version for r in self.world().revisions.values() if r.event_code == event_code),
                default=0,
            )
            + 1
        )
