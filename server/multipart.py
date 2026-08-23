"""Small streaming multipart/form-data reader.

The removed :mod:`cgi` module and parsers that buffer the complete request are
both poor fits for multi-hundred-megabyte reference videos.  This reader keeps
only a small boundary-sized tail in memory and spools file parts to disk.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import BinaryIO

from .errors import ApiError


@dataclass(slots=True)
class MultipartPart:
    name: str
    filename: str | None
    content_type: str
    size: int
    value: bytes | None = None
    temp_path: Path | None = None


def boundary_from_content_type(content_type: str) -> bytes:
    message = Message()
    message["content-type"] = content_type
    if message.get_content_type() != "multipart/form-data":
        raise ApiError(415, "content_type", "Content-Type must be multipart/form-data")
    boundary = message.get_param("boundary", header="content-type")
    if not boundary or not isinstance(boundary, str):
        raise ApiError(400, "multipart_boundary", "multipart boundary is missing")
    try:
        raw = boundary.encode("ascii")
    except UnicodeEncodeError as error:
        raise ApiError(400, "multipart_boundary", "multipart boundary must be ASCII") from error
    if not 1 <= len(raw) <= 200 or any(x < 33 or x > 126 for x in raw):
        raise ApiError(400, "multipart_boundary", "multipart boundary is invalid")
    return raw


class _Body:
    def __init__(self, source: BinaryIO, length: int) -> None:
        self.source = source
        self.remaining = length
        self.buffer = bytearray()

    def _fill(self, minimum: int = 1) -> bool:
        while len(self.buffer) < minimum and self.remaining:
            chunk = self.source.read(min(64 * 1024, self.remaining))
            if not chunk:
                raise ApiError(400, "truncated_body", "request body ended early")
            self.remaining -= len(chunk)
            self.buffer.extend(chunk)
        return len(self.buffer) >= minimum

    def take(self, size: int) -> bytes:
        if not self._fill(size):
            raise ApiError(400, "truncated_multipart", "multipart body ended early")
        value = bytes(self.buffer[:size])
        del self.buffer[:size]
        return value

    def readline(self, limit: int) -> bytes:
        while True:
            position = self.buffer.find(b"\n")
            if position >= 0:
                if position + 1 > limit:
                    raise ApiError(400, "multipart_header", "multipart header line is too long")
                return self.take(position + 1)
            if len(self.buffer) >= limit:
                raise ApiError(400, "multipart_header", "multipart header line is too long")
            if not self._fill(len(self.buffer) + 1):
                if self.buffer:
                    return self.take(len(self.buffer))
                return b""

    def stream_until(self, marker: bytes, sink: BinaryIO, maximum: int) -> tuple[int, bool]:
        written = 0
        keep = len(marker) - 1
        while True:
            position = self.buffer.find(marker)
            if position >= 0:
                chunk = bytes(self.buffer[:position])
                if written + len(chunk) > maximum:
                    raise ApiError(413, "upload_too_large", "multipart part is too large")
                sink.write(chunk)
                written += len(chunk)
                del self.buffer[: position + len(marker)]
                suffix = self.take(2)
                if suffix == b"--":
                    if self._fill(2) and self.buffer[:2] == b"\r\n":
                        del self.buffer[:2]
                    return written, True
                if suffix != b"\r\n":
                    raise ApiError(400, "multipart_boundary", "malformed multipart boundary")
                return written, False

            if len(self.buffer) > keep:
                length = len(self.buffer) - keep
                if written + length > maximum:
                    raise ApiError(413, "upload_too_large", "multipart part is too large")
                sink.write(self.buffer[:length])
                written += length
                del self.buffer[:length]
            before = len(self.buffer)
            if not self._fill(before + 1):
                raise ApiError(400, "multipart_boundary", "closing multipart boundary is missing")


def parse_multipart(
    source: BinaryIO,
    *,
    content_type: str,
    content_length: int,
    temp_dir: Path,
    max_total_bytes: int,
    max_field_bytes: int = 64 * 1024,
) -> list[MultipartPart]:
    if content_length <= 0:
        raise ApiError(400, "empty_body", "multipart request body is required")
    if content_length > max_total_bytes:
        raise ApiError(413, "request_too_large", "multipart request exceeds server limit")
    boundary = boundary_from_content_type(content_type)
    body = _Body(source, content_length)
    if body.readline(512) != b"--" + boundary + b"\r\n":
        raise ApiError(400, "multipart_boundary", "multipart body has an invalid opening boundary")

    parts: list[MultipartPart] = []
    finished = False
    try:
        while not finished:
            headers: dict[str, str] = {}
            header_bytes = 0
            while True:
                line = body.readline(8192)
                header_bytes += len(line)
                if header_bytes > 32 * 1024:
                    raise ApiError(400, "multipart_header", "multipart headers are too large")
                if line == b"\r\n":
                    break
                if not line.endswith(b"\r\n") or b":" not in line:
                    raise ApiError(400, "multipart_header", "malformed multipart header")
                key, value = line[:-2].split(b":", 1)
                try:
                    headers[key.decode("ascii").lower()] = value.decode("utf-8").strip()
                except UnicodeDecodeError as error:
                    raise ApiError(400, "multipart_header", "invalid multipart header encoding") from error

            disposition = Message()
            disposition["content-disposition"] = headers.get("content-disposition", "")
            if disposition.get_content_disposition() != "form-data":
                raise ApiError(400, "multipart_disposition", "part must use form-data disposition")
            name = disposition.get_param("name", header="content-disposition")
            filename = disposition.get_filename()
            if not isinstance(name, str) or not name:
                raise ApiError(400, "multipart_name", "multipart part name is missing")
            content = headers.get("content-type", "application/octet-stream")
            marker = b"\r\n--" + boundary
            if filename is None:
                from io import BytesIO

                sink = BytesIO()
                size, finished = body.stream_until(marker, sink, max_field_bytes)
                parts.append(MultipartPart(name, None, content, size, value=sink.getvalue()))
            else:
                temp_dir.mkdir(parents=True, exist_ok=True)
                handle = tempfile.NamedTemporaryFile(
                    mode="w+b", prefix="upload-", dir=temp_dir, delete=False
                )
                path = Path(handle.name)
                try:
                    size, finished = body.stream_until(marker, handle, max_total_bytes)
                    handle.flush()
                except Exception:
                    path.unlink(missing_ok=True)
                    raise
                finally:
                    handle.close()
                parts.append(MultipartPart(name, filename, content, size, temp_path=path))
        return parts
    except Exception:
        for part in parts:
            if part.temp_path:
                part.temp_path.unlink(missing_ok=True)
        raise
