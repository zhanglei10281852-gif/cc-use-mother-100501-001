"""事件存储：哈希链、幂等、并发版本、重启重放测试。"""

import tempfile
import unittest
from pathlib import Path

from basin_dispatch.events import ConcurrencyError, EventStore, PendingEvent


class EventStoreTests(unittest.TestCase):
    def test_idempotent_retry_returns_first_event(self) -> None:
        store = EventStore()
        first = store.append("s1", "Did", {"x": 1}, actor="a", idempotency_key="k-1")
        retry = store.append("s1", "Did", {"x": 1}, actor="a", idempotency_key="k-1")
        self.assertEqual(first.event_id, retry.event_id)
        self.assertEqual(len(store.events), 1)

    def test_stream_versions_and_sequence(self) -> None:
        store = EventStore()
        e1 = store.append("s1", "A", {}, actor="a")
        e2 = store.append("s2", "B", {}, actor="a")
        e3 = store.append("s1", "C", {}, actor="a")
        self.assertEqual((e1.seq, e2.seq, e3.seq), (1, 2, 3))
        self.assertEqual((e1.stream_version, e3.stream_version), (1, 2))

    def test_optimistic_concurrency_conflict(self) -> None:
        store = EventStore()
        store.append("s1", "A", {}, actor="a")
        with self.assertRaises(ConcurrencyError):
            store.append("s1", "B", {}, actor="a", expected_version=99)

    def test_batch_is_atomic_on_version_conflict(self) -> None:
        store = EventStore()
        with self.assertRaises(ConcurrencyError):
            store.append_many([
                PendingEvent(stream_id="s1", event_type="A", data={}, actor="a"),
                PendingEvent(stream_id="s1", event_type="B", data={}, actor="a",
                             expected_version=5),
            ])
        self.assertEqual(len(store.events), 0)

    def test_hash_chain_rebuilds_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            store = EventStore(path)
            store.append("s1", "A", {}, actor="a", idempotency_key="k-1")
            store.append("s1", "B", {}, actor="a")
            reopened = EventStore(path)
            self.assertEqual(len(reopened.events), 2)
            self.assertIsNotNone(reopened.find_idempotent("k-1"))

    def test_tampered_log_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            store = EventStore(path)
            store.append("s1", "A", {"amount": 100}, actor="a")
            lines = path.read_text(encoding="utf-8").splitlines()
            import json
            raw = json.loads(lines[0])
            raw["data"]["amount"] = 999
            path.write_text(json.dumps(raw, ensure_ascii=False) + "\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                EventStore(path)


if __name__ == "__main__":
    unittest.main()
