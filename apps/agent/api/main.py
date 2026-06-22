"""Agent control plane.

v2 is intentionally LLM-free here. The user picks two map points; we use OSRM to get
the road geometry between them, resample to 20m waypoints, and enqueue a capture job.
The (vision) LLM lives in the backend service, applied per detected-damage image.
"""
from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager

from agent.routing.sampler import sample_polyline
from api.jobs import JobRegistry
from api.schemas import CreateJobRequest, CreateJobResponse, JobStatusResponse
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

from agent.routing.directions import directions_polyline  # noqa: E402

log = logging.getLogger("patchguard.api")
logging.basicConfig(level=logging.INFO)

REGISTRY = JobRegistry()
MAX_WAYPOINTS = int(os.environ.get("WORKER_MAX_WAYPOINTS", "1000"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("agent API ready (LLM-free; v2 click-to-survey)")
    yield


app = FastAPI(title="patchguard-agent", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/jobs", response_model=CreateJobResponse)
async def create_job(req: CreateJobRequest) -> CreateJobResponse:
    if not req.start_end:
        raise HTTPException(400, "start_end is required")
    se = req.start_end
    try:
        poly = await directions_polyline((se.start_lat, se.start_lng), (se.end_lat, se.end_lng))
    except Exception as e:
        log.exception("routing failed")
        raise HTTPException(502, f"routing failed: {e}") from e
    waypoints = sample_polyline(poly, every_m=se.every_m)
    if not waypoints:
        raise HTTPException(400, "no waypoints produced (start and end too close)")
    if len(waypoints) > MAX_WAYPOINTS:
        raise HTTPException(
            400,
            f"route too long ({len(waypoints)} waypoints > {MAX_WAYPOINTS}). Pick closer points.",
        )
    job_id = uuid.uuid4().hex
    await REGISTRY.enqueue(
        job_id=job_id,
        waypoints=[w.asdict() for w in waypoints],
        label=f"{se.start_lat:.4f},{se.start_lng:.4f} → {se.end_lat:.4f},{se.end_lng:.4f}",
    )
    return CreateJobResponse(job_id=job_id, status="queued")


@app.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def job_status(job_id: str) -> JobStatusResponse:
    job = REGISTRY.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return JobStatusResponse(
        job_id=job.job_id,
        label=job.label,
        state=job.state,
        captured=job.captured,
        skipped=job.skipped,
        total_waypoints=job.total_waypoints,
        next_index=job.next_index,
    )


@app.get("/worker/next")
async def worker_next() -> dict:
    job = await REGISTRY.claim_next()
    if job is None:
        raise HTTPException(503, "registry empty after claim")
    return {
        "job_id": job.job_id,
        "label": job.label,
        "waypoints": job.waypoints,
        "batch_size": int(os.environ.get("WORKER_BATCH_SIZE", "15")),
        "settle_ms": int(os.environ.get("WORKER_SETTLE_MS", "1500")),
        "upload_base": os.environ.get("PATCHGUARD_API_BASE", "http://localhost:8000"),
    }


@app.post("/worker/events/{job_id}")
async def worker_event(job_id: str, event: dict) -> dict:
    await REGISTRY.update_progress(job_id, event)
    return {"ok": True}


@app.websocket("/jobs/{job_id}/events")
async def ws_job_events(ws: WebSocket, job_id: str):
    await ws.accept()
    q = await REGISTRY.subscribe(job_id)
    job = REGISTRY.get(job_id)
    if job is not None:
        await ws.send_json({
            "t": "snapshot",
            "label": job.label,
            "state": job.state,
            "next_index": job.next_index,
            "captured": job.captured,
            "skipped": job.skipped,
            "total_waypoints": job.total_waypoints,
        })
    try:
        while True:
            event = await q.get()
            await ws.send_json(event)
            if event.get("t") == "done":
                break
    except WebSocketDisconnect:
        pass
    finally:
        await REGISTRY.unsubscribe(job_id, q)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}
