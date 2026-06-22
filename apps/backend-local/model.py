"""YOLOv5 inference wrapper — loads the PatchGuard weights and renders annotated JPEGs.

The dashboard's DamageClass enum uses these labels:
    'longitudinal crack' | 'transverse crack' | 'alligator crack' | 'other corruption' | 'Pothole'

Our training data uses RDD2022 codes (D00/D10/D20/D40/D43). We map them at the boundary so
the dashboard categories stay stable regardless of what the model was trained on.
"""
from __future__ import annotations

import io
import os
import pathlib
import platform
from dataclasses import dataclass
from pathlib import Path

import torch
from PIL import Image, ImageDraw, ImageFont

# best.pt was trained on Windows and its pickle contains WindowsPath objects.
# Loading on Linux/WSL requires aliasing them to PosixPath BEFORE torch.load runs.
if platform.system() != "Windows":
    pathlib.WindowsPath = pathlib.PosixPath  # type: ignore[attr-defined,misc]

# Map RDD2022 internal class IDs (the order in rdd2022_data.yaml) → dashboard labels.
RDD_TO_DASHBOARD = {
    "D00-Longitudinal_Crack": "longitudinal crack",
    "D10-Transverse_Crack": "transverse crack",
    "D20-Alligator_Crack": "alligator crack",
    "D40-Pothole": "Pothole",
    "D43-Crosswalk_Blur": "other corruption",
}

# Colors per damage class for the annotated overlay (RGB).
CLASS_COLORS = {
    "longitudinal crack": (255, 99, 71),
    "transverse crack": (255, 165, 0),
    "alligator crack": (220, 20, 60),
    "Pothole": (138, 43, 226),
    "other corruption": (105, 105, 105),
}


@dataclass
class Detection:
    damage_class: str
    confidence: float
    bbox_x1: int
    bbox_y1: int
    bbox_x2: int
    bbox_y2: int


class YoloV5Damage:
    def __init__(
        self,
        weights_path: str,
        yolov5_dir: str,
        conf: float = 0.25,
        iou: float = 0.45,
        device: str = "cpu",
    ) -> None:
        if not Path(weights_path).is_file():
            raise FileNotFoundError(f"weights not found: {weights_path}")
        if not Path(yolov5_dir).is_dir():
            raise FileNotFoundError(f"yolov5 source dir not found: {yolov5_dir}")

        # torch.hub.load with source='local' uses the local yolov5 repo — no internet required.
        self.model = torch.hub.load(
            yolov5_dir,
            "custom",
            path=weights_path,
            source="local",
            force_reload=False,
        )
        self.model.conf = conf
        self.model.iou = iou
        if device != "cpu":
            self.model.to(device)
        self.device = device
        self.model_version = f"yolov5s-rdd2022-{Path(weights_path).stem}"

    def infer(self, jpeg_bytes: bytes) -> tuple[list[Detection], bytes]:
        """Run inference. Returns (detections, annotated_jpeg_bytes)."""
        img = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
        results = self.model(img, size=640)
        # results.xyxy[0] is a tensor [N, 6] = [x1, y1, x2, y2, conf, cls]
        rows = results.xyxy[0].cpu().numpy()
        # Class names from the loaded model (preserves training order)
        names: dict[int, str] = self.model.names  # type: ignore[assignment]

        detections: list[Detection] = []
        for x1, y1, x2, y2, conf, cls_id in rows:
            raw = names[int(cls_id)]
            label = RDD_TO_DASHBOARD.get(raw, "other corruption")
            detections.append(
                Detection(
                    damage_class=label,
                    confidence=float(conf),
                    bbox_x1=int(x1),
                    bbox_y1=int(y1),
                    bbox_x2=int(x2),
                    bbox_y2=int(y2),
                )
            )
        annotated = self._draw(img, detections)
        return detections, annotated

    @staticmethod
    def _draw(img: Image.Image, detections: list[Detection]) -> bytes:
        out = img.copy()
        draw = ImageDraw.Draw(out)
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", 14)
        except OSError:
            font = ImageFont.load_default()
        for d in detections:
            color = CLASS_COLORS.get(d.damage_class, (200, 200, 200))
            draw.rectangle(
                [(d.bbox_x1, d.bbox_y1), (d.bbox_x2, d.bbox_y2)],
                outline=color,
                width=3,
            )
            tag = f"{d.damage_class} {d.confidence:.2f}"
            tw, th = draw.textbbox((0, 0), tag, font=font)[2:]
            ty = max(0, d.bbox_y1 - th - 4)
            draw.rectangle([(d.bbox_x1, ty), (d.bbox_x1 + tw + 6, ty + th + 4)], fill=color)
            draw.text((d.bbox_x1 + 3, ty + 2), tag, fill=(255, 255, 255), font=font)
        buf = io.BytesIO()
        out.save(buf, format="JPEG", quality=85)
        return buf.getvalue()


def load_from_env() -> YoloV5Damage:
    weights = os.environ["MODEL_WEIGHTS"]
    yolov5_dir = os.environ["YOLOV5_DIR"]
    conf = float(os.environ.get("CONF_THRESHOLD", "0.25"))
    iou = float(os.environ.get("IOU_THRESHOLD", "0.45"))
    device = os.environ.get("INFER_DEVICE", "cpu")
    return YoloV5Damage(weights, yolov5_dir, conf=conf, iou=iou, device=device)


# === Gemini Vision caption (best-effort) ==================================

_VISION_PROMPT = (
    "Describe the visible road damage in this image in one sentence "
    "(at most 25 words). Mention severity if obvious (minor / moderate / severe) "
    "and location in the frame (centre, left lane, edge). "
    "If you do not see any road damage, reply exactly: 'no damage visible'."
)


def _vision_client():
    """Lazily build the genai client. Returns None if disabled or no key."""
    if os.environ.get("VISION_ENABLED", "true").lower() != "true":
        return None
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    try:
        from google import genai
        return genai.Client(api_key=key)
    except Exception:
        return None


def vision_caption(jpeg_bytes: bytes) -> str | None:
    """Run Gemini Vision against an annotated JPEG. Returns a short caption or None on any error."""
    import logging
    log = logging.getLogger("patchguard.vision")

    client = _vision_client()
    if client is None:
        return None
    model = os.environ.get("VISION_MODEL", "gemini-2.5-flash")
    try:
        from google.genai import types as gtypes
        resp = client.models.generate_content(
            model=model,
            contents=[
                gtypes.Part.from_bytes(data=jpeg_bytes, mime_type="image/jpeg"),
                _VISION_PROMPT,
            ],
        )
        text = (resp.text or "").strip()
        if not text:
            return None
        # Trim quotes / trailing punctuation noise.
        return text.strip(" \n\"'.")
    except Exception as e:
        log.warning("vision_caption failed: %s", e)
        return None
