"""Sampling state: only successful collection replaces the data timestamp."""
import copy

from .metrics import build_snapshot


class CollectionState:
    def __init__(self):
        self.snapshot = None
        self.last_success_monotonic = None
        self.error = None

    def succeed(self, payloads, wall_time, monotonic_time):
        candidate = build_snapshot(*payloads, at=wall_time)
        self.snapshot = candidate
        self.last_success_monotonic = monotonic_time
        self.error = None

    def fail(self, message):
        self.error = message

    def restore(self, snapshot, wall_time, monotonic_time):
        """Retain the last successful aggregate across collector restarts."""
        if not isinstance(snapshot, dict) or type(snapshot.get("generated_at")) is not int:
            raise ValueError("无效的上次成功采样")
        if not isinstance(snapshot.get("updated_at"), str):
            raise ValueError("上次采样缺少更新时间")
        age = max(0, wall_time-snapshot["generated_at"])
        self.snapshot = copy.deepcopy(snapshot)
        self.last_success_monotonic = monotonic_time-age

    def view(self, monotonic_time):
        age = (max(0, int((monotonic_time - self.last_success_monotonic) * 1000))
               if self.last_success_monotonic is not None else None)
        return {
            "schema": 1,
            "delivery": "local-only",
            "status": "waiting" if self.snapshot is None else "stale" if age >= 60000 else "ready",
            "age_ms": age,
            "error": self.error,
            "snapshot": copy.deepcopy(self.snapshot),
        }
