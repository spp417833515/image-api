"""Transport to ComfyUI over HTTP (httpx) and websocket (websockets). ComfyUI's error bodies are kept verbatim."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Any, Self

import httpx
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosedError, InvalidHandshake

from ..backend import UnexpectedResponse
from .config import ComfyConfig

# ComfyUI may stream preview frames as binary messages; they are skipped but must not trip the size limit.
_WS_MAX_MESSAGE = 64 * 1024 * 1024


class ComfyUnreachable(Exception):
    """No usable connection to ComfyUI (refused, reset, timed out, or the websocket dropped)."""

    def __init__(self, target: str, cause: BaseException | str) -> None:
        super().__init__(f"cannot reach ComfyUI at {target}: {cause!r}" if isinstance(cause, BaseException) else cause)
        self.target = target


class ComfyHTTPError(Exception):
    """ComfyUI answered with a non-2xx status; body is its response text, unedited."""

    def __init__(self, method: str, url: str, status: int, body: str) -> None:
        super().__init__(f"{method} {url} → HTTP {status}: {body}")
        self.status = status
        self.body = body


class ComfyClient:
    """One session with ComfyUI. Use as `async with ComfyClient(config) as client:`."""

    def __init__(self, config: ComfyConfig) -> None:
        self._config = config
        self._http = httpx.AsyncClient(base_url=config.base_url, timeout=config.http_timeout_s)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self._http.aclose()

    async def system_stats(self) -> dict[str, Any]:
        return await self._json("GET", "/system_stats")

    async def object_info(self) -> dict[str, Any]:
        return await self._json("GET", "/object_info")

    async def upload_image(self, name: str, png: bytes) -> str:
        """Upload into ComfyUI's input directory under exactly this name; returns the name for LoadImage."""
        if not name.startswith(self._config.file_prefix) or "/" in name:
            raise ValueError(
                f"upload names must be plain file names starting with {self._config.file_prefix!r}, got {name!r}"
            )
        stored = await self._json(
            "POST",
            "/upload/image",
            files={"image": (name, png, "image/png")},
            data={"overwrite": "true", "type": "input"},
        )
        if stored.get("name") != name or stored.get("subfolder"):
            raise UnexpectedResponse(f"ComfyUI stored upload {name!r} under another name or folder", str(stored))
        return name

    async def submit(self, graph: dict[str, Any], client_id: str, prompt_id: str) -> None:
        """Queue an API-format graph under prompt_id (a UUID chosen by the caller, so the prompt can be cancelled even
        if this call is interrupted). A rejected graph raises ComfyHTTPError with ComfyUI's body."""
        body = {"prompt": graph, "client_id": client_id, "prompt_id": prompt_id}
        queued = await self._json("POST", "/prompt", json=body)
        if queued.get("prompt_id") != prompt_id:
            raise UnexpectedResponse(f"ComfyUI queued the prompt under another id than {prompt_id}", str(queued))

    async def history(self, prompt_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/history/{prompt_id}")

    async def queue(self) -> dict[str, Any]:
        """{"queue_running": [...], "queue_pending": [...]}; each item is [number, prompt_id, graph, extra, outputs]."""
        return await self._json("GET", "/queue")

    async def in_queue(self, prompt_id: str) -> bool:
        """Whether ComfyUI is still running the prompt or has it waiting."""
        queue = await self.queue()
        try:
            return any(item[1] == prompt_id for key in ("queue_running", "queue_pending") for item in queue[key])
        except (KeyError, IndexError, TypeError) as exc:
            raise UnexpectedResponse("GET /queue has an unknown shape", str(queue)[:2000]) from exc

    async def view(self, filename: str, subfolder: str, type: str) -> bytes:
        params = {"filename": filename, "subfolder": subfolder, "type": type}
        return (await self._request("GET", "/view", params=params)).content

    async def cancel(self, prompt_id: str) -> None:
        """Drop the prompt from the queue, or interrupt it if it is the one running."""
        await self._request("POST", "/queue", json={"delete": [prompt_id]})
        await self._request("POST", "/interrupt", json={"prompt_id": prompt_id})

    @asynccontextmanager
    async def events(self, client_id: str) -> AsyncIterator[AsyncIterator[dict[str, Any]]]:
        """Websocket messages addressed to client_id, as parsed JSON. Open it before submitting so nothing is missed."""
        url = f"{self._config.ws_url}?clientId={client_id}"
        try:
            ws = await connect(url, max_size=_WS_MAX_MESSAGE, open_timeout=self._config.http_timeout_s)
        except (OSError, InvalidHandshake, TimeoutError) as exc:
            raise ComfyUnreachable(url, exc) from exc
        async with ws:
            yield _messages(ws, url)

    async def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """The response's JSON object; anything else is an UnexpectedResponse carrying the text."""
        response = await self._request(method, path, **kwargs)
        try:
            data = response.json()
        except ValueError as exc:
            raise UnexpectedResponse(f"{method} {path} did not return JSON", response.text[:2000]) from exc
        if not isinstance(data, dict):
            raise UnexpectedResponse(f"{method} {path} did not return a JSON object", response.text[:2000])
        return data

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise ComfyUnreachable(f"{self._config.base_url}{path}", exc) from exc
        if not response.is_success:
            raise ComfyHTTPError(method, str(response.url), response.status_code, response.text)
        return response


def decode_message(text: str) -> dict[str, Any]:
    """One websocket text frame as the JSON object ComfyUI always sends ({"type": …, "data": …})."""
    try:
        message = json.loads(text)
    except ValueError as exc:
        raise UnexpectedResponse("ComfyUI sent a websocket message that is not JSON", text[:2000]) from exc
    if not isinstance(message, dict):
        raise UnexpectedResponse("ComfyUI sent a websocket message that is not a JSON object", text[:2000])
    return message


async def _messages(ws: ClientConnection, url: str) -> AsyncIterator[dict[str, Any]]:
    try:
        async for message in ws:
            if isinstance(message, str):
                yield decode_message(message)
    except ConnectionClosedError as exc:
        raise ComfyUnreachable(url, exc) from exc
