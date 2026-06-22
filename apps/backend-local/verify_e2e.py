"""End-to-end verification of the ERP backend against a running instance on :8000.

Covers: login, roles/authz, users CRUD, contractors + work records, worker upload
with guarantee matching (uses a real RDD2022 image at a point ON the seeded
Abercrombie St path), actions autopopulation, damage-report query, mock report
generation + guardrail sections.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE = "http://localhost:8000"
WORKER_TOKEN = os.environ["WORKER_TOKEN"]
SAMPLE_IMG = os.environ.get(
    "SAMPLE_IMG",
    str(pathlib.Path(__file__).parent.parent.parent / "yolov5" / "data" / "images" / "zidane.jpg"),
)
# A point on the seeded Abercrombie St work path (within 30 m).
ON_PATH = (-33.89071, 151.19833)
# A point far away (Bondi) — must NOT raise an action.
OFF_PATH = (-33.8915, 151.2767)

passed = 0
failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def main() -> None:
    c = httpx.Client(base_url=BASE, timeout=120)

    # --- auth ---
    r = c.post("/api/v1/auth/login", json={"email": "admin@patchguard.local", "password": "admin123"})
    check("admin login", r.status_code == 200, r.text[:200])
    admin_tok = r.json()["token"]
    admin = {"Authorization": f"Bearer {admin_tok}"}

    r = c.post("/api/v1/auth/login", json={"email": "admin@patchguard.local", "password": "wrong"})
    check("wrong password rejected", r.status_code == 401)

    r = c.get("/api/v1/users")
    check("no token -> 401", r.status_code == 401)

    r = c.post("/api/v1/auth/login", json={"email": "viewer@patchguard.local", "password": "viewer123"})
    viewer = {"Authorization": f"Bearer {r.json()['token']}"}
    r = c.get("/api/v1/users", headers=viewer)
    check("viewer cannot list users (403)", r.status_code == 403)

    # --- users CRUD ---
    r = c.post("/api/v1/users", headers=admin, json={
        "email": "test.inspector@patchguard.local", "full_name": "Test Inspector",
        "role": "inspector", "password": "testpass123",
    })
    check("admin creates user", r.status_code in (201, 409), r.text[:200])

    r = c.post("/api/v1/auth/login", json={"email": "test.inspector@patchguard.local", "password": "testpass123"})
    check("new user can log in", r.status_code == 200, r.text[:200])
    inspector = {"Authorization": f"Bearer {r.json()['token']}"}

    # --- contractors ---
    r = c.get("/api/v1/contractors", headers=viewer)
    check("viewer can read contractors", r.status_code == 200)
    contractors = r.json()
    check("seeded contractor present", any(x["name"] == "Acme Roads Pty Ltd" for x in contractors))
    acme = next(x for x in contractors if x["name"] == "Acme Roads Pty Ltd")

    r = c.get(f"/api/v1/contractors/{acme['id']}/work-records", headers=viewer)
    records = r.json()
    check("seeded work record with path", len(records) == 1 and records[0]["path"] is not None,
          json.dumps(records)[:200])
    check("guarantee expiry computed", records[0]["guarantee_months"] == 24)

    r = c.post("/api/v1/contractors", headers=viewer, json={"name": "Nope Pty Ltd"})
    check("viewer cannot create contractor (403)", r.status_code == 403)

    # --- inspection + worker upload (ON the guaranteed path) ---
    job_id = "e2e" + os.urandom(8).hex()[:13]
    r = c.post("/api/v1/inspections", headers=inspector, json={
        "job_id": job_id, "start": list(ON_PATH), "end": [ON_PATH[0] + 0.002, ON_PATH[1] + 0.001],
    })
    check("inspector creates inspection", r.status_code == 201, r.text[:200])

    jpeg = open(SAMPLE_IMG, "rb").read()
    items = [{
        "filename": "e2e_on_path.jpg", "latitude": ON_PATH[0], "longitude": ON_PATH[1],
        "captured_at": "2026-06-11T10:00:00Z", "heading": 90, "altitude": None, "gps_accuracy": 1.0,
    }, {
        "filename": "e2e_off_path.jpg", "latitude": OFF_PATH[0], "longitude": OFF_PATH[1],
        "captured_at": "2026-06-11T10:00:05Z", "heading": 90, "altitude": None, "gps_accuracy": 1.0,
    }]
    r = c.post(
        "/api/v1/images/batch",
        files=[("files", ("e2e_on_path.jpg", jpeg, "image/jpeg")),
               ("files", ("e2e_off_path.jpg", jpeg, "image/jpeg"))],
        data={"items_json": json.dumps(items), "job_id": job_id},
        headers={"X-Worker-Token": WORKER_TOKEN},
    )
    check("worker batch upload", r.status_code == 200, r.text[:300])
    batch = r.json()
    on_path_item = batch["items"][0]
    check("YOLOv5 detected damage", on_path_item["damages"] > 0, json.dumps(batch)[:200])
    check("action raised for on-path damage", on_path_item.get("actions_raised", 0) >= 1,
          json.dumps(on_path_item))
    check("NO action for off-path damage", batch["items"][1].get("actions_raised", 0) == 0,
          json.dumps(batch["items"][1]))

    r = c.post("/api/v1/images/batch", files=[("files", ("x.jpg", jpeg, "image/jpeg"))],
               data={"items_json": json.dumps(items[:1])},
               headers={"X-Worker-Token": "wrong-token"})
    check("wrong worker token -> 401", r.status_code == 401)

    # --- actions ---
    r = c.get("/api/v1/actions", headers=viewer)
    check("actions listed", r.status_code == 200)
    actions = r.json()
    match = [a for a in actions if a["contractor_name"] == "Acme Roads Pty Ltd"]
    check("action autopopulated with contractor + guarantee",
          len(match) >= 1 and match[0]["guarantee_expires"] > "2026" and match[0]["distance_m"] < 30,
          json.dumps(match[:1]))

    if match:
        r = c.patch(f"/api/v1/actions/{match[0]['id']}", headers=viewer, json={"status": "resolved"})
        check("viewer cannot update action (403)", r.status_code == 403)
        r = c.patch(f"/api/v1/actions/{match[0]['id']}", headers=inspector,
                    json={"status": "notified"})
        check("inspector updates action status", r.status_code == 200, r.text[:200])

    # --- damage-report (map) ---
    r = c.get("/api/v1/images/damage-report", headers=viewer, params={
        "lon_min": 151.19, "lat_min": -33.90, "lon_max": 151.21, "lat_max": -33.88,
    })
    check("damage-report bbox query", r.status_code == 200 and len(r.json()) >= 1,
          f"status={r.status_code} n={len(r.json()) if r.status_code == 200 else '?'}")

    # --- report generation (mock mode) ---
    r = c.post(f"/api/v1/inspections/{job_id}/report", headers=inspector)
    check("report generated", r.status_code == 200, r.text[:200])
    rep = r.json()
    check("report is mock mode", rep["is_mock"] is True)
    for section in ["Executive Summary", "Survey Details", "Findings by Severity",
                    "Guarantee Implications", "Recommended Actions"]:
        check(f"report has section: {section}", f"## {section}" in rep["content_md"])
    check("report mentions guarantee match", "Acme Roads" in rep["content_md"],
          rep["content_md"][:300])

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
