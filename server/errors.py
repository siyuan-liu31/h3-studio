"""Typed API errors shared by validation, storage, and HTTP handlers."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any


class ApiError(Exception):
    """An error that is safe to serialize to an API caller."""

    def __init__(
        self,
        status: int | HTTPStatus,
        code: str,
        message: str,
        *,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            error["details"] = self.details
        return {"error": error}


class CapabilityError(ApiError):
    """A requested workflow cannot run with the installed ComfyUI models/nodes."""

    def __init__(self, message: str, *, details: Any | None = None) -> None:
        super().__init__(
            HTTPStatus.CONFLICT,
            "capability_unavailable",
            message,
            details=details,
        )
