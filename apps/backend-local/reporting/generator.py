"""Inspection report generation: Claude (with prompt caching) or deterministic mock.

REPORT_MODE=mock   → template report from real DB data, no API call (default)
REPORT_MODE=claude → Anthropic Messages API; falls back to the template if the
                     output fails validation twice or the API errors.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models_db import (
    Action,
    Contractor,
    Image,
    Inspection,
    Report,
    User,
    WorkRecord,
)
from reporting.prompts import REQUIRED_SECTIONS, SYSTEM_PROMPT, build_user_prompt

log = logging.getLogger("patchguard.reports")

SEVERITY_ORDER = [
    "Pothole",
    "alligator crack",
    "transverse crack",
    "longitudinal crack",
    "other corruption",
]

MAX_WORDS = 1200  # hard cap enforced by the validator (prompt asks for 800)


# ---------- Context assembly (input guardrail: whitelist only) ----------

async def _build_context(session: AsyncSession, insp: Inspection) -> dict[str, Any]:
    images = (
        await session.execute(
            select(Image)
            .options(selectinload(Image.damages))
            .where(Image.inspection_id == insp.id)
        )
    ).scalars().all()

    by_class: dict[str, list[float]] = defaultdict(list)
    damage_locations: list[dict[str, Any]] = []
    captions: list[str] = []
    for img in images:
        if img.vision_description:
            captions.append(img.vision_description)
        for d in img.damages:
            by_class[d.damage_class].append(d.confidence)
            damage_locations.append({
                "class": d.damage_class,
                "confidence": round(d.confidence, 2),
                "lat": round(img.lat, 5),
                "lng": round(img.lng, 5),
            })

    actions = (
        await session.execute(
            select(Action, Contractor, WorkRecord)
            .join(Contractor, Action.contractor_id == Contractor.id)
            .join(WorkRecord, Action.work_record_id == WorkRecord.id)
            .join(Image, Action.image_id == Image.id)
            .where(Image.inspection_id == insp.id)
        )
    ).all()

    return {
        "inspection_id": insp.id,
        "status": insp.status.value,
        "started_at": insp.started_at.isoformat() if insp.started_at else None,
        "finished_at": insp.finished_at.isoformat() if insp.finished_at else None,
        "images_captured": len(images),
        "images_with_damage": sum(1 for i in images if i.damages),
        "damages_by_class": {
            cls: {
                "count": len(confs),
                "mean_confidence": round(sum(confs) / len(confs), 2),
                "max_confidence": round(max(confs), 2),
            }
            for cls, confs in by_class.items()
        },
        "damage_locations": damage_locations[:50],  # cap context size
        "vision_captions": captions[:25],
        "guarantee_matches": [
            {
                "contractor": c.name,
                "work_title": wr.title,
                "work_date": wr.work_date.isoformat(),
                "guarantee_expires": wr.guarantee_expires.isoformat(),
                "distance_m": float(a.distance_m),
                "action_status": a.status.value,
            }
            for a, c, wr in actions
        ],
    }


# ---------- Output guardrail ----------

_COORD_RE = re.compile(r"(-?\d{1,3}\.\d{3,}),\s*(-?\d{1,3}\.\d{3,})")


def validate_report(md: str, context: dict[str, Any]) -> str | None:
    """Return None if valid, else a description of the violation."""
    for section in REQUIRED_SECTIONS:
        if f"## {section}" not in md:
            return f"missing required section heading '## {section}'"
    if len(md.split()) > MAX_WORDS:
        return f"report exceeds {MAX_WORDS} words"
    # Every coordinate pair mentioned must exist in the input (rounded to 3+ dp match).
    allowed = {
        (round(loc["lat"], 3), round(loc["lng"], 3)) for loc in context["damage_locations"]
    }
    for m in _COORD_RE.finditer(md):
        pair = (round(float(m.group(1)), 3), round(float(m.group(2)), 3))
        if allowed and pair not in allowed:
            return f"coordinate {m.group(0)} not present in survey data"
    return None


# ---------- Mock / fallback template ----------

def render_template_report(context: dict[str, Any]) -> str:
    dmg = context["damages_by_class"]
    total = sum(v["count"] for v in dmg.values())
    by_severity = [
        f"- **{cls}** — {dmg[cls]['count']} detection(s), "
        f"mean confidence {dmg[cls]['mean_confidence']:.0%}, "
        f"max {dmg[cls]['max_confidence']:.0%}"
        for cls in SEVERITY_ORDER if cls in dmg
    ] or ["- No damage detected on this survey."]

    matches = context["guarantee_matches"]
    if matches:
        guarantee_lines = [
            f"- **{m['contractor']}** — \"{m['work_title']}\" (completed {m['work_date']}, "
            f"guarantee until {m['guarantee_expires']}). Damage found "
            f"{m['distance_m']:.0f} m from the work path. Action status: {m['action_status']}."
            for m in matches
        ]
    else:
        guarantee_lines = ["- No damage was found within any active guarantee zone."]

    recommendations: list[str] = []
    if any(cls in dmg for cls in ("Pothole", "alligator crack")):
        recommendations.append(
            "- Schedule priority repair assessment for pothole / alligator-crack sites."
        )
    if matches:
        recommendations.append(
            "- Notify the listed contractor(s) of damage within active guarantee zones."
        )
    if total > 0 and not recommendations:
        recommendations.append("- Monitor identified crack sites at the next survey cycle.")
    if total == 0:
        recommendations.append("- No action required; road sections surveyed are in good condition.")

    return f"""## Executive Summary
