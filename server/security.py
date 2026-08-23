"""Path, identifier, filename, and media-content security helpers."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .errors import ApiError


ID_RE = re.compile(r"^[0-9a-f]{32}$")
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class MediaSignature:
    kind: str
    mime_type: str
    extension: str


def validate_id(value: str, label: str = "id") -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ApiError(400, "invalid_id", f"{label} must be a 32-character hex id")
    return value


def safe_filename(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ApiError(400, "invalid_filename", "a non-empty filename is required")
    normalized = unicodedata.normalize("NFKC", value).replace("\\", "/")
    basename = normalized.rsplit("/", 1)[-1].strip()
    if basename in {"", ".", ".."}:
        raise ApiError(400, "invalid_filename", "filename is not valid")
    cleaned = SAFE_FILENAME_RE.sub("_", basename).strip("._")
    if not cleaned:
        cleaned = "upload"
    return cleaned[:180]


def secure_join(root: Path, *parts: str) -> Path:
    candidate = root.joinpath(*parts).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as error:
        raise ApiError(403, "unsafe_path", "path escapes the configured storage root") from error
    return candidate


def sniff_media(head: bytes, filename: str = "") -> MediaSignature | None:
    lower = filename.lower()
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return MediaSignature("image", "image/png", ".png")
    if head.startswith(b"\xff\xd8\xff"):
        return MediaSignature("image", "image/jpeg", ".jpg")
    if head[:6] in {b"GIF87a", b"GIF89a"}:
        return MediaSignature("image", "image/gif", ".gif")
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return MediaSignature("image", "image/webp", ".webp")
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return MediaSignature("audio", "audio/wav", ".wav")
    if head.startswith(b"fLaC"):
        return MediaSignature("audio", "audio/flac", ".flac")
    if head.startswith(b"OggS"):
        return MediaSignature("audio", "audio/ogg", ".ogg")
    if head.startswith(b"ID3") or (
        len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0
    ):
        return MediaSignature("audio", "audio/mpeg", ".mp3")
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand == b"qt  " or lower.endswith(".mov"):
            return MediaSignature("video", "video/quicktime", ".mov")
        return MediaSignature("video", "video/mp4", ".mp4")
    if head.startswith(b"\x1aE\xdf\xa3"):
        if lower.endswith(".webm"):
            return MediaSignature("video", "video/webm", ".webm")
        return MediaSignature("video", "video/x-matroska", ".mkv")
    return None


def validate_media(
    *,
    head: bytes,
    filename: str,
    requested_kind: str,
    size: int,
    limits: dict[str, int],
) -> MediaSignature:
    signature = sniff_media(head, filename)
    if signature is None:
        raise ApiError(
            415,
            "unsupported_media",
            "file content is not a supported image, video, or audio format",
        )
    if requested_kind not in {"", "auto", signature.kind}:
        raise ApiError(
            400,
            "media_kind_mismatch",
            f"declared kind {requested_kind!r} does not match detected {signature.kind!r}",
        )
    if size <= 0:
        raise ApiError(400, "empty_upload", "uploaded file is empty")
    if size > limits[signature.kind]:
        raise ApiError(
            413,
            "upload_too_large",
            f"{signature.kind} exceeds the configured {limits[signature.kind]} byte limit",
        )
    return signature
