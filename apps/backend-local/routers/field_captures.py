"""Mobile field capture endpoints.

  POST  /api/v1/mobile/frames       upload a batch of frames (JWT, store only)
  POST  /api/v1/analysis/trigger    start background inference on pending images
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import date
from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db import SessionLocal, get_session
from geo import point_wkt
from models_db import Action, Damage, Image, Role, Source, User, WorkRecord
from security import require_role

log = logging.getLogger("patchguard.mobile")

router = APIRouter(prefix="/api/v1", tags=["mobile"])

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()

inspector_or_admin = require_role(Role.admin, Role.inspector)


@router.post("/mobile/frames", status_code=201)
async def upload_mobile_frames(
    files: list[UploadFile] = File(...),
    items_json: str = Form(...),
    _: User = Depends(inspector_or_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Store raw frames from the mobile app. No inference — call /analysis/trigger separately."""
    items = json.loads(items_json)
    if len(items) != len(files):
        raise HTTPException(400, f"files ({len(files)}) and items ({len(items)}) must match")

    raw_dir = DATA_DIR / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    out: list[dict] = []
    for file, meta in zip(files, items):
        if meta.get("latitude") is None or meta.get("longitude") is None:
            log.warning("dropping %s: no gps", meta.get("filename"))
            continue
        jpeg = await file.read()
        image_id = uuid.uuid4().hex
        (raw_dir / f"{image_id}.jpg").write_bytes(jpeg)

        lat = float(meta["latitude"])
        lng = float(meta["longitude"])
        session.add(Image(
            id=image_id,
            pt=point_wkt(lat, lng),
            lat=lat,
            lng=lng,
            heading=meta.get("heading"),
            captured_at=meta["captured_at"],
            raw_path=str(raw_dir / f"{image_id}.jpg"),
            source=Source.mobile,
            analyzed=False,
        ))
        out.append({"image_id": image_id, "latitude": lat, "longitude": lng})

    await session.commit()
    return {"ok": True, "ingested": len(out), "items": out}


@router.post("/analysis/trigger", status_code=202)
async def trigger_analysis(
    background_tasks: BackgroundTasks,
    _: User = Depends(inspector_or_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Start background inference on all unanalyzed mobile images. Returns immediately."""
    pending = (
        await session.execute(select(Image).where(Image.analyzed.is_(False)))
    ).scalars().all()
    image_ids = [img.id for img in pending]
    if image_ids:
        background_tasks.add_task(_analyze_pending, image_ids)
    return {"accepted": len(image_ids)}


async def _analyze_pending(image_ids: list[str]) -> None:
    from geoalchemy2.functions import ST_Distance, ST_DWithin, ST_GeogFromText
    from main import APP
    from model import vision_caption

    from storage import upload_annotated

    action_radius = float(os.environ.get("ACTION_MATCH_RADIUS_M", "30"))

    async with SessionLocal() as session:
        for image_id in image_ids:
            img = (
                await session.execute(select(Image).where(Image.id == image_id))
            ).scalar_one_or_none()
            if img is None or img.analyzed or not img.raw_path:
                continue
            img.analyzed = True
            await session.flush()
            try:
                jpeg = Path(img.raw_path).read_bytes()
                detections, annotated_bytes = APP.model.infer(jpeg)
                img.annotated_path = upload_annotated(annotated_bytes, img.id)
                img.vision_description = vision_caption(annotated_bytes) if detections else None

                damage_rows: list[Damage] = []
                for d in detections:
                    row = Damage(
                        image_id=img.id,
                        damage_class=d.damage_class,
                        confidence=d.confidence,
                        bbox_x1=d.bbox_x1, bbox_y1=d.bbox_y1,
                        bbox_x2=d.bbox_x2, bbox_y2=d.bbox_y2,
                        model_version=APP.model.model_version,
                    )
                    session.add(row)
                    damage_rows.append(row)
                await session.flush()

                if damage_rows:
                    damage_pt = ST_GeogFromText(point_wkt(img.lat, img.lng))
                    matches = (await session.execute(
                        select(WorkRecord.id, WorkRecord.contractor_id,
                               ST_Distance(WorkRecord.path, damage_pt).label("distance_m"))
                        .where(WorkRecord.path.isnot(None))
                        .where(WorkRecord.guarantee_expires >= date.today())
                        .where(ST_DWithin(WorkRecord.path, damage_pt, action_radius))
                    )).all()
                    for wr_id, contractor_id, distance_m in matches:
                        exists = (await session.execute(
                            select(Action.id)
                            .where(Action.image_id == img.id)
                            .where(Action.work_record_id == wr_id)
                        )).scalar_one_or_none()
                        if not exists:
                            session.add(Action(
                                image_id=img.id,
                                damage_id=damage_rows[0].id,
                                work_record_id=wr_id,
                                contractor_id=contractor_id,
                                distance_m=round(float(distance_m), 1),
                            ))

                await session.commit()
                log.info("analyzed image %s: %d detections", image_id, len(detections))
            except Exception:
                log.exception("analysis failed for image %s", image_id)
                await session.rollback()
