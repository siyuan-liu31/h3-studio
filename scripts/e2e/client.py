"""Small standard-library client for the MiniMax H3 Video Studio HTTP API."""

from __future__ import annotations

import hashlib
import http.client
import json
import mimetypes
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable


class E2EError(RuntimeError):
    """A remote API or verification failure."""

    partial_evidence: dict[str, Any] | None = None


class JobTimeout(E2EError):
    """A submitted job exceeded its caller-supplied deadline."""


_OPAQUE_ID = re.compile(r"^[0-9a-f]{32}$")
_MAX_JSON_BYTES = 8 * 1024 * 1024


def validate_opaque_id(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _OPAQUE_ID.fullmatch(value) is None:
        raise E2EError(f"{label} must be 32 lowercase hexadecimal characters")
    return value


def _origin(parsed: urllib.parse.SplitResult) -> tuple[str, str, int]:
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    return (parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or default_port)


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Turn every redirect into an HTTPError instead of forwarding credentials."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class ApiClient:
    def __init__(self, base_url: str, api_key: str = "", timeout: float = 60) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("base_url must be an http(s) URL")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain a query or fragment")
        if "\r" in api_key or "\n" in api_key:
            raise ValueError("api_key must not contain line breaks")
        self.base_url = base_url.rstrip("/")
        self._base = urllib.parse.urlsplit(self.base_url)
        self.api_key = api_key
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_RejectRedirects())

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "h3-studio-e2e/1"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if extra:
            headers.update(extra)
        return headers

    def _url(self, path: str) -> str:
        result = urllib.parse.urljoin(self.base_url + "/", path.lstrip("/"))
        if _origin(urllib.parse.urlsplit(result)) != _origin(self._base):
            raise E2EError("refusing to call an API path on a different origin")
        return result

    @staticmethod
    def _read_json_body(response: Any, *, context: str) -> bytes:
        raw = response.read(_MAX_JSON_BYTES + 1)
        if len(raw) > _MAX_JSON_BYTES:
            raise E2EError(f"{context} response exceeds {_MAX_JSON_BYTES} bytes")
        return raw

    def json_request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = self._headers({"Content-Type": "application/json"} if body is not None else None)
        request = urllib.request.Request(self._url(path), data=body, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = self._read_json_body(response, context=f"{method} {path}")
        except urllib.error.HTTPError as error:
            raw = error.read(4096)
            if 300 <= error.code < 400:
                raise E2EError(f"{method} {path} refused HTTP redirect {error.code}") from error
            try:
                detail = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                detail = raw[:500].decode("utf-8", "replace")
            raise E2EError(f"{method} {path} returned HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise E2EError(f"{method} {path} failed: {error.reason}") from error
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise E2EError(f"{method} {path} returned invalid JSON") from error
        if not isinstance(value, dict):
            raise E2EError(f"{method} {path} returned a non-object JSON response")
        return value

    def capabilities(self) -> dict[str, Any]:
        return self.json_request("GET", "/api/capabilities")

    def upload(self, path: Path, kind: str) -> dict[str, Any]:
        if not path.is_file():
            raise E2EError(f"upload path does not exist: {path}")
        parsed = urllib.parse.urlsplit(self._url("/api/assets"))
        boundary = f"----h3studioe2e{uuid.uuid4().hex}"
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        safe_name = path.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        prefix = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"kind\"\r\n\r\n{kind}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{safe_name}\"\r\n"
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
        suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
        headers = self._headers({
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(prefix) + path.stat().st_size + len(suffix)),
        })
        connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        connection = connection_type(parsed.hostname, parsed.port, timeout=self.timeout)
        target = urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, ""))
        try:
            connection.putrequest("POST", target)
            for name, value in headers.items():
                connection.putheader(name, value)
            connection.endheaders()
            connection.send(prefix)
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    connection.send(chunk)
            connection.send(suffix)
            response = connection.getresponse()
            raw = response.read(_MAX_JSON_BYTES + 1)
        except (OSError, http.client.HTTPException, ssl.SSLError) as error:
            raise E2EError(f"upload failed for {path}: {error}") from error
        finally:
            connection.close()
        if 300 <= response.status < 400:
            raise E2EError(f"upload refused HTTP redirect {response.status}")
        if len(raw) > _MAX_JSON_BYTES:
            raise E2EError(f"upload response exceeds {_MAX_JSON_BYTES} bytes")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise E2EError(f"upload returned HTTP {response.status} with invalid JSON") from error
        if response.status not in {200, 201}:
            raise E2EError(f"upload returned HTTP {response.status}: {value}")
        if not isinstance(value, dict):
            raise E2EError("upload response is not an object")
        asset = value.get("asset") if isinstance(value.get("asset"), dict) else value
        asset_id = asset.get("id", value.get("asset_id"))
        validate_opaque_id(asset_id, label="asset id")
        return {**asset, "id": asset_id}

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.json_request("POST", "/api/generate", payload)

    def wait(
        self,
        job_id: str,
        *,
        timeout: float,
        interval: float = 3,
        on_status: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        validate_opaque_id(job_id, label="job id")
        deadline = time.monotonic() + timeout
        while True:
            status = self.json_request("GET", f"/api/status?id={urllib.parse.quote(job_id)}")
            if on_status:
                on_status(status)
            state = str(status.get("status", status.get("state", ""))).lower()
            if state == "completed":
                return self.json_request("GET", f"/api/result?id={urllib.parse.quote(job_id)}")
            if state in {"failed", "error", "canceled", "cancelled"}:
                raise E2EError(f"job {job_id} ended as {state}: {status.get('message', status)}")
            if time.monotonic() >= deadline:
                raise JobTimeout(f"job {job_id} did not finish within {timeout:g}s")
            time.sleep(max(0.1, interval))

    def cancel(self, job_id: str) -> dict[str, Any]:
        validate_opaque_id(job_id, label="job id")
        return self.json_request("POST", f"/api/jobs/{job_id}/cancel", {})

    def download(self, path_or_url: str, destination: Path) -> dict[str, Any]:
        supplied = urllib.parse.urlsplit(path_or_url)
        if supplied.scheme or supplied.netloc:
            if supplied.username or supplied.password or _origin(supplied) != _origin(self._base):
                raise E2EError("refusing to download an output from a different origin")
            download_url = path_or_url
        else:
            download_url = self._url(path_or_url)
        request = urllib.request.Request(
            download_url,
            headers=self._headers(), method="GET",
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + f".tmp-{uuid.uuid4().hex}")
        digest = hashlib.sha256()
        size = 0
        try:
            with self._opener.open(request, timeout=self.timeout) as response, temporary.open("xb") as output:
                while chunk := response.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
                    output.write(chunk)
            if size <= 0:
                raise E2EError("downloaded output is empty")
            os.replace(temporary, destination)
        except urllib.error.HTTPError as error:
            if 300 <= error.code < 400:
                raise E2EError(f"download refused HTTP redirect {error.code}") from error
            raise E2EError(f"download failed with HTTP {error.code}") from error
        except (OSError, urllib.error.URLError) as error:
            raise E2EError(f"download failed: {error}") from error
        finally:
            temporary.unlink(missing_ok=True)
        return {"path": str(destination), "size": size, "sha256": digest.hexdigest()}
