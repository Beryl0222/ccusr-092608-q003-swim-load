"""事件存储：内存追加日志，支持幂等与按聚合的乐观并发。

持久化由调用方通过子类或替换 `_persist` 实现；核心规则不依赖具体介质。
"""

from __future__ import annotations

import copy
import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping


class EventStoreError(Exception):
    """事件存储被违反（重复标识内容不一致、版本冲突等）。"""


@dataclass(frozen=True)
class RecordedEvent:
    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    occurred_at: str
    version: int
    payload: Mapping[str, Any]
    actor: Mapping[str, Any] | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RecordedEvent":
        return cls(
            event_id=data["event_id"],
            event_type=data["event_type"],
            aggregate_type=data["aggregate_type"],
            aggregate_id=data["aggregate_id"],
            occurred_at=data["occurred_at"],
            version=data["version"],
            payload=copy.deepcopy(dict(data["payload"])),
            actor=copy.deepcopy(dict(data["actor"])) if data.get("actor") else None,
        )

    def to_dict(self) -> dict[str, Any]:
        data = {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "occurred_at": self.occurred_at,
            "version": self.version,
            "payload": copy.deepcopy(dict(self.payload)),
        }
        if self.actor is not None:
            data["actor"] = copy.deepcopy(dict(self.actor))
        return data


@dataclass
class EventStore:
    """追加式事件日志。

    - 相同 ``event_id`` 重试：内容完全一致则幂等返回，不产生新事件。
    - 同一聚合 ``version`` 必须从 1 连续递增（乐观锁）。
    - ``append_batch`` 内任一事件不合法则整批拒绝。
    """

    _events: list[RecordedEvent] = field(default_factory=list)
    _ids: dict[str, RecordedEvent] = field(default_factory=dict)
    _aggregate_versions: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))

    def append(self, event: Mapping[str, Any]) -> RecordedEvent:
        """追加单个事件；若 event_id 已存在则按幂等返回已有事件。"""
        candidate = RecordedEvent.from_dict(dict(event))
        if candidate.event_id in self._ids:
            results = self.append_batch([event])
            if not results:
                return self._ids[candidate.event_id]
            return results[0]
        return self.append_batch([event])[0]

    def append_batch(self, events: Iterable[Mapping[str, Any]]) -> list[RecordedEvent]:
        candidates = [RecordedEvent.from_dict(dict(e)) for e in events]
        staged_versions = dict(self._aggregate_versions)
        recorded: list[RecordedEvent] = []
        for candidate in candidates:
            key = (candidate.aggregate_type, candidate.aggregate_id)
            if candidate.event_id in self._ids:
                existing = self._ids[candidate.event_id]
                if existing != candidate:
                    raise EventStoreError(
                        f"事件标识 {candidate.event_id} 已存在但内容不一致，拒绝覆盖"
                    )
                # 幂等重试：重复事件不进入本次写入，也不校验版本
                continue
            expected = staged_versions.get(key, 0) + 1
            if candidate.version != expected:
                raise EventStoreError(
                    f"聚合 {key} 版本冲突：期望 {expected}，收到 {candidate.version}"
                )
            staged_versions[key] = candidate.version
            recorded.append(candidate)
        # 全部通过校验后一次性提交
        for candidate in recorded:
            self._events.append(candidate)
            self._ids[candidate.event_id] = candidate
            self._aggregate_versions[(candidate.aggregate_type, candidate.aggregate_id)] = (
                candidate.version
            )
        return recorded

    def all_events(self) -> list[RecordedEvent]:
        return list(self._events)

    def events_for(self, aggregate_type: str, aggregate_id: str) -> list[RecordedEvent]:
        return [
            e
            for e in self._events
            if e.aggregate_type == aggregate_type and e.aggregate_id == aggregate_id
        ]

    def version_of(self, aggregate_type: str, aggregate_id: str) -> int:
        return self._aggregate_versions.get((aggregate_type, aggregate_id), 0)

    def contains(self, event_id: str) -> bool:
        return event_id in self._ids

    def basis(self) -> tuple[str, ...]:
        """当前日志的冻结依据：事件标识序列。"""
        return tuple(e.event_id for e in self._events)

    def branch(self, basis: Iterable[str]) -> "EventStore":
        """基于给定事件标识序列（须为当前日志前缀）创建分支。"""
        wanted = list(basis)
        if tuple(e.event_id for e in self._events[: len(wanted)]) != tuple(wanted):
            raise EventStoreError("推演 basis 必须是当前事件日志的前缀")
        clone = EventStore()
        for event in self._events[: len(wanted)]:
            clone.append(event.to_dict())
        return clone

    def save_jsonl(self, path: str | Path) -> None:
        path = Path(path)
        path.write_text(
            "".join(json.dumps(e.to_dict(), ensure_ascii=False) + "\n" for e in self._events),
            encoding="utf-8",
        )

    @classmethod
    def load_jsonl(cls, path: str | Path) -> "EventStore":
        store = cls()
        text = Path(path).read_text(encoding="utf-8")
        batch = [json.loads(line) for line in text.splitlines() if line.strip()]
        if batch:
            store.append_batch(batch)
        return store
