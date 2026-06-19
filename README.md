# PatchGuard ERP

A road-maintenance ERP with an **autonomous inspection agent**. Instead of driving a
phone-mounted camera down every street, an agent routes between two map clicks, captures
Google Street View imagery along the way, runs a **YOLOv5 road-damage model** on each frame,
narrates findings with **Gemini Vision**, and — when damage lands on a road still under a
contractor's guarantee — raises an action automatically via **PostGIS** geo-matching.

> Built on top of a YOLOv5 RDD2022 model (5 damage classes: longitudinal / transverse /
> alligator cracks, potholes, surface corruption).
 
---

## Features

- **Role-based access** — `admin` / `inspector` / `viewer`, JWT-authenticated.
- **Users & Accounts** — admin user management (create, edit, deactivate, reset password).
- **Contractors** — work records with date, cost, hours, guarantee period and **invoice PDF**,
  plus the exact **work path drawn on a map**.
- **Inspection** — click a start + end point → the agent routes the road (OSRM), captures
  Street View every ~20 m, detects damage (YOLOv5) and captions it (Gemini Vision). The
  dashboard streams progress live: a moving agent marker, a growing trail, the agent's
  current view, and damage pins as detections land.
- **AI inspection reports** — one click produces a structured Markdown report (Claude API
  with guardrails, or a deterministic template in mock mode).
- **Actions** — when detected damage falls within 30 m of a guaranteed work path
  (`ST_DWithin`), an action is raised automatically with the contractor, work record,
  guarantee expiry and distance pre-filled.

---

## Architecture

```
┌──────────────────────────────┐
│  apps/web  (React, :5173)     │  Login · Users · Contractors · Inspection · Actions
└───────────────┬──────────────┘
                │ JWT
   ┌────────────┴───────────────┐
   ▼                            ▼
┌──────────────────────┐   ┌──────────────────────────┐
│ ERP backend (:8000)  │   │ Agent control plane(:8765)│
│ FastAPI              │   │ OSRM routing · job queue  │
│ • auth / users       │   │ · WebSocket events        │
│ • contractors+paths  │   └────────────┬──────────────┘
│ • YOLOv5 + Vision    │                │ long-poll
│ • PostGIS matching   │   ┌────────────▼──────────────┐
│ • Claude reports     │◄──│ Capture worker (Node)      │
└──────────┬───────────┘   │ Google Street View Static  │
           ▼               │ (satellite fallback)       │
┌──────────────────────┐   └────────────────────────────┘
│ PostgreSQL + PostGIS │
└──────────────────────┘
```

**Tech stack:** React + Vite + TypeScript + Leaflet · FastAPI + SQLAlchemy 2 (async) +
GeoAlchemy2 · PostgreSQL 16 + PostGIS · YOLOv5 (PyTorch) · Google Gemini Vision ·
Google Street View Static API · OSRM · Anthropic Claude API · Node 22 capture worker.

---

## Project structure

```
apps/
  web/             React ERP frontend
    src/auth/      AuthContext, login, route guards
    src/layout/    AppShell (role-gated 4-tab nav)
    src/pages/     UsersPage, ContractorsPage, InspectionPage, ActionsPage, DashboardPage
    src/components/ PointPicker, AgentView, WorkPathEditor, ReportPanel
  backend-local/   FastAPI ERP backend
    models_db.py   SQLAlchemy + PostGIS models
    security.py    bcrypt + JWT + role dependencies
    routers/       auth, users, contractors, inspections, actions
    reporting/     Claude report generator + prompt/guardrails (mock fallback)
    model.py       YOLOv5 inference + Gemini Vision caption
    init_db.py     create tables + seed users/contractor/work record
  agent/           Inspection control plane (FastAPI) + routing (OSRM)
    worker/        Node capture worker (Street View / satellite sources)
```

---

## Prerequisites

- **PostgreSQL 16 + PostGIS** (or Docker — see below)
- **Python 3.11+** with a venv per backend (`apps/backend-local`, `apps/agent`)
- **Node 22+** for `apps/web` and `apps/agent/worker`
- **Google Maps API key** with the Street View Static API enabled (capture worker)
- *(optional)* **Gemini API key** for Vision captions, **Anthropic API key** for live reports

