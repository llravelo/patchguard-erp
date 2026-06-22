"""PatchGuard ERP backend.

Single FastAPI service owning: auth (JWT + roles), users, contractors + work records,
inspections (worker uploads → YOLOv5 + Vision → PostGIS guarantee matching), actions,
and report generation. Files (raw/annotated images, invoices) live on disk under
DATA_DIR; the index lives in PostgreSQL/PostGIS.
"""
from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from model import YoloV5Damage, load_from_env

log = logging.getLogger("patchguard.backend")
logging.basicConfig(level=logging.INFO)


class App:
    model: YoloV5Damage


APP = App()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("loading YOLOv5 weights…")
    APP.model = load_from_env()
    log.info("model ready: %s", APP.model.model_version)
    yield


app = FastAPI(title="patchguard-erp", lifespan=lifespan)
_raw_origins = os.environ.get("ALLOWED_ORIGINS", "")
_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()] or ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers import APP from this module — import after APP is defined.
from routers import actions, auth, contractors, field_captures, inspections, users  # noqa: E402

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(contractors.router)
app.include_router(inspections.router)
app.include_router(field_captures.router)
app.include_router(actions.router)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "model": APP.model.model_version if hasattr(APP, "model") else None}
