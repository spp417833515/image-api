"""ComfyClient's HTTP side against httpx.MockTransport: what ComfyUI answers becomes a dict or a named error with raw."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from imagegen.logic.backend import UnexpectedResponse
from imagegen.logic.comfy.client import ComfyClient, ComfyHTTPError, ComfyUnreachable
from imagegen.logic.comfy.config import ComfyConfig
from offline import UNREACHABLE_URL

CONFIG = ComfyConfig(base_url=UNREACHABLE_URL)


def answering(status: int, body: str | bytes, content_type: str = "application/json") -> Callable:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers={"content-type": content_type})

    return handler


async def call(handler: Callable, method: str = "system_stats", *args):
    async with ComfyClient(CONFIG, transport=httpx.MockTransport(handler)) as client:
        return await getattr(client, method)(*args)


async def test_json_object_is_returned() -> None:
    assert await call(answering(200, '{"system": {"comfyui_version": "0.42.0"}}')) == {
        "system": {"comfyui_version": "0.42.0"}
    }


@pytest.mark.parametrize("method", ["system_stats", "object_info", "queue"])
async def test_reply_that_is_not_json_is_unexpected_response_with_the_text(method: str) -> None:
    page = "<html><body>502 Bad Gateway from a proxy</body></html>"
    with pytest.raises(UnexpectedResponse, match="did not return JSON") as caught:
        await call(answering(200, page, "text/html"), method)
    assert caught.value.raw == page
    assert not isinstance(caught.value, ValueError)  # ValueError means "the caller's step is malformed"


@pytest.mark.parametrize("body", ["[1, 2]", '"text"', "null", "42"])
async def test_json_that_is_not_an_object_is_unexpected_response(body: str) -> None:
    with pytest.raises(UnexpectedResponse, match="did not return a JSON object") as caught:
        await call(answering(200, body))
    assert caught.value.raw == body


async def test_history_reply_is_checked_too() -> None:
    with pytest.raises(UnexpectedResponse, match="did not return a JSON object"):
        await call(answering(200, "[]"), "history", "pid")


async def test_error_status_keeps_comfyui_body() -> None:
    with pytest.raises(ComfyHTTPError) as caught:
        await call(answering(500, "Server got itself in trouble", "text/plain"))
    assert caught.value.status == 500 and caught.value.body == "Server got itself in trouble"


async def test_transport_failure_is_unreachable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(ComfyUnreachable, match="connection refused"):
        await call(refuse)


async def test_upload_stored_under_another_name_is_unexpected() -> None:
    stored = {"name": "imageapi_x (1).png", "subfolder": "", "type": "input"}
    with pytest.raises(UnexpectedResponse, match="under another name") as caught:
        await call(answering(200, json.dumps(stored)), "upload_image", "imageapi_x.png", b"\x89PNG")
    assert "imageapi_x (1).png" in caught.value.raw


async def test_prompt_queued_under_another_id_is_unexpected() -> None:
    with pytest.raises(UnexpectedResponse, match="under another id"):
        await call(answering(200, '{"prompt_id": "other", "number": 1}'), "submit", {}, "client", "mine")


@pytest.mark.parametrize(
    "body", ['{"queue_running": "x"}', '{"queue_pending": []}', '{"queue_running": [[]], "queue_pending": []}']
)
async def test_queue_of_an_unknown_shape_is_unexpected(body: str) -> None:
    with pytest.raises(UnexpectedResponse, match="unknown shape"):
        await call(answering(200, body), "in_queue", "pid")
