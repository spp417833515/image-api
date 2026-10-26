"""run_prompt: every ending other than success makes ComfyUI let go of the prompt before the error propagates.

A scripted stand-in replaces ComfyClient (no network); websocket messages are the ones captured from the real ComfyUI.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from imagegen.logic.backend import StepFailed
from imagegen.logic.comfy.client import ComfyHTTPError, ComfyUnreachable
from imagegen.logic.comfy.config import ComfyConfig
from imagegen.logic.comfy.prompt import run_prompt

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = ComfyConfig(run_timeout_s=5.0, stop_timeout_s=2.0)


def captured(name: str, prompt_id: str) -> list[dict[str, Any]]:
    text = (FIXTURES / name).read_text(encoding="utf-8")
    original = json.loads(text)[0]["data"]["prompt_id"]
    return json.loads(text.replace(original, prompt_id))


class FakeComfy:
    """Records calls; `script(prompt_id)` produces the websocket stream after submit; stays queued for `busy` polls."""

    def __init__(self, script: Callable[[str], AsyncIterator[dict[str, Any]]], *, busy: int = 2) -> None:
        self.script = script
        self.busy = busy
        self.calls: list[str] = []
        self.prompt_id = ""
        self.submit_error: BaseException | None = None
        self.cancel_error: BaseException | None = None
        self.submitted = asyncio.Event()

    @asynccontextmanager
    async def events(self, client_id: str) -> AsyncIterator[AsyncIterator[dict[str, Any]]]:
        async def after_submit() -> AsyncIterator[dict[str, Any]]:
            await self.submitted.wait()
            async for message in self.script(self.prompt_id):
                yield message

        yield after_submit()

    async def submit(self, graph: dict[str, Any], client_id: str, prompt_id: str) -> None:
        self.calls.append("submit")
        self.prompt_id = prompt_id
        if self.submit_error is not None:
            raise self.submit_error
        self.submitted.set()

    async def cancel(self, prompt_id: str) -> None:
        assert prompt_id == self.prompt_id
        self.calls.append("cancel")
        if self.cancel_error is not None:
            raise self.cancel_error

    async def in_queue(self, prompt_id: str) -> bool:
        self.calls.append("in_queue")
        self.busy -= 1
        return self.busy >= 0

    @property
    def stopped(self) -> bool:
        """cancel, then polled until ComfyUI no longer had it."""
        return "cancel" in self.calls and self.calls[-1] == "in_queue" and self.busy < 0


async def go(fake: FakeComfy, config: ComfyConfig = CONFIG, on_progress=None) -> str:
    return await run_prompt(fake, {"g": {}}, config, progress_node="sample", on_progress=on_progress)  # type: ignore[arg-type]


def replay(name: str, then: BaseException | None = None, cut: int | None = None):
    async def script(prompt_id: str) -> AsyncIterator[dict[str, Any]]:
        for message in captured(name, prompt_id)[:cut]:
            yield message
        if then is not None:
            raise then

    return script


def forever_after_progress(progressed: asyncio.Event):
    async def script(prompt_id: str) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "progress", "data": {"value": 1, "max": 25, "prompt_id": prompt_id, "node": "sample"}}
        progressed.set()
        await asyncio.Event().wait()
        yield {}

    return script


async def test_success_does_not_cancel() -> None:
    fake = FakeComfy(replay("ws_success.json"))
    assert await go(fake) == fake.prompt_id
    assert fake.calls == ["submit"]


async def test_websocket_drop_stops_the_prompt_then_reports_unreachable() -> None:
    fake = FakeComfy(replay("ws_success.json", then=ComfyUnreachable("ws://x/ws", "connection reset"), cut=6))
    with pytest.raises(ComfyUnreachable, match="connection reset"):
        await go(fake)
    assert fake.stopped


async def test_stream_closing_early_stops_the_prompt() -> None:
    fake = FakeComfy(replay("ws_success.json", cut=6))
    with pytest.raises(ComfyUnreachable, match="closed before prompt"):
        await go(fake)
    assert fake.stopped


async def test_cancelling_the_task_stops_the_prompt_first() -> None:
    progressed = asyncio.Event()
    fake = FakeComfy(forever_after_progress(progressed))
    task = asyncio.create_task(go(fake))
    await progressed.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.stopped


async def test_timeout_stops_the_prompt_and_fails_the_step() -> None:
    fake = FakeComfy(forever_after_progress(asyncio.Event()))
    with pytest.raises(StepFailed, match="did not finish prompt .* within 0s; it was cancelled"):
        await go(fake, ComfyConfig(run_timeout_s=0.05, stop_timeout_s=2.0))
    assert fake.stopped


async def test_comfyui_failure_keeps_its_text_and_waits_for_comfyui_to_let_go() -> None:
    fake = FakeComfy(replay("ws_execution_error.json"))
    with pytest.raises(StepFailed, match="PIL.UnidentifiedImageError: cannot identify image file"):
        await go(fake)
    assert fake.stopped


async def test_rejected_graph_is_not_cancelled() -> None:
    fake = FakeComfy(replay("ws_success.json"))
    fake.submit_error = ComfyHTTPError("POST", "http://x/prompt", 400, '{"error": "bad"}')
    with pytest.raises(ComfyHTTPError):
        await go(fake)
    assert fake.calls == ["submit"]


async def test_submit_lost_in_transit_is_still_stopped() -> None:
    fake = FakeComfy(replay("ws_success.json"))
    fake.submit_error = ComfyUnreachable("http://x/prompt", "read timeout")
    with pytest.raises(ComfyUnreachable):
        await go(fake)
    assert fake.stopped


async def test_failing_to_stop_is_a_note_not_a_replacement() -> None:
    fake = FakeComfy(replay("ws_execution_error.json"))
    fake.cancel_error = ComfyUnreachable("http://x/queue", "refused")
    with pytest.raises(StepFailed, match="PIL.UnidentifiedImageError") as caught:
        await go(fake)
    assert any("stopping ComfyUI prompt" in note and "refused" in note for note in caught.value.__notes__)
