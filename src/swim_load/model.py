"""领域值对象、时间与哈希工具。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any


class Role(str, Enum):
    HEAD_COACH = "head_coach"
    RELAY_COACH = "relay_coach"
    MEDICAL = "medical"
    STAFF = "staff"


class LoadType(str, Enum):
    SPLIT = "split"
    WARMUP = "warmup"
    RECOVERY = "recovery"
    RACE = "race"


def parse_dt(value: str | datetime) -> datetime:
    """解析时间，拒绝无时区输入。"""
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"时间必须包含时区: {value!r}")
    return parsed


def to_iso(value: datetime) -> str:
    return value.isoformat()


def canonical_hash(data: Any) -> str:
    """对可 JSON 化数据计算稳定的 sha256，十六进制返回。"""
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def overlaps(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    return start_a < end_b and start_b < end_a


def gap_minutes(left_end: datetime, right_start: datetime) -> float:
    return (right_start - left_end) / timedelta(minutes=1)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
