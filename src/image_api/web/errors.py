"""Every error leaves as the contract's body {code, message, raw?}: the service's refusals, FastAPI's validation errors,
unknown paths and methods, and anything unexpected. No {"detail": …} body reaches a caller."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from imagegen.biz.errors import BadImage, InvalidRequest, NotFound, Rejected, ServiceError, ServiceUnavailable
from imagegen.biz.models import ErrorBody, ErrorCode, RejectedBody
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

_STATUS: dict[type[ServiceError], int] = {
    BadImage: 422,
    InvalidRequest: 422,
    Rejected: 422,
    NotFound: 404,
    ServiceUnavailable: 503,
}
_HTTP_ERRORS = {
    404: (ErrorCode.NOT_FOUND, "没有这个地址"),
    405: (ErrorCode.METHOD_NOT_ALLOWED, "这个地址不支持这种请求方法"),
}


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ServiceError, _service_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(HTTPException, _http_error)
    app.add_middleware(CatchUnexpected)


class CatchUnexpected:
    """Answers an unexpected exception with a 500 contract body, inside the app.

    Starlette's own catch-all sits in its outermost middleware, which sends the 500 and then re-raises: uvicorn logs
    the error a second time and drops the keep-alive connection, so the client's next request fails. Here the
    exception ends with the 500 and the connection stays usable. Once a response has started, nothing can be sent
    instead, so the exception is re-raised.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def send_tracking(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, receive, send_tracking)
        except Exception as exc:
            if started:
                raise
            logger.error("%s %s failed", scope["method"], scope["path"], exc_info=exc)
            raw = "\n".join([f"{type(exc).__name__}: {exc}", *getattr(exc, "__notes__", ())])
            body = ErrorBody(code=ErrorCode.INTERNAL_ERROR, message="服务内部出错", raw=raw)
            await _body(500, body)(scope, receive, send)


def _body(status: int, body: ErrorBody, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(body.model_dump(mode="json"), status_code=status, headers=headers)


async def _service_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ServiceError)
    status = next(code for kind, code in _STATUS.items() if isinstance(exc, kind))
    if isinstance(exc, Rejected):
        return _body(status, RejectedBody(message=exc.message, plan=exc.plan))
    return _body(status, ErrorBody(code=exc.code, message=exc.message, raw=exc.raw))


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    problems = [f"{'.'.join(str(part) for part in e['loc'])}: {e['msg']}" for e in exc.errors()]
    message = f"请求格式不对：{problems[0]}" if problems else "请求格式不对"
    return _body(422, ErrorBody(code=ErrorCode.INVALID_REQUEST, message=message, raw="\n".join(problems) or None))


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    """Errors Starlette raises itself: unknown paths and methods, and requests it cannot parse (e.g. a broken
    multipart body, 400)."""
    assert isinstance(exc, HTTPException)
    default = (
        (ErrorCode.INVALID_REQUEST, "请求有问题")
        if exc.status_code < 500
        else (ErrorCode.INTERNAL_ERROR, "服务内部出错")
    )
    code, text = _HTTP_ERRORS.get(exc.status_code, default)
    message = f"{text}：{request.method} {request.url.path}"
    return _body(exc.status_code, ErrorBody(code=code, message=message, raw=str(exc.detail)), exc.headers)
