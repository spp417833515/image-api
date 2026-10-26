"""The contract models refuse responses the page would reject, so no code path can send one."""

from __future__ import annotations

import pytest
from imagegen.biz.models import Job, PlanResponse, PlanStep
from imagegen.biz.status_models import RouteState
from pydantic import ValidationError

IMAGE = {"id": "img_1", "url": "/api/images/img_1", "width": 64, "height": 64, "route": "t2i", "seed": 1}
REJECT = {"code": "empty_instruction", "message": "写一句指令"}


def plan_step(**fields) -> dict:
    return {"step": 1, "route": "t2i", "output": None, "requested": None, "reject": None, "unavailable": None} | fields


def job_step(step: int, state: str, **fields) -> dict:
    return {"step": step, "route": "t2i", "state": state, "progress": None, "image": None, "error": None} | fields


@pytest.mark.parametrize(
    "fields",
    [
        {"route": None, "reject": None},
        {"route": "t2i", "reject": REJECT},
        {"route": "t2i", "reject": {**REJECT, "message": ""}},
    ],
    ids=["neither", "both", "empty message"],
)
def test_a_plan_step_has_exactly_one_of_route_and_reject(fields) -> None:
    with pytest.raises(ValidationError):
        PlanStep.model_validate(plan_step(**fields))


def test_plan_ok_and_numbering_are_consistent() -> None:
    PlanResponse.model_validate({"ok": True, "steps": [plan_step(step=2), plan_step(step=3)]})
    for body in (
        {"ok": True, "steps": [plan_step(route=None, reject=REJECT)]},  # ok despite a reject
        {"ok": False, "steps": [plan_step()]},  # not ok without a reason
        {"ok": True, "steps": [plan_step(step=1), plan_step(step=3)]},  # a gap
        {"ok": True, "steps": []},
    ):
        with pytest.raises(ValidationError):
            PlanResponse.model_validate(body)


def test_job_steps_match_their_state() -> None:
    Job.model_validate(
        {"id": "j", "state": "running", "first_step": 2, "steps": [job_step(2, "running", progress=0.3)]}
    )
    for step in (
        job_step(1, "done"),  # done without an image
        job_step(1, "failed"),  # failed without the error text
        job_step(1, "queued", progress=0.5),  # progress only while running
        job_step(1, "running", progress=1.5),
        job_step(1, "skipped", image=IMAGE),
    ):
        with pytest.raises(ValidationError):
            Job.model_validate({"id": "j", "state": "running", "first_step": 1, "steps": [step]})


def test_job_state_matches_its_steps() -> None:
    done, failed = job_step(1, "done", image=IMAGE), job_step(2, "failed", error={"raw": "原文"})
    Job.model_validate({"id": "j", "state": "failed", "first_step": 1, "steps": [done, failed, job_step(3, "skipped")]})
    for state, steps in (
        ("done", [done, job_step(2, "queued")]),
        ("failed", [done, job_step(2, "skipped")]),  # failed without a failed step
        ("running", [done, failed]),
        ("queued", [done]),
        ("done", [job_step(2, "done", image=IMAGE)]),  # numbering must start at first_step
    ):
        with pytest.raises(ValidationError):
            Job.model_validate({"id": "j", "state": state, "first_step": 1, "steps": steps})


def test_an_unavailable_route_names_its_reason() -> None:
    RouteState(available=False, reason="缺少蒙版权重 models/model_patches/x.safetensors")
    for available, reason in ((False, None), (False, ""), (True, "why")):
        with pytest.raises(ValidationError):
            RouteState(available=available, reason=reason)
