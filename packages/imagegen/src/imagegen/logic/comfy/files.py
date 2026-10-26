"""The files one run leaves in ComfyUI's input / output directories. ComfyUI has no HTTP API to delete them."""

from __future__ import annotations

from pathlib import Path

from .config import ComfyConfig


class CleanupError(Exception):
    """A run's files could not all be found or removed in ComfyUI's input / output directories."""


def remove_run_files(config: ComfyConfig, token: str, *, uploaded: int, outputs: int) -> list[Path]:
    """Delete every file named <token>_* in ComfyUI's input and output directories and return them.

    token is unique per run and starts with config.file_prefix, so nothing else matches. Fewer files than the run
    is known to have made means the directories are not where ComfyUI writes (see ComfyConfig.root), which is raised
    rather than ignored. Raises CleanupError (or OSError if a file cannot be deleted).
    """
    if not token.startswith(config.file_prefix) or not token[len(config.file_prefix) :].isalnum():
        raise ValueError(f"run token must be {config.file_prefix!r} followed by letters/digits, got {token!r}")
    inputs = sorted(config.input_dir.glob(f"{token}_*"))
    results = sorted(config.output_dir.glob(f"{token}_*"))
    for path in inputs + results:
        path.unlink()
    if len(inputs) < uploaded or len(results) < outputs:
        raise CleanupError(
            f"cleanup of {token}: expected {uploaded} file(s) in {config.input_dir} and {outputs} in "
            f"{config.output_dir}, found {len(inputs)} and {len(results)}; is ComfyConfig.root ({config.root}) "
            "the directory ComfyUI runs from?"
        )
    return inputs + results
