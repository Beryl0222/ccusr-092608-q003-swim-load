"""构造一个多项目重叠比赛日的标准场景。

时间线（2026-09-26，东八区）：
- 09:00  100自 预赛   a1（正选）
- 17:00  200混 预赛   a2（正选）
- 18:00  100自 决赛   a1（正选）、a2（替补）
- 次日 10:00 4x100自接力 预赛  候选 a1..a4
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from swim_load.model import Role
from swim_load.service import Actor, LoadRelayService
from swim_load.store import EventStore

STAFF = Actor("staff-1", Role.STAFF)
MEDICAL = Actor("med-1", Role.MEDICAL)
HEAD = Actor("head-1", Role.HEAD_COACH)
RELAY = Actor("relay-1", Role.RELAY_COACH)

ATHLETES = [("a1", "一姐"), ("a2", "二姐"), ("a3", "三姐"), ("a4", "四姐")]


def build_service() -> LoadRelayService:
    store = EventStore()
    svc = LoadRelayService(store)
    t = "2026-09-26T08:00:00+08:00"
    for aid, name in ATHLETES:
        svc.register_athlete(STAFF, aid, name, occurred_at=t)

    svc.register_entry(
        STAFF, "e-100h-a1", "a1", "100FR", "heat",
        "2026-09-26T09:00:00+08:00", "2026-09-26T09:05:00+08:00",
        occurred_at="2026-09-26T08:05:00+08:00",
    )
    svc.register_entry(
        STAFF, "e-100f-a1", "a1", "100FR", "final",
        "2026-09-26T18:00:00+08:00", "2026-09-26T18:05:00+08:00",
        occurred_at="2026-09-26T08:06:00+08:00",
    )
    svc.register_entry(
        STAFF, "e-100f-a2", "a2", "100FR", "final",
        "2026-09-26T18:00:00+08:00", "2026-09-26T18:05:00+08:00",
        entry_role="alternate", occurred_at="2026-09-26T08:07:00+08:00",
    )
    svc.register_entry(
        STAFF, "e-200h-a2", "a2", "200IM", "heat",
        "2026-09-26T17:00:00+08:00", "2026-09-26T17:08:00+08:00",
        occurred_at="2026-09-26T08:08:00+08:00",
    )
    # a1 100自预赛后实测分段（高负荷）
    svc.record_load(
        STAFF, "a1", "100FR-heat-split", "split", 3.0,
        "2026-09-26T09:05:00+08:00", "2026-09-26T09:20:00+08:00",
        occurred_at="2026-09-26T09:21:00+08:00",
    )
    # a2 200混预赛后也有分段，保证纪录材料覆盖一个项目
    svc.record_load(
        STAFF, "a2", "200IM-heat-split", "split", 2.0,
        "2026-09-26T17:08:00+08:00", "2026-09-26T17:20:00+08:00",
        occurred_at="2026-09-26T17:21:00+08:00",
    )
    return svc
