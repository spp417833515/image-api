"""What the service refuses, and why. Each carries the contract's error code; the web layer only picks the HTTP status."""

from __future__ import annotations

from .models import PlanResponse, UnavailableReason


class ServiceError(Exception):
    code: str = "error"

    def __init__(self, message: str, raw: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.raw = raw


class BadImage(ServiceError):
    """An upload that is not a PNG / JPEG / WEBP image."""

    code = "bad_image"


class InvalidRequest(ServiceError):
    """A parameter outside what the service accepts (e.g. a malformed history cursor)."""

    code = "invalid_request"


class NotFound(ServiceError):
    code = "not_found"


class Rejected(ServiceError):
    """generate: at least one step is rejected; plan says which and why."""

    code = "rejected"

    def __init__(self, plan: PlanResponse) -> None:
        rejected = [s for s in plan.steps if s.reject is not None]
        first = rejected[0]
        assert first.reject is not None
        more = f"（另有 {len(rejected) - 1} 步也要改）" if len(rejected) > 1 else ""
        super().__init__(f"第 {first.step} 步要改一下：{first.reject.message}{more}")
        self.plan = plan


class ServiceUnavailable(ServiceError):
    """generate: a route the request needs cannot run now (backend down, file / node missing, upgrade running)."""

    def __init__(self, reason: UnavailableReason, message: str, raw: str) -> None:
        super().__init__(message, raw)
        self.code = reason.value