Survey **{context['inspection_id'][:8]}** captured {context['images_captured']} images, of which {context['images_with_damage']} showed road damage ({total} individual detection(s)). {len(matches)} detection(s) fell within an active contractor guarantee zone.

## Survey Details
- Inspection ID: {context['inspection_id']}
- Status: {context['status']}
- Started: {context['started_at'] or 'not recorded'}
- Finished: {context['finished_at'] or 'not recorded'}
- Images captured: {context['images_captured']}

## Findings by Severity
{chr(10).join(by_severity)}

## Guarantee Implications
{chr(10).join(guarantee_lines)}

## Recommended Actions
{chr(10).join(recommendations)}
"""


# ---------- Claude mode ----------

def _claude_generate(context_json: str) -> str:
    import anthropic

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    model = os.environ.get("REPORT_MODEL", "claude-haiku-4-5")
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                # Prefix caching: the system prompt is byte-stable across reports, so
                # repeated report generations within the TTL read it from cache.
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": build_user_prompt(context_json)}],
    )
    return next((b.text for b in response.content if b.type == "text"), "")


# ---------- Entry point ----------

async def generate_inspection_report(
    session: AsyncSession, insp: Inspection, user: User
) -> Report:
    context = await _build_context(session, insp)
    context_json = json.dumps(context, indent=2, sort_keys=True)

    mode = os.environ.get("REPORT_MODE", "mock").lower()
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    use_claude = mode == "claude" and has_key
    if mode == "claude" and not has_key:
        log.warning("REPORT_MODE=claude but ANTHROPIC_API_KEY missing — using mock template")

    content: str | None = None
    model_used = "template-v1"
    is_mock = True

    if use_claude:
        model_used = os.environ.get("REPORT_MODEL", "claude-haiku-4-5")
        for attempt in range(2):
            try:
                candidate = _claude_generate(context_json)
            except Exception as e:
                log.warning("claude report generation failed (attempt %d): %s", attempt + 1, e)
                break
            violation = validate_report(candidate, context)
            if violation is None:
                content = candidate
                is_mock = False
                break
            log.warning("report validation failed (attempt %d): %s", attempt + 1, violation)

    if content is None:
        content = render_template_report(context)
        if use_claude:
            model_used = "template-v1 (claude fallback)"

    report = Report(
        inspection_id=insp.id,
        generated_by=user.id,
        model=model_used,
        is_mock=is_mock,
        content_md=content,
    )
    session.add(report)
    await session.commit()
    return report
