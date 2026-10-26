"""Every error leaves as the contract's body {code, message, raw?}: the service's refusals, FastAPI's validation errors,
unknown paths and methods, and anything unexpected. No {"detail": …} body reaches a caller."""

from __future__ import annotations

import logging
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from imagegen.biz.errors import BadImage, InvalidRequest, NotFound, Rejected, ServiceError, ServiceUnavailable
from imagegen.biz.models import ErrorBody, RejectedBody
from starlette.exceptions import HTTPException

logger = logging.getLogger(__name__)

_STATUS: dict[type[ServiceError], int] = {
    BadImage: 422,
    InvalidRequest: 422,
    Rejected: 422,
    NotFound: 404,
    ServiceUnavailable: 503,
}
_HTTP_MESSAGES = {404: "没有这个地址", 405: "这个地址不支持这种请求方法"}


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ServiceError, _service_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(Exception, _unexpected)


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
    return _body(422, ErrorBody(code="invalid_request", message=message, raw="\n".join(problems) or None))


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, HTTPException)
    phrase = HTTPStatus(exc.status_code).phrase if exc.status_code in HTTPStatus else str(exc.status_code)
    code = phrase.lower().replace(" ", "_")
    message = f"{_HTTP_MESSAGES.get(exc.status_code, phrase)}：{request.method} {request.url.path}"
    return _body(exc.status_code, ErrorBody(code=code, message=message, raw=str(exc.detail)), exc.headers)


async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
    logger.error("%s %s failed", request.method, request.url.path, exc_info=exc)
    raw = "\n".join([f"{type(exc).__name__}: {exc}", *getattr(exc, "__notes__", ())])
    return _body(500, ErrorBody(code="internal_error", message="服务内部出错", raw=raw))
