"""Хранение картинок: оригинал + миниатюра 320px."""
from __future__ import annotations

import io
import uuid
from pathlib import Path

from PIL import Image, ImageOps

from ..config import MEDIA_DIR
from ..security import safe_filename

THUMB_SIZE = (320, 320)
MIN_SIDE = 150  # картинки меньше — иконки и пиксели, не сохраняем при парсинге


class ImageError(Exception):
    pass


def save_image(data: bytes, original_name: str, *, min_side: int = 0) -> dict:
    original_name = safe_filename(original_name, "image")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        raise ImageError(f"Файл «{original_name}» не похож на картинку") from e

    width, height = img.size
    if min(width, height) < min_side:
        raise ImageError("Картинка слишком маленькая")

    fmt = (img.format or "PNG").lower()
    ext = {"jpeg": "jpg", "mpo": "jpg"}.get(fmt, fmt)
    key = uuid.uuid4().hex
    folder = MEDIA_DIR / key[:2]
    folder.mkdir(parents=True, exist_ok=True)

    original = folder / f"{key}.{ext}"
    original.write_bytes(data)  # оригинал сохраняется байт в байт

    thumb = ImageOps.exif_transpose(img)
    thumb.thumbnail(THUMB_SIZE)
    if thumb.mode not in ("RGB", "RGBA"):
        thumb = thumb.convert("RGBA")
    thumb_path = folder / f"{key}_t.webp"
    thumb.save(thumb_path, "WEBP", quality=82)

    return {
        "filename": str(original.relative_to(MEDIA_DIR)),
        "thumb_filename": str(thumb_path.relative_to(MEDIA_DIR)),
        "original_name": original_name or original.name,
        "width": width,
        "height": height,
    }


def media_path(relative: str) -> Path:
    path = (MEDIA_DIR / relative).resolve()
    if MEDIA_DIR not in path.parents:
        raise ImageError("Недопустимый путь")
    return path


def remove_files(*relative: str) -> None:
    for rel in relative:
        try:
            media_path(rel).unlink(missing_ok=True)
        except ImageError:
            pass
