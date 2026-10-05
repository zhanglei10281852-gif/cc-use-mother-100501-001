"""事件日志与事件存储。

所有状态变化都以不可变事件追加到仅追加日志：
- 每个事件属于一个 stream（方案流、指令流、序列号流等），带流内版本号；
- 全局 seq 表示追加顺序；
- prev_hash/hash 构成哈希链，重放时校验，防止或发现日志被改写；
- idempotency_key 使重复上报/重试返回同一事件，不产生第二条有效结果；
- JSONL 落盘，服务重启后重放即可恢复全部状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
from typing import Any, Callable
from uuid import uuid4

from .contracts import canonical_fingerprint, canonical_json


class ConcurrencyError(RuntimeError):
    """追加事件时流版本与预期不符（乐观锁失败）。"""


class EventStore:
    """线程安全的 JSONL 仅追加事件存储。"""

    def __init__(self, path: str | Path | None = None,
                 clock: Callable[[], str] | None = None) -> None:
        self._path = Path(path) if path else None
        self._lock = threading.RLock()
        self._events: list[Event] = []
        self._stream_versions: dict[str, int] = {}
        self._idempotency: dict[str, str] = {}  # key -> event_id
        self.clock = clock or utc_now
        if self._path is not None and self._path.exists():
            self._load()

    # ---------- 读取 ----------

    @property
    def events(self) -> tuple[Event, ...]:
        with self._lock:
            return tuple(self._events)

    def stream_events(self, stream_id: str) -> tuple[Event, ...]:
        with self._lock:
            return tuple(e for e in self._events if e.stream_id == stream_id)

    def all_after(self, seq: int) -> tuple[Event, ...]:
        with self._lock:
            return tuple(e for e in self._events if e.seq > seq)

    def find_idempotent(self, key: str | None) -> Event | None:
        if not key:
            return None
        with self._lock:
            event_id = self._idempotency.get(key)
            if event_id is None:
                return None
            return next(e for e in self._events if e.event_id == event_id)

    # ---------- 写入 ----------

    def append(
        self,
        stream_id: str,
        event_type: str,
        data: dict[str, Any],
        *,
        actor: str,
        role: str | None = None,
        occurred_at: str | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> Event:
        """追加单个事件；带幂等键时重复调用返回首次事件。"""
        events = self.append_many(
            [
                PendingEvent(
                    stream_id=stream_id,
                    event_type=event_type,
                    data=data,
                    actor=actor,
                    role=role,
                    occurred_at=occurred_at,
                    causation_id=causation_id,
                    correlation_id=correlation_id,
                    idempotency_key=idempotency_key,
                    expected_version=expected_version,
                )
            ]
        )
        return events[0]

    def append_many(self, pending: list["PendingEvent"]) -> list[Event]:
        """原子地追加一批事件：要么全部落盘，要么都不生效。"""
        with self._lock:
            # 纯重试：整批仅一个事件且幂等键已处理，原样返回首次事件
            if (
                len(pending) == 1
                and pending[0].idempotency_key
                and pending[0].idempotency_key in self._idempotency
            ):
                return [self.find_idempotent(pending[0].idempotency_key)]  # type: ignore[list-item]
            # 先做全部检查，避免半批次写入
            for item in pending:
                if item.idempotency_key and item.idempotency_key in self._idempotency:
                    raise RuntimeError(
                        f"批处理中出现已处理的幂等键: {item.idempotency_key}"
                    )
                current = self._stream_versions.get(item.stream_id, 0)
                if item.expected_version is not None and item.expected_version != current:
                    raise ConcurrencyError(
                        f"流 {item.stream_id} 版本 {current} 与预期 {item.expected_version} 不符"
                    )

            prepared: list[Event] = []
            versions = dict(self._stream_versions)
            prev_hash = self._events[-1].hash if self._events else "GENESIS"
            for item in pending:
                versions[item.stream_id] = versions.get(item.stream_id, 0) + 1
                seq = len(self._events) + len(prepared) + 1
                causation = item.causation_id
                if causation == "@prev":
                    causation = prepared[-1].event_id if prepared else None
                meta = {
                    "actor": item.actor,
                    "role": item.role,
                    "occurred_at": item.occurred_at or self.clock(),
                    "causation_id": causation,
                    "correlation_id": item.correlation_id,
                    "idempotency_key": item.idempotency_key,
                }
                event = Event(
                    event_id=uuid4().hex,
                    stream_id=item.stream_id,
                    stream_version=versions[item.stream_id],
                    seq=seq,
                    type=item.event_type,
                    data=item.data,
                    metadata=meta,
                    prev_hash=prev_hash,
                )
                object.__setattr__(event, "hash", event.compute_hash())
                prepared.append(event)
                prev_hash = event.hash

            if self._path is not None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as handle:
                    for event in prepared:
                        handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())

            self._events.extend(prepared)
            self._stream_versions = versions
            for event, item in zip(prepared, pending):
                if item.idempotency_key:
                    self._idempotency[item.idempotency_key] = event.event_id
            return prepared

    # ---------- 重放 ----------

    def _load(self) -> None:
        assert self._path is not None
        prev_hash = "GENESIS"
        with self._path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                raw = json.loads(line)
                event = Event.from_dict(raw)
                if event.seq != len(self._events) + 1:
                    raise RuntimeError(f"事件日志在第 {line_no} 行序号不连续")
                if event.prev_hash != prev_hash:
                    raise RuntimeError(f"事件日志哈希链在第 {line_no} 行断裂")
                if event.hash != event.compute_hash():
                    raise RuntimeError(f"事件日志在第 {line_no} 行内容被篡改")
                if self._stream_versions.get(event.stream_id, 0) >= event.stream_version:
                    raise RuntimeError(f"流 {event.stream_id} 版本倒退")
                self._events.append(event)
                self._stream_versions[event.stream_id] = event.stream_version
                prev_hash = event.hash
        # 幂等索引（历史事件的幂等键存放在 metadata 中）
        for event in self._events:
            key = event.metadata.get("idempotency_key")
            if key:
                self._idempotency[key] = event.event_id


@dataclass(frozen=True, slots=True)
class PendingEvent:
    stream_id: str
    event_type: str
    data: dict[str, Any]
    actor: str
    role: str | None = None
    occurred_at: str | None = None
    causation_id: str | None = None
    correlation_id: str | None = None
    idempotency_key: str | None = None
    expected_version: int | None = None


@dataclass(frozen=True, slots=True)
class Event:
    event_id: str
    stream_id: str
    stream_version: int
    seq: int
    type: str
    data: dict[str, Any]
    metadata: dict[str, Any]
    prev_hash: str
    hash: str = ""

    def compute_hash(self) -> str:
        payload = {
            "event_id": self.event_id,
            "stream_id": self.stream_id,
            "stream_version": self.stream_version,
            "seq": self.seq,
            "type": self.type,
            "data": self.data,
            "metadata": self.metadata,
            "prev_hash": self.prev_hash,
        }
        return canonical_fingerprint(payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "stream_id": self.stream_id,
            "stream_version": self.stream_version,
            "seq": self.seq,
            "type": self.type,
            "data": self.data,
            "metadata": self.metadata,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Event":
        return cls(
            event_id=raw["event_id"],
            stream_id=raw["stream_id"],
            stream_version=raw["stream_version"],
            seq=raw["seq"],
            type=raw["type"],
            data=raw["data"],
            metadata=raw["metadata"],
            prev_hash=raw["prev_hash"],
            hash=raw["hash"],
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def event_json(event: Event) -> str:
    """便于 CLI/API 展示事件。"""
    return canonical_json(event.to_dict())