Configuration is per-service via `.env` files (copy each `.env.example`). No secrets are
committed.

---

## Setup & run

### 1. Database

```bash
# Native PostgreSQL + PostGIS (Debian/Ubuntu/WSL):
sudo apt install -y postgresql-16 postgresql-16-postgis-3
sudo service postgresql start
sudo -u postgres psql -c "CREATE USER patchguard WITH PASSWORD 'patchguard' SUPERUSER;"
sudo -u postgres psql -c "CREATE DATABASE patchguard OWNER patchguard;"

# …or with Docker:
docker compose up -d db
```

### 2. Create tables + seed

```bash
cd apps/backend-local
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # set MODEL_WEIGHTS / YOLOV5_DIR; DATABASE_URL defaults to localhost
python init_db.py
```

Seeds three logins and a demo contractor:

| Login | Password | Access |
| --- | --- | --- |
| `admin@patchguard.local` | `admin123` | all four tabs |
| `inspector@patchguard.local` | `inspect123` | Contractors (read), Inspection, Actions |
| `viewer@patchguard.local` | `viewer123` | read-only |

### 3. Run the services (four terminals)

```bash
# Terminal 1 — ERP backend
cd apps/backend-local && source .venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8000

# Terminal 2 — agent control plane
cd apps/agent && source .venv/bin/activate && pip install -e .
cp .env.example .env          # set GOOGLE_MAPS_API_KEY, WORKER_TOKEN (match backend)
uvicorn api.main:app --host 0.0.0.0 --port 8765

# Terminal 3 — capture worker
cd apps/agent/worker && npm install && npm run dev

# Terminal 4 — frontend
cd apps/web && npm install
cp .env.example .env          # VITE_API_BASE=http://localhost:8000  VITE_AGENT_BASE=http://localhost:8765
npm run dev
```

Open **http://localhost:5173** and log in as admin.

---

## Demo: automatic guarantee actions

1. Log in as `admin@patchguard.local`.
2. **Contractors** → "Acme Roads Pty Ltd" has a seeded work record along **Abercrombie St,
   Darlington** with a 24-month guarantee and a drawn work path.
3. **Inspection** → click a start and end point **along Abercrombie St** → *Start survey*.
4. The agent captures Street View frames; where YOLOv5 detects damage within 30 m of the
   guaranteed path, the **Actions** tab badge increments.
5. **Actions** → the row is fully auto-populated (contractor, work date, guarantee expiry,
   damage class, distance). Click *View* for the annotated image + Vision caption.
6. Back in **Inspection** → *Generate report* → a structured Markdown inspection report.

---

## AI report generation

`apps/backend-local/.env`:

```
REPORT_MODE=mock              # deterministic template from real survey data (default)
# REPORT_MODE=claude          # Anthropic Claude API (Messages, cached system prompt)
# ANTHROPIC_API_KEY=sk-ant-...
# REPORT_MODEL=claude-haiku-4-5
```

Guardrails (all modes): whitelisted-field context only, untrusted-text delimiting for
externally-generated vision captions, and output validation (required sections, length cap,
no hallucinated coordinates) with one retry then a template fallback.

---

## Capture sources

The worker abstracts the image source behind a `CaptureSource` interface
(`apps/agent/worker/src`):

| `CAPTURE_SOURCE` | Source | Notes |
| --- | --- | --- |
| `streetview` *(default)* | Google Street View Static API | Pitched down at the road surface; metadata + freshness checks; falls back to satellite where there's no coverage |
| `satellite` | Esri World Imagery tiles | Keyless top-down imagery |
| `earth` | Puppeteer + earth.google.com | Requires working WebGL |

---

## Services & ports

| Service | Port | Auth |
| --- | --- | --- |
| PostgreSQL + PostGIS | 5432 | `patchguard` / `patchguard` |
| ERP backend | 8000 | JWT (browser) + `X-Worker-Token` (worker uploads) |
| Agent control plane | 8765 | localhost only |
| Frontend (Vite) | 5173 | — |

---

## Notes

- This is a final-project / portfolio build. Default credentials and the demo seed are for
  local use — change them before any non-local deployment.
- API keys live only in gitignored `.env` files; rotate any key that has been shared.
