"""The route table, cell by cell, through ImageGenService.plan (fake backend, no network).

Routing looks only at the number of images ("$prev" counts as one) and whether there is a mask; the instruction never
changes the route; a route the backend does not declare is rejected as route_unsupported.
"""

from __future__ import annotations

import pytest
from biz_fakes import FakeBackend, make_service, mask_png, png
from imagegen.biz.models import GenerateRequest, PlanStep, RejectCode, StepRequest
from imagegen.biz.routing import route_for
from imagegen.logic.types import Route

PREV = "$prev"


@pytest.fixture
async def service(tmp_path):
    async with make_service(FakeBackend(), tmp_path) as service:
        yield service


async def upload(service, data: bytes) -> str:
    return (await service.upload_image(data)).id


async def plan_steps(service, *steps: dict, first_step: int = 1) -> list[PlanStep]:
    request = GenerateRequest(first_step=first_step, steps=[StepRequest(**s) for s in steps])
    return (await service.plan(request)).steps


def outcome(step: PlanStep) -> Route | RejectCode:
    assert (step.route is None) != (step.reject is None)
    return step.route if step.reject is None else step.reject.code


def test_route_table_is_images_times_mask() -> None:
    assert route_for(0, False) is Route.T2I
    assert route_for(0, True) is None  # a mask with nothing to paint on
    for count in (1, 2, 3, 4):
        assert route_for(count, False) is Route.REF
        assert route_for(count, True) is Route.MASK


async def test_every_cell_of_the_route_table(service) -> None:
    a = await upload(service, png(960, 960))
    b, c, d, e = [await upload(service, png(64, 64)) for _ in range(4)]
    white = await upload(service, mask_png(960, 960, (100, 100, 400, 400)))
    black = await upload(service, mask_png(960, 960))
    small_mask = await upload(service, mask_png(512, 512, (0, 0, 100, 100)))
    say = {"instruction": "画一只狐狸"}

    cells = {
        "0 images": ([say | {"images": []}], Route.T2I),
        "1 image": ([say | {"images": [a]}], Route.REF),
        "4 images": ([say | {"images": [a, b, c, d]}], Route.REF),
        "5 images": ([say | {"images": [a, b, c, d, e]}], RejectCode.TOO_MANY_IMAGES),
        "mask only": ([say | {"mask": white}], RejectCode.MASK_WITHOUT_IMAGE),
        "image + mask": ([say | {"images": [a], "mask": white}], Route.MASK),
        "empty instruction": ([{"instruction": "  ", "images": [a]}], RejectCode.EMPTY_INSTRUCTION),
        "all-black mask": ([say | {"images": [a], "mask": black}], RejectCode.MASK_EMPTY),
        "mask / base size mismatch": ([say | {"images": [a], "mask": small_mask}], RejectCode.MASK_SIZE_MISMATCH),
        "image step with width": ([say | {"images": [a], "width": 1024}], RejectCode.SIZE_WITH_IMAGES),
        "image step with height": ([say | {"images": [a], "height": 1024}], RejectCode.SIZE_WITH_IMAGES),
        "$prev on the first step": ([say | {"images": [PREV]}], RejectCode.PREV_ON_FIRST_STEP),
        "unknown image id": ([say | {"images": ["img_0000000000000000000000"]}], RejectCode.UNKNOWN_IMAGE),
        "malformed image id": ([say | {"images": ["not-an-id"]}], RejectCode.UNKNOWN_IMAGE),
        "unknown mask id": ([say | {"images": [a], "mask": "img_ffffffffffffffffffffff"}], RejectCode.UNKNOWN_IMAGE),
        "$prev as the mask": ([say | {"images": [a], "mask": PREV}], RejectCode.UNKNOWN_IMAGE),
    }
    for name, (steps, expected) in cells.items():
        [step] = await plan_steps(service, *steps)
        assert outcome(step) == expected, name
        if step.reject is not None:
            assert step.reject.message.strip(), name
            assert step.output is None and step.requested is None and step.unavailable is None, name


async def test_prev_counts_as_one_image(service) -> None:
    a, b, c, d = [await upload(service, png(64, 64)) for _ in range(4)]
    say = "接着改"
    steps = await plan_steps(
        service,
        {"instruction": "画一只狐狸"},
        {"instruction": say, "images": [PREV, a, b, c, d]},  # $prev + 4 = 5
        {"instruction": say, "images": [PREV, a, b, c]},  # $prev + 3 = 4
        {"instruction": say, "images": [PREV]},
        {"instruction": "a new picture, no $prev"},
    )
    assert [outcome(s) for s in steps] == [
        Route.T2I,
        RejectCode.TOO_MANY_IMAGES,
        Route.REF,
        Route.REF,
        Route.T2I,  # a later step without $prev does not inherit the previous image
    ]


async def test_the_instruction_never_changes_the_route(service) -> None:
    base = await upload(service, png(640, 480))
    ref = await upload(service, png(64, 64))
    mask = await upload(service, mask_png(640, 480, (10, 10, 200, 200)))
    instructions = ["画一只狐狸", "mask this area please", "参考第二张图的风格", "把图1里的东西去掉", "x" * 500]
    for images, mask_id, expected in (
        ([], None, Route.T2I),
        ([base], None, Route.REF),
        ([base, ref], None, Route.REF),
        ([base], mask, Route.MASK),
        ([base, ref], mask, Route.MASK),
    ):
        routes = {
            outcome((await plan_steps(service, {"instruction": text, "images": images, "mask": mask_id}))[0])
            for text in instructions
        }
        assert routes == {expected}, (images, mask_id)


async def test_a_backend_without_a_route_rejects_it_as_unsupported(tmp_path) -> None:
    backend = FakeBackend(routes=frozenset({Route.T2I}), max_images=0)
    async with make_service(backend, tmp_path) as service:
        base = await upload(service, png(512, 512))
        mask = await upload(service, mask_png(512, 512, (0, 0, 100, 100)))
        steps = await plan_steps(
            service,
            {"instruction": "画一只狐狸"},
            {"instruction": "改成白狐", "images": [base], "mask": mask},
            {"instruction": "改成白狐", "images": [base]},
        )
        assert [outcome(s) for s in steps] == [Route.T2I, RejectCode.ROUTE_UNSUPPORTED, RejectCode.ROUTE_UNSUPPORTED]
        assert "蒙版改写" in steps[1].reject.message and "参考图编辑" in steps[2].reject.message
        status = await service.status()
        assert status.routes.t2i.available and status.limits.max_images == 0
        assert not status.routes.mask.available and status.routes.mask.reason
        assert not status.routes.ref.available and status.routes.ref.reason


async def test_the_image_limit_is_narrowed_by_the_backend(tmp_path) -> None:
    async with make_service(FakeBackend(max_images=2), tmp_path) as service:
        ids = [await upload(service, png(64, 64)) for _ in range(3)]
        [two] = await plan_steps(service, {"instruction": "x", "images": ids[:2]})
        [three] = await plan_steps(service, {"instruction": "x", "images": ids})
        assert outcome(two) is Route.REF
        assert outcome(three) is RejectCode.TOO_MANY_IMAGES
        assert (await service.status()).limits.max_images == 2


async def test_step_numbers_follow_first_step(service) -> None:
    steps = await plan_steps(service, {"instruction": "a"}, {"instruction": ""}, {"instruction": "c"}, first_step=4)
    assert [s.step for s in steps] == [4, 5, 6]
