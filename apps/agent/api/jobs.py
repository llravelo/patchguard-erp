from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Job:
    job_id: str
    label: str
    waypoints: list[dict[str, float]]
    state: str = "pending"          # pending | running | done | failed
    next_index: int = 0
    captured: int = 0
    skipped: int = 0
    created_at: float = field(default_factory=time.time)

    @property
    def total_waypoints(self) -> int:
        return len(self.waypoints)


class JobRegistry:
    """In-memory job board. Worker long-polls for the next pending job.

    Events are fan-out: every subscriber gets every event for their job.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._pending: asyncio.Queue[str] = asyncio.Queue()
        self._subscribers: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}
        self._lock = asyncio.Lock()

    async def enqueue(
        self,
        job_id: str,
        waypoints: list[dict[str, float]],
        label: str,
    ) -> Job:
        async with self._lock:
            job = Job(job_id=job_id, label=label, waypoints=waypoints)
            self._jobs[job_id] = job
            await self._pending.put(job_id)
        # Fire-and-forget the planning-complete event for the dashboard.
        await self.publish(job_id, {
            "t": "route",
            "polyline": [[w["lat"], w["lng"]] for w in waypoints],
            "label": label,
        })
        return job

    async def claim_next(self, timeout: float = 25.0) -> Job | None:
        """Worker calls this to pick up the next pending job.

        Returns None after `timeout` seconds so the worker can retry rather than
        holding the connection open past undici's headersTimeout (5 min default).
        """
        try:
            job_id = await asyncio.wait_for(self._pending.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            job.state = "running"
            return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    async def update_progress(self, job_id: str, event: dict[str, Any]) -> None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            t = event.get("t")
            if t == "progress":
                idx = event.get("index")
                if isinstance(idx, int):
                    job.next_index = idx + 1
            elif t == "batch_uploaded":
                job.captured += int(event.get("count", 0))
            elif t == "waypoint_failed":
                job.skipped += 1
            elif t == "done":
                job.state = "done"
            elif t == "error":
                job.state = "failed"
        await self.publish(job_id, event)

    async def publish(self, job_id: str, event: dict[str, Any]) -> None:
        async with self._lock:
            subs = list(self._subscribers.get(job_id, []))
        for q in subs:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # drop on slow subscriber rather than block producer

    async def subscribe(self, job_id: str) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=512)
        async with self._lock:
            self._subscribers.setdefault(job_id, []).append(q)
        return q

    async def unsubscribe(self, job_id: str, q: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            subs = self._subscribers.get(job_id)
            if subs and q in subs:
                subs.remove(q)
