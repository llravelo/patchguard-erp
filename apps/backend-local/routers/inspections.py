"""Inspection lifecycle: creation from the dashboard, worker batch uploads (with YOLOv5 +
Vision + guarantee matching), bbox damage-report for the map, annotated image serving,
and Claude/mock report generation.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from geoalchemy2.functions import ST_DWithin, ST_Distance, ST_GeogFromText
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from db import get_session
from geo import linestring_wkt, point_wkt
from models_db import (
    Action,
    Damage,
    Image,
    Inspection,
    InspectionStatus,
    Role,
    Source,
    User,
    WorkRecord,
)
from security import current_user, require_role, require_worker_token

log = logging.getLogger("patchguard.inspections")

router = APIRouter(prefix="/api/v1", tags=["inspections"])

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
ACTION_MATCH_RADIUS_M = float(os.environ.get("ACTION_MATCH_RADIUS_M", "30"))

inspector_or_admin = require_role(Role.admin, Role.inspector)


# ---------- Inspection lifecycle ----------

class CreateInspection(BaseModel):
    job_id: str
    start: list[float]          # [lat, lng]
    end: list[float]
    route: list[list[float]] | None = None


class InspectionOut(BaseModel):
    id: str
    status: str
    captured: int
    skipped: int
    started_at: datetime
    finished_at: datetime | None


@router.post("/inspections", response_model=InspectionOut, status_code=201)
async def create_inspection(
    req: CreateInspection,
    user: User = Depends(inspector_or_admin),
    session: AsyncSession = Depends(get_session),
) -> InspectionOut:
    existing = (
        await session.execute(select(Inspection).where(Inspection.id == req.job_id))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(409, "Inspection already exists for this job")
    insp = Inspection(
        id=req.job_id,
        started_by=user.id,
        start_pt=point_wkt(req.start[0], req.start[1]),
        end_pt=point_wkt(req.end[0], req.end[1]),
        route=linestring_wkt(req.route) if req.route and len(req.route) >= 2 else None,
    )
    session.add(insp)
    await session.commit()
    return InspectionOut(
        id=insp.id, status=insp.status.value, captured=insp.captured,
        skipped=insp.skipped, started_at=insp.started_at, finished_at=insp.finished_at,
    )


class FinishInspection(BaseModel):
    status: str = "done"
    captured: int = 0
    skipped: int = 0


@router.patch("/inspections/{inspection_id}", response_model=InspectionOut)
async def finish_inspection(
    inspection_id: str,
    req: FinishInspection,
    _: User = Depends(inspector_or_admin),
    session: AsyncSession = Depends(get_session),
) -> InspectionOut:
    insp = (
        await session.execute(select(Inspection).where(Inspection.id == inspection_id))
    ).scalar_one_or_none()
    if insp is None:
        raise HTTPException(404, "Inspection not found")
    insp.status = InspectionStatus(req.status)
    insp.captured = req.captured
    insp.skipped = req.skipped
    insp.finished_at = datetime.now(timezone.utc)
    await session.commit()
    return InspectionOut(
        id=insp.id, status=insp.status.value, captured=insp.captured,
        skipped=insp.skipped, started_at=insp.started_at, finished_at=insp.finished_at,
    )


# ---------- Worker batch upload (token-authed, not JWT) ----------

@router.post("/images/batch")
async def upload_batch(
    request: Request,
    files: list[UploadFile] = File(...),
    items_json: str = Form(...),
    job_id: str | None = Form(default=None),
    _: None = Depends(require_worker_token),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from main import APP  # late import to reach the loaded YOLO model
    from model import vision_caption

    items = json.loads(items_json)
    if len(items) != len(files):
        raise HTTPException(400, f"files ({len(files)}) and items ({len(items)}) must match")

    from storage import upload_annotated

    raw_dir = DATA_DIR / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    base = str(request.base_url).rstrip("/")
    out: list[dict] = []

    for file, meta in zip(files, items):
        if meta.get("latitude") is None or meta.get("longitude") is None:
            log.warning("dropping %s: no gps", meta.get("filename"))
            continue
        jpeg = await file.read()
        image_id = uuid.uuid4().hex
        (raw_dir / f"{image_id}.jpg").write_bytes(jpeg)

        try:
            detections, annotated = APP.model.infer(jpeg)
        except Exception as e:
            log.exception("inference failed for %s", meta.get("filename"))
            raise HTTPException(500, f"inference failed: {e}") from e
        annotated_path = upload_annotated(annotated, image_id)

        caption = vision_caption(annotated) if detections else None
        if caption:
            log.info("vision: %s", caption)

        lat = float(meta["latitude"])
        lng = float(meta["longitude"])
        img = Image(
            id=image_id,
            inspection_id=job_id,
            pt=point_wkt(lat, lng),
            lat=lat,
            lng=lng,
            heading=meta.get("heading"),
            captured_at=meta["captured_at"],
            raw_path=str(raw_dir / f"{image_id}.jpg"),
            annotated_path=annotated_path,
            vision_description=caption,
            source=Source.worker,
            analyzed=True,
        )
        session.add(img)

        damage_rows: list[Damage] = []
        for d in detections:
            row = Damage(
                image_id=image_id,
                damage_class=d.damage_class,
                confidence=d.confidence,
                bbox_x1=d.bbox_x1, bbox_y1=d.bbox_y1, bbox_x2=d.bbox_x2, bbox_y2=d.bbox_y2,
                model_version=APP.model.model_version,
            )
            session.add(row)
            damage_rows.append(row)
        await session.flush()

        # Guarantee matching — only when damage was actually found.
        actions_raised = 0
        if damage_rows:
            actions_raised = await _match_guarantees(session, img, damage_rows[0])

        out.append({
            "image_id": image_id,
            "annotated_image_url": f"{base}/api/v1/images/{image_id}/annotated",
            "damages": len(damage_rows),
            "vision_description": caption,
            "latitude": lat,
            "longitude": lng,
            "actions_raised": actions_raised,
        })

    await session.commit()
    return {"ok": True, "ingested": len(out), "items": out}


async def _match_guarantees(session: AsyncSession, img: Image, first_damage: Damage) -> int:
    """Raise an Action for each work record whose guarantee is live and whose path passes
    within ACTION_MATCH_RADIUS_M of the damage point. Deduped by (image, work_record)."""
    damage_pt = ST_GeogFromText(point_wkt(img.lat, img.lng))
    matches = (
        await session.execute(
            select(
                WorkRecord.id,
                WorkRecord.contractor_id,
                ST_Distance(WorkRecord.path, damage_pt).label("distance_m"),
            )
            .where(WorkRecord.path.isnot(None))
            .where(WorkRecord.guarantee_expires >= datetime.now(timezone.utc).date())
            .where(ST_DWithin(WorkRecord.path, damage_pt, ACTION_MATCH_RADIUS_M))
        )
    ).all()
    raised = 0
    for wr_id, contractor_id, distance_m in matches:
        exists = (
            await session.execute(
                select(Action.id)
                .where(Action.image_id == img.id)
                .where(Action.work_record_id == wr_id)
            )
        ).scalar_one_or_none()
        if exists:
            continue
        session.add(
            Action(
                image_id=img.id,
                damage_id=first_damage.id,
                work_record_id=wr_id,
                contractor_id=contractor_id,
                distance_m=round(float(distance_m), 1),
            )
        )
        raised += 1
    if raised:
        log.info("raised %d action(s) for image %s", raised, img.id)
    return raised


# ---------- Map queries (JWT-authed) ----------

@router.get("/images/damage-report")
async def damage_report(
    request: Request,
    lon_min: float,
    lat_min: float,
    lon_max: float,
    lat_max: float,
    source: str | None = None,
    inspection_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    _: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    q = (
        select(Image)
        .options(selectinload(Image.damages))
        .where(Image.lng >= lon_min, Image.lng <= lon_max)
        .where(Image.lat >= lat_min, Image.lat <= lat_max)
    )
    if source is not None:
        q = q.where(Image.source == source)
    if inspection_id is not None:
        q = q.where(Image.inspection_id == inspection_id)
    if date_from is not None:
        q = q.where(Image.captured_at >= date_from)
    if date_to is not None:
        q = q.where(Image.captured_at <= date_to + "T23:59:59")
    images = (await session.execute(q)).scalars().all()
    base = str(request.base_url).rstrip("/")
    return [
        {
            "image_id": img.id,
            "annotated_image_url": f"{base}/api/v1/images/{img.id}/annotated",
            "latitude": img.lat,
            "longitude": img.lng,
            "captured_at": img.captured_at,
            "vision_description": img.vision_description,
            "damages": [
                {
                    "id": d.id,
                    "image_id": d.image_id,
                    "damage_class": d.damage_class,
                    "confidence": d.confidence,
                    "bbox_x1": d.bbox_x1, "bbox_y1": d.bbox_y1,
                    "bbox_x2": d.bbox_x2, "bbox_y2": d.bbox_y2,
                    "model_version": d.model_version,
                }
                for d in img.damages
            ],
        }
        for img in images
    ]


@router.get("/images/{image_id}/annotated")
async def annotated(image_id: str, session: AsyncSession = Depends(get_session)):
    img = (await session.execute(select(Image).where(Image.id == image_id))).scalar_one_or_none()
    if img is None or not img.annotated_path:
        raise HTTPException(404, "image not found")
    if img.annotated_path.startswith("http"):
        return RedirectResponse(img.annotated_path)
    if not Path(img.annotated_path).exists():
        raise HTTPException(404, "image not found")
    return FileResponse(img.annotated_path, media_type="image/jpeg")


# ---------- Report generation ----------

@router.post("/inspections/{inspection_id}/report")
async def generate_report(
    inspection_id: str,
    user: User = Depends(inspector_or_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from reporting.generator import generate_inspection_report

    insp = (
        await session.execute(select(Inspection).where(Inspection.id == inspection_id))
    ).scalar_one_or_none()
    if insp is None:
        raise HTTPException(404, "Inspection not found")
    report = await generate_inspection_report(session, insp, user)
    return {
        "report_id": report.id,
        "is_mock": report.is_mock,
        "model": report.model,
        "content_md": report.content_md,
        "created_at": report.created_at.isoformat(),
    }
