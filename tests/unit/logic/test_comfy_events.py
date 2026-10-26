"""Following a prompt over the websocket, replaying message streams captured from the real ComfyUI."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

import pytest
from imagegen.logic.backend import StepFailed, UnexpectedResponse
from imagegen.logic.comfy.client import decode_message
from imagegen.logic.comfy.events import StreamEnded, follow

FIXTURES = Path(__file__).parent / "fixtures"


def captured(name: str) -> list[dict[str, Any]]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


async def stream(messages: Iterable[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    for message in messages:
        yield message


def prompt_of(messages: list[dict[str, Any]]) -> str:
    return messages[0]["data"]["prompt_id"]


async def test_success_reports_sampler_progress_up_to_one() -> None:
    messages = captured("ws_success.json")
    seen: list[float] = []
    await follow(stream(messages), prompt_of(messages), progress_node="sample", on_progress=seen.append)
    assert seen == sorted(seen)
    assert seen[0] == pytest.approx(1 / 25)
    assert seen[-1] == 1.0


async def test_progress_from_other_nodes_is_not_the_steps_progress() -> None:
    messages = captured("ws_success.json")
    mine = prompt_of(messages)
    other = [
        {"type": "progress", "data": {"value": 3, "max": 4, "prompt_id": mine, "node": "encode"}},
        {"type": "progress", "data": {"value": 1, "max": 1, "prompt_id": mine, "node": "decode"}},
    ]
    seen: list[float] = []
    await follow(stream(other + messages), mine, progress_node="sample", on_progress=seen.append)
    sampler = [m["data"]["value"] / m["data"]["max"] for m in messages if m["type"] == "progress"]
    assert seen == sampler


async def test_execution_error_keeps_comfyui_text() -> None:
    messages = captured("ws_execution_error.json")
    with pytest.raises(StepFailed) as caught:
        await follow(stream(messages), prompt_of(messages), progress_node="sample", on_progress=None)
    raw = caught.value.raw
    assert raw.startswith("LoadImage (node load_1): PIL.UnidentifiedImageError: cannot identify image file")
    error = next(m["data"] for m in messages if m["type"] == "execution_error")
    assert error["exception_message"] in raw


async def test_messages_for_other_prompts_are_ignored() -> None:
    messages = captured("ws_success.json")
    mine = prompt_of(messages)
    noise = [{"type": "execution_error", "data": {"prompt_id": "someone-else", "exception_message": "x"}}]
    noise.append({"type": "status", "data": {"status": {"exec_info": {"queue_remaining": 1}}}})
    await follow(stream(noise + messages), mine, progress_node="sample", on_progress=None)


async def test_interrupted_prompt_fails() -> None:
    messages = [
        {"type": "execution_interrupted", "data": {"prompt_id": "p", "node_id": "sample", "node_type": "KSampler"}}
    ]
    with pytest.raises(StepFailed, match=r"execution_interrupted: KSampler \(node sample\)"):
        await follow(stream(messages), "p", progress_node="sample", on_progress=None)


async def test_stream_closing_early_is_not_success() -> None:
    messages = captured("ws_success.json")[:5]
    with pytest.raises(StreamEnded):
        await follow(stream(messages), prompt_of(messages), progress_node="sample", on_progress=None)


@pytest.mark.parametrize(
    "data",
    [
        {"value": 5, "node": "sample"},  # no max
        {"value": 5, "max": 0, "node": "sample"},  # division by zero
        {"value": "5", "max": 25, "node": "sample"},  # not a number
    ],
)
async def test_unreadable_progress_is_unexpected_response_with_raw(data: dict[str, Any]) -> None:
    message = {"type": "progress", "data": {**data, "prompt_id": "p"}}
    with pytest.raises(UnexpectedResponse, match="usable value / max") as caught:
        await follow(stream([message]), "p", progress_node="sample", on_progress=None)
    assert json.loads(caught.value.raw) == message


async def test_messages_without_a_data_object_are_not_about_this_prompt() -> None:
    messages = captured("ws_success.json")
    odd = [{"type": "crystools.monitor", "data": [1, 2]}, {"type": "status"}, {"type": "progress", "data": "x"}]
    await follow(stream(odd + messages), prompt_of(messages), progress_node="sample", on_progress=None)


@pytest.mark.parametrize(("text", "expected"), [("{not json", "not JSON"), ("[1, 2]", "not a JSON object")])
def test_websocket_frames_that_are_not_json_objects(text: str, expected: str) -> None:
    with pytest.raises(UnexpectedResponse, match=expected) as caught:
        decode_message(text)
    assert caught.value.raw == text
    assert not isinstance(caught.value, ValueError)  # ValueError means "the caller's step is malformed"


def test_websocket_frame_decodes_to_its_object() -> None:
    assert decode_message('{"type": "status", "data": {}}') == {"type": "status", "data": {}}
