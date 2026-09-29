"""方案推演与四维评分。

所有推演都在冻结的世界状态上进行（见 ``EventStore.branch``），不写入事件；
四个维度同时计入：相邻项目准备时间、恢复负荷、替补可用性、纪录材料完整性。
硬阻断（已检录锁定、医疗停赛、时间直接冲突）使方案不可确认；其余为扣分项。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from .model import gap_minutes, overlaps, parse_dt
from .projection import World

# 默认推演参数，可由调用方按比赛规程覆盖
DEFAULT_PREP_MINUTES = 30.0
DEFAULT_RECOVERY_HORIZON_MINUTES = 60.0
DEFAULT_WARMUP_BEFORE_MINUTES = 40.0
DEFAULT_WARMUP_LEAD_MINUTES = 10.0
DEFAULT_RECOVERY_AFTER_MINUTES = 30.0
# 每单位负荷值折抵的分数
LOAD_WEIGHT = 8.0
NEIGHBORHOOD = timedelta(hours=2)


@dataclass(frozen=True)
class SubstitutionSpec:
    """一次让渡：个人项目（entry_id 非空）或接力棒次（relay_id+slot）。

    replacement 为 None 表示弃权退出，不指定替代者。
    """

    target_event_code: str
    target_round: str
    replaced_athlete: str
    replacement: str | None
    entry_id: str | None = None
    relay_id: str | None = None
    slot: int | None = None

    @property
    def kind(self) -> str:
        return "relay" if self.relay_id is not None else "individual"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target_event_code": self.target_event_code,
            "target_round": self.target_round,
            "replaced_athlete": self.replaced_athlete,
            "replacement": self.replacement,
            "entry_id": self.entry_id,
            "relay_id": self.relay_id,
            "slot": self.slot,
        }


@dataclass
class Finding:
    dimension: str  # prep_gap / recovery_load / availability / record_material
    severity: str  # blocker / penalty / info
    message: str
    subject: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "severity": self.severity,
            "message": self.message,
            "subject": self.subject,
        }


@dataclass
class Evaluation:
    feasible: bool
    scores: dict[str, float]
    total: float
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "feasible": self.feasible,
            "scores": dict(self.scores),
            "total": round(self.total, 2),
            "findings": [f.to_dict() for f in self.findings],
        }


@dataclass
class Impact:
    """一次让渡对其他安排的连锁影响。"""

    subject: str
    relation: str  # replacement / replaced
    event_code: str
    round: str
    before_minutes: float | None
    after_minutes: float | None
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "relation": self.relation,
            "event_code": self.event_code,
            "round": self.round,
            "before_minutes": None if self.before_minutes is None else round(self.before_minutes, 1),
            "after_minutes": None if self.after_minutes is None else round(self.after_minutes, 1),
            "detail": self.detail,
        }


@dataclass
class Simulation:
    spec: SubstitutionSpec
    evaluation: Evaluation
    occupied_windows: list[dict[str, Any]]
    released_arrangements: list[dict[str, Any]]
    impacts: list[Impact]

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec": self.spec.to_dict(),
            "evaluation": self.evaluation.to_dict(),
            "occupied_windows": copy.deepcopy(self.occupied_windows),
            "released_arrangements": copy.deepcopy(self.released_arrangements),
            "impacts": [i.to_dict() for i in self.impacts],
        }


def target_window(world: World, spec: SubstitutionSpec) -> tuple[Any, Any]:
    """返回目标项目的 (scheduled_start, scheduled_end)。"""
    if spec.kind == "individual":
        entry = world.entries[spec.entry_id]
        return parse_dt(entry.scheduled_start), parse_dt(entry.scheduled_end)
    relay = world.relays[spec.relay_id]
    if relay.scheduled_start and relay.scheduled_end:
        return parse_dt(relay.scheduled_start), parse_dt(relay.scheduled_end)
    raise KeyError(f"接力 {spec.relay_id} 缺少赛程时间")


def evaluate(
    world: World,
    spec: SubstitutionSpec,
    *,
    required_prep_minutes: float = DEFAULT_PREP_MINUTES,
    recovery_horizon_minutes: float = DEFAULT_RECOVERY_HORIZON_MINUTES,
) -> Evaluation:
    findings: list[Finding] = []
    scores: dict[str, float] = {}

    # 硬阻断 1：该轮次已检录锁定
    revision = world.locked_revision(spec.target_event_code, spec.target_round)
    if revision is not None:
        findings.append(
            Finding(
                "availability",
                "blocker",
                f"项目 {spec.target_event_code}/{spec.target_round} 已在 "
                f"{revision.locked_at} 完成检录（版本 {revision.roster_version}），不得再换人",
            )
        )

    start, end = target_window(world, spec)

    if spec.replacement is None:
        # 弃权：不指定替代者，只释放占用
        scores.update(
            {"prep_gap": 100.0, "recovery_load": 100.0, "availability": 100.0, "record_material": 100.0}
        )
        findings.append(Finding("availability", "info", "弃权方案：不指定替代者，仅释放占用"))
        feasible = not any(f.severity == "blocker" for f in findings)
        return Evaluation(feasible, scores, 100.0 if feasible else 0.0, findings)

    athlete = world.athletes.get(spec.replacement)
    if athlete is None:
        findings.append(Finding("availability", "blocker", f"替代者 {spec.replacement} 未登记"))
        scores = {"prep_gap": 0.0, "recovery_load": 0.0, "availability": 0.0, "record_material": 0.0}
        return Evaluation(False, scores, 0.0, findings)

    # 硬阻断 2：医疗停赛（队医可停赛，但任何人都不能把停赛队员排进方案）
    if athlete.medical_held:
        findings.append(
            Finding(
                "availability",
                "blocker",
                f"{athlete.name} 处于医疗停赛：{athlete.medical_reason}",
                subject=spec.replacement,
            )
        )

    prep_score, prep_findings = _score_prep(
        world,
        spec.replacement,
        start,
        end,
        required_prep_minutes,
        (spec.target_event_code, spec.target_round),
    )
    scores["prep_gap"] = prep_score
    findings.extend(prep_findings)

    load_score, load_findings = _score_recovery_load(
        world, spec.replacement, start, recovery_horizon_minutes
    )
    scores["recovery_load"] = load_score
    findings.extend(load_findings)

    avail_score, avail_findings = _score_availability(world, spec, spec.replacement, start, end)
    scores["availability"] = avail_score
    findings.extend(avail_findings)

    material_score, material_findings = _score_record_material(world, spec, spec.replacement)
    scores["record_material"] = material_score
    findings.extend(material_findings)

    feasible = not any(f.severity == "blocker" for f in findings)
    total = round(sum(scores.values()) / len(scores), 2)
    return Evaluation(feasible, scores, total if feasible else min(total, 59.0), findings)


def _score_prep(
    world: World,
    athlete_id: str,
    start: Any,
    end: Any,
    required_prep_minutes: float,
    spec_target: tuple[str, str],
) -> tuple[float, list[Finding]]:
    findings: list[Finding] = []
    worst_gap: float | None = None
    for entry in world.entries.values():
        if entry.athlete_id != athlete_id or entry.released:
            continue
        # 替代者本人在目标项目同轮次的报名（如其替补席位）不算相邻冲突
        if (entry.event_code, entry.round) == (spec_target[0], spec_target[1]):
            continue
        other_start = parse_dt(entry.scheduled_start)
        other_end = parse_dt(entry.scheduled_end)
        if overlaps(other_start, other_end, start, end):
            findings.append(
                Finding(
                    "prep_gap",
                    "blocker",
                    f"与相邻项目 {entry.event_code}/{entry.round} 时间直接重叠",
                    subject=athlete_id,
                )
            )
        elif other_end <= start:
            gap = gap_minutes(other_end, start)
            worst_gap = gap if worst_gap is None else min(worst_gap, gap)
            if gap < required_prep_minutes:
                findings.append(
                    Finding(
                        "prep_gap",
                        "penalty",
                        f"相邻项目 {entry.event_code}/{entry.round} 仅间隔 {gap:.0f} 分钟，"
                        f"不足 {required_prep_minutes:.0f} 分钟",
                        subject=athlete_id,
                    )
                )
    if any(f.severity == "blocker" for f in findings):
        return 0.0, findings
    if worst_gap is None:
        return 100.0, findings
    return round(min(100.0, worst_gap / required_prep_minutes * 100.0), 1), findings


def _score_recovery_load(
    world: World, athlete_id: str, start: Any, horizon_minutes: float
) -> tuple[float, list[Finding]]:
    findings: list[Finding] = []
    horizon_start = start - timedelta(minutes=horizon_minutes)
    recent = 0.0
    for window in world.athlete_windows(athlete_id):
        w_end = parse_dt(window.window_end)
        if horizon_start < w_end <= start:
            recent += window.load_value
            findings.append(
                Finding(
                    "recovery_load",
                    "info",
                    f"开赛前 {horizon_minutes:.0f} 分钟内有 "
                    f"{window.load_type} 负荷 {window.load_value}（{window.source_ref}）",
                    subject=athlete_id,
                )
            )
    score = max(0.0, 100.0 - recent * LOAD_WEIGHT)
    if recent > 0:
        findings.append(
            Finding(
                "recovery_load",
                "penalty",
                f"恢复窗口内累计负荷 {recent:.1f}，恢复储备下降",
                subject=athlete_id,
            )
        )
    return round(score, 1), findings


def _score_availability(
    world: World, spec: SubstitutionSpec, athlete_id: str, start: Any, end: Any
) -> tuple[float, list[Finding]]:
    findings: list[Finding] = []
    if world.athletes[athlete_id].medical_held:
        return 0.0, findings  # blocker 已在 evaluate 中记录

    score = 100.0
    if spec.kind == "individual":
        designated = any(
            e.athlete_id == athlete_id
            and e.event_code == spec.target_event_code
            and e.round == spec.target_round
            and e.role == "alternate"
            and not e.released
            for e in world.entries.values()
        )
    else:
        relay = world.relays[spec.relay_id]
        designated = athlete_id in relay.candidates
        # 一人不能在同一接力中占两个棒次
        if any(
            athlete_id == holder and slot != spec.slot
            for slot, holder in enumerate(relay.slots)
        ):
            findings.append(
                Finding(
                    "availability",
                    "blocker",
                    "替代者已在该接力另一棒次，一人不能同时占两棒",
                    subject=athlete_id,
                )
            )
    if not designated:
        score -= 40.0
        findings.append(
            Finding(
                "availability",
                "penalty",
                "替代者不在该项目登记替补/接力候选名单内，需要临赛授权",
                subject=athlete_id,
            )
        )

    # 已锁定项目中的时间冲突不可协调
    for (event_code, round_), revision in world.revisions.items():
        if (event_code, round_) == (spec.target_event_code, spec.target_round):
            continue
        for line in revision.entries:
            if line.get("athlete_id") != athlete_id:
                continue
            other = world.entries.get(line.get("entry_id", ""))
            if other is not None and overlaps(
                parse_dt(other.scheduled_start), parse_dt(other.scheduled_end), start, end
            ):
                findings.append(
                    Finding(
                        "availability",
                        "blocker",
                        f"替代者已在锁定名单 {event_code}/{round_} 中且时间冲突",
                        subject=athlete_id,
                    )
                )
    if any(f.severity == "blocker" for f in findings):
        return 0.0, findings
    return round(score, 1), findings


def _score_record_material(
    world: World, spec: SubstitutionSpec, athlete_id: str
) -> tuple[float, list[Finding]]:
    findings: list[Finding] = []
    prior_entries = [
        e
        for e in world.entries.values()
        if e.athlete_id == athlete_id and not e.released and e.entry_id != spec.entry_id
    ]
    if not prior_entries:
        return 100.0, findings
    split_refs = {w.source_ref for w in world.athlete_windows(athlete_id) if w.load_type == "split"}
    certified_events = {(sheet.event_code, sheet.round) for sheet in world.sheets.values()}
    missing: list[str] = []
    covered = 0
    for entry in prior_entries:
        has_split = any(ref.startswith(entry.event_code) for ref in split_refs)
        has_sheet = (entry.event_code, entry.round) in certified_events
        if has_split or has_sheet:
            covered += 1
        else:
            missing.append(f"{entry.event_code}/{entry.round}")
    score = round(covered / len(prior_entries) * 100.0, 1)
    if missing:
        findings.append(
            Finding(
                "record_material",
                "penalty",
                f"替代者缺少 {', '.join(missing)} 的分段或封存成绩，纪录申报材料不完整",
                subject=athlete_id,
            )
        )
    return score, findings


def simulate(
    world: World,
    spec: SubstitutionSpec,
    *,
    required_prep_minutes: float = DEFAULT_PREP_MINUTES,
    recovery_horizon_minutes: float = DEFAULT_RECOVERY_HORIZON_MINUTES,
    warmup_before_minutes: float = DEFAULT_WARMUP_BEFORE_MINUTES,
    warmup_lead_minutes: float = DEFAULT_WARMUP_LEAD_MINUTES,
    recovery_after_minutes: float = DEFAULT_RECOVERY_AFTER_MINUTES,
) -> Simulation:
    """在冻结世界上推演让渡：评分、占窗、释放与连锁影响。"""
    evaluation = evaluate(
        world,
        spec,
        required_prep_minutes=required_prep_minutes,
        recovery_horizon_minutes=recovery_horizon_minutes,
    )
    start, end = target_window(world, spec)

    occupied_windows: list[dict[str, Any]] = []
    if spec.replacement is not None and evaluation.feasible:
        occupied_windows = [
            _warmup_window(spec.replacement, start, warmup_before_minutes, warmup_lead_minutes),
            _recovery_window(spec.replacement, end, recovery_after_minutes),
        ]

    released_arrangements: list[dict[str, Any]] = []
    if spec.replacement is not None or spec.kind == "individual":
        for window in world.athlete_windows(spec.replaced_athlete):
            if window.load_type not in ("warmup", "recovery"):
                continue
            w_start, w_end = parse_dt(window.window_start), parse_dt(window.window_end)
            if w_start >= start - NEIGHBORHOOD and w_end <= end + NEIGHBORHOOD:
                released_arrangements.append(
                    {"kind": "window", "athlete_id": spec.replaced_athlete, "source_ref": window.source_ref}
                )
    if spec.kind == "individual":
        if spec.replacement is None:
            released_arrangements.append({"kind": "entry", "entry_id": spec.entry_id})
        else:
            # 替补升任正选时消费掉自己在该轮次的替补报名，避免名单重复
            for entry in world.active_entries(spec.target_event_code, spec.target_round):
                if entry.athlete_id == spec.replacement and entry.role == "alternate":
                    released_arrangements.append({"kind": "entry", "entry_id": entry.entry_id})

    impacts = _chain_impacts(world, spec, released_arrangements, start)
    return Simulation(
        spec=spec,
        evaluation=evaluation,
        occupied_windows=occupied_windows,
        released_arrangements=released_arrangements,
        impacts=impacts,
    )


def _warmup_window(athlete_id: str, start: Any, before: float, lead: float) -> dict[str, Any]:
    return {
        "athlete_id": athlete_id,
        "load_type": "warmup",
        "window_start": (start - timedelta(minutes=before)).isoformat(),
        "window_end": (start - timedelta(minutes=lead)).isoformat(),
        "load_value": 1.0,
    }


def _recovery_window(athlete_id: str, end: Any, after: float) -> dict[str, Any]:
    return {
        "athlete_id": athlete_id,
        "load_type": "recovery",
        "window_start": end.isoformat(),
        "window_end": (end + timedelta(minutes=after)).isoformat(),
        "load_value": 1.5,
    }


def _chain_impacts(
    world: World,
    spec: SubstitutionSpec,
    released_arrangements: list[dict[str, Any]],
    target_start: Any,
) -> list[Impact]:
    impacts: list[Impact] = []
    freed = any(r.get("athlete_id") == spec.replaced_athlete for r in released_arrangements)

    # 被让渡者：目标项目之后的其他项目恢复占用被释放
    for entry in world.entries.values():
        if entry.athlete_id != spec.replaced_athlete or entry.released:
            continue
        if (entry.event_code, entry.round) == (spec.target_event_code, spec.target_round):
            continue
        other_start = parse_dt(entry.scheduled_start)
        if other_start <= target_start:
            continue
        impacts.append(
            Impact(
                subject=spec.replaced_athlete,
                relation="replaced",
                event_code=entry.event_code,
                round=entry.round,
                before_minutes=None,
                after_minutes=None,
                detail="释放目标项目热身/恢复占用，后续项目恢复储备回升"
                if freed
                else "让渡目标项目，总负荷下降",
            )
        )

    # 替代者：新占用窗口落在其相邻项目间隔内时压缩准备时间
    if spec.replacement is None:
        return impacts
    for entry in world.entries.values():
        if (
            entry.athlete_id != spec.replacement
            or entry.released
            or (entry.event_code, entry.round) == (spec.target_event_code, spec.target_round)
        ):
            continue
        other_end = parse_dt(entry.scheduled_end)
        if other_end > target_start:
            continue
        gap = gap_minutes(other_end, target_start)
        impacts.append(
            Impact(
                subject=spec.replacement,
                relation="replacement",
                event_code=entry.event_code,
                round=entry.round,
                before_minutes=gap,
                after_minutes=max(0.0, gap - DEFAULT_WARMUP_BEFORE_MINUTES + DEFAULT_WARMUP_LEAD_MINUTES),
                detail="替补出战需新占热身/恢复窗口，相邻项目准备时间被压缩",
            )
        )
    return impacts


def rank(simulations: list[Simulation]) -> list[Simulation]:
    """按可行性优先、总分降序排列候选方案。"""
    return sorted(simulations, key=lambda s: (not s.evaluation.feasible, -s.evaluation.total))
