"""Annotated image storage — DO Spaces when configured, local disk fallback for dev.

Set SPACES_KEY + SPACES_BUCKET to enable Spaces. Without those vars the module
writes files under DATA_DIR/annotated/ exactly as before, so local dev needs no changes.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("patchguard.storage")

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()


def _spaces_enabled() -> bool:
    return bool(os.environ.get("SPACES_KEY") and os.environ.get("SPACES_BUCKET"))


def _client():
    import boto3
    region = os.environ.get("SPACES_REGION", "syd1")
    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=f"https://{region}.digitaloceanspaces.com",
        aws_access_key_id=os.environ["SPACES_KEY"],
        aws_secret_access_key=os.environ["SPACES_SECRET"],
    )


def upload_annotated(image_bytes: bytes, image_id: str) -> str:
    """Store annotated JPEG. Returns a Spaces URL or a local filesystem path.

    Store the return value in Image.annotated_path. The /images/{id}/annotated
    endpoint detects which form it is and either redirects or serves the file.
    """
    if _spaces_enabled():
        bucket = os.environ["SPACES_BUCKET"]
        region = os.environ.get("SPACES_REGION", "syd1")
        key = f"annotated/{image_id}.jpg"
        _client().put_object(
            Bucket=bucket,
            Key=key,
            Body=image_bytes,
            ContentType="image/jpeg",
            ACL="public-read",
        )
        cdn = os.environ.get("SPACES_CDN_ENDPOINT", "").rstrip("/")
        if cdn:
            return f"{cdn}/{key}"
        return f"https://{bucket}.{region}.digitaloceanspaces.com/{key}"

    annotated_dir = DATA_DIR / "annotated"
    annotated_dir.mkdir(parents=True, exist_ok=True)
    path = annotated_dir / f"{image_id}.jpg"
    path.write_bytes(image_bytes)
    return str(path)
