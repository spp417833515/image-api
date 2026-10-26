"""Suite registry: the single source for what the gate runs and how.

Each suite is a pytest selection or a static-check command. `modes` says which gate mode includes it:
- check:  the routine gate (executors, reviewers and the supervisor all run the same command)
- formal: the strict gate before merge; adds the real ComfyUI upgrade/rollback suite (it stops ComfyUI for a few minutes)
A suite whose capability is missing (e.g. ComfyUI is down) counts as red with its name reported; it is never silently skipped.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CHECK = "check"
FORMAL = "formal"


@dataclass(frozen=True)
class Suite:
    name: str
    kind: str  # "pytest" | "command"
    args: tuple[str, ...]  # pytest: selection arguments; command: argv
    modes: frozenset[str]
    needs: tuple[str, ...] = field(default=())  # capability names, see capabilities.py
    timeout_s: int = 600


SUITES: tuple[Suite, ...] = (
    Suite("ruff-lint", "command", ("ruff", "check", "."), frozenset({CHECK, FORMAL}), timeout_s=120),
    Suite("ruff-format", "command", ("ruff", "format", "--check", "."), frozenset({CHECK, FORMAL}), timeout_s=120),
    Suite(
        "unit", "pytest", ("tests", "-m", "not live and not live_versions"), frozenset({CHECK, FORMAL}), timeout_s=300
    ),
    Suite("live", "pytest", ("tests", "-m", "live"), frozenset({CHECK, FORMAL}), needs=("comfyui",), timeout_s=1800),
    Suite(
        "live-versions",
        "pytest",
        ("tests", "-m", "live_versions"),
        frozenset({FORMAL}),
        needs=("comfyui", "comfyui-service"),
        timeout_s=3600,
    ),
)


def for_mode(mode: str) -> list[Suite]:
    return [s for s in SUITES if mode in s.modes]
