"""Submit one graph and see it through to the end — or make ComfyUI let go of it, so nothing writes files afterwards."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from ..backend import StepFailed, UnexpectedResponse
from ..types import ProgressCallback
from .client import ComfyClient, ComfyHTTPError, ComfyUnreachable
from .config import ComfyConfig
from .events import StreamEnded, follow

_POLL_S = 0.2


async def run_prompt(
    client: ComfyClient,
    graph: dict[str, Any],
    config: ComfyConfig,
    *,
    progress_node: str,
    on_progress: ProgressCallback | None,
) -> str:
    """Queue graph and follow it until ComfyUI has finished it successfully; return its prompt_id.

    Any other ending — ComfyUI reports a failure, the step times out, the websocket drops, the calling task is
    cancelled — first dequeues / interrupts the prompt and waits until ComfyUI no longer holds it, so the caller's
    cleanup sees every file it wrote. Raises StepFailed (ComfyUI failure or timeout), ComfyUnreachable (connection
    lost), ComfyHTTPError (graph rejected; nothing was queued), or re-raises cancellation.
    """
    client_id = uuid.uuid4().hex
    prompt_id = str(uuid.uuid4())
    async with client.events(client_id) as events:
        try:
            await client.submit(graph, client_id, prompt_id)
        except ComfyHTTPError:
            raise  # rejected by validation: nothing was queued
        except BaseException as failure:  # the request may still have reached ComfyUI
            await stop_prompt(client, prompt_id, failure, config.stop_timeout_s)
            raise
        try:
            async with asyncio.timeout(config.run_timeout_s):
                await follow(events, prompt_id, progress_node=progress_node, on_progress=on_progress)
        except BaseException as failure:
            final = _final_error(failure, prompt_id, config)
            await stop_prompt(client, prompt_id, final, config.stop_timeout_s)
            if final is failure:
                raise
            raise final from failure
    return prompt_id


async def stop_prompt(client: ComfyClient, prompt_id: str, failure: BaseException, timeout_s: float) -> None:
    """Dequeue or interrupt the prompt and wait until ComfyUI has let go of it.

    failure is the error the caller is about to raise; if stopping fails, that is added to it as a note instead of
    replacing it.
    """
    try:
        await client.cancel(prompt_id)
        async with asyncio.timeout(timeout_s):
            while await client.in_queue(prompt_id):
                await asyncio.sleep(_POLL_S)
    except (ComfyUnreachable, ComfyHTTPError, UnexpectedResponse, TimeoutError) as exc:
        failure.add_note(f"stopping ComfyUI prompt {prompt_id} also failed, it may still write files: {exc!r}")


def _final_error(failure: BaseException, prompt_id: str, config: ComfyConfig) -> BaseException:
    if isinstance(failure, TimeoutError):
        return StepFailed(
            f"ComfyUI did not finish prompt {prompt_id} within {config.run_timeout_s:.0f}s; it was cancelled"
        )
    if isinstance(failure, StreamEnded):
        return ComfyUnreachable(config.ws_url, str(failure))
    return failure
