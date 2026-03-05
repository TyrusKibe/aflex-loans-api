from __future__ import annotations

import secrets
from datetime import UTC, datetime

import cloudinary
import cloudinary.uploader

from .config import settings


def _configure_cloudinary() -> None:
    cloud_name = (settings.cloudinary_cloud_name or "").strip()
    api_key = (settings.cloudinary_api_key or "").strip()
    api_secret = (settings.cloudinary_api_secret or "").strip()
    if not cloud_name or not api_key or not api_secret:
        raise RuntimeError("Cloudinary credentials are missing")
    cloudinary.config(
        cloud_name=cloud_name,
        api_key=api_key,
        api_secret=api_secret,
        secure=True,
    )


def upload_photo_bytes(*, file_bytes: bytes, filename: str, folder: str = "aflex-loans") -> dict[str, str]:
    provider = (settings.upload_provider or "cloudinary").strip().lower()
    if provider != "cloudinary":
        raise RuntimeError(f"Unsupported upload provider: {provider}")

    _configure_cloudinary()
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    public_id = f"{folder}/{stamp}-{secrets.token_hex(6)}"
    result = cloudinary.uploader.upload(
        file_bytes,
        resource_type="image",
        folder=folder,
        public_id=public_id,
        use_filename=True,
        unique_filename=True,
        overwrite=False,
        filename_override=filename,
    )
    url = str(result.get("secure_url") or "").strip()
    if not url:
        raise RuntimeError("Cloudinary upload did not return a secure URL")
    return {
        "url": url,
        "provider": "cloudinary",
        "public_id": str(result.get("public_id") or ""),
    }
