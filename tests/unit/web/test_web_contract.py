"""Every endpoint's response, in-process over HTTP, as the page sees it: field shapes checked by the page's own
shapes.js, step numbering, real pixel sizes, history paging, and the page and its static files."""

from __future__ import annotations

import io
from datetime import UTC, datetime

from biz_fakes import decoded, encode, mask_png, png
from imagegen.biz.version_state import (
    CheckItem,
    LastCheck,
    Operation,
    OperationKind,
    OperationResult,
    Upgrading,
    Version,
)
from imagegen.logic.backend import StepFailed
from page_shapes import assert_page_accepts
from PIL import Image

AT = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def upload(client, data: bytes, filename: str = "x.png") -> dict:
    response = client.post("/api/images", files={"file": (filename, data)})
    assert response.status_code == 201, response.text
    return response.json()


def test_upload_reports_the_real_pixel_size_and_serves_a_named_png(client) -> None:
    rotated = Image.new("RGB", (300, 200), (200, 10, 10))
    exif = rotated.getexif()
    exif[0x0112] = 6  # EXIF orientation: rotate 90° on display
    jpeg = encode_jpeg_with_exif(rotated, exif)
    body = upload(client, jpeg, "photo.jpg")
    assert_page_accepts([("upload", body)])
    assert body["url"] == f"/api/images/{body['id']}"
    image = client.get(body["url"])
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    assert f'filename="{body["id"]}.png"' in image.headers["content-disposition"]
    assert decoded(image.content).size == (body["width"], body["height"]) == (200, 300)


def encode_jpeg_with_exif(image: Image.Image, exif: Image.Exif) -> bytes:
    out = io.BytesIO()
    image.save(out, format="JPEG", exif=exif)
    return out.getvalue()


def test_webp_and_transparent_png_uploads_are_accepted(client) -> None:
    for data in (encode(Image.new("RGB", (64, 48)), "WEBP"), encode(Image.new("RGBA", (40, 30)), "PNG")):
        body = upload(client, data, "no-extension")
        assert (body["width"], body["height"]) == decoded(client.get(body["url"]).content).size


def test_plan_status_generate_job_history_as_the_page_reads_them(client, wait_for_job) -> None:
    base = upload(client, png(960, 540))
    mask = upload(client, mask_png(960, 540, (0, 0, 300, 300)))
    request = {
        "first_step": 2,
        "steps": [
            {"instruction": "画一只狐狸", "images": [], "mask": None, "seed": None, "width": 1000, "height": 700},
            {"instruction": "改", "images": ["$prev"], "mask": None, "seed": 5, "width": None, "height": None},
            {
                "instruction": "改这块",
                "images": [base["id"]],
                "mask": mask["id"],
                "seed": None,
                "width": None,
                "height": None,
            },
        ],
    }
    plan = client.post("/api/plan", json=request)
    assert plan.status_code == 200
    plan = plan.json()
    assert plan["ok"] and [s["step"] for s in plan["steps"]] == [2, 3, 4]
    assert plan["steps"][0]["output"] == {"width": 992, "height": 704}
    assert plan["steps"][0]["requested"] == {"width": 1000, "height": 700}
    assert [s["route"] for s in plan["steps"]] == ["t2i", "ref", "mask"]

    accepted = client.post("/api/generate", json=request)
    assert accepted.status_code == 202
    polls = wait_for_job(accepted.json()["job_id"])
    job = polls[-1]
    assert job["state"] == "done" and [s["step"] for s in job["steps"]] == [2, 3, 4]
    assert job["steps"][1]["image"]["seed"] == 5
    for step in job["steps"]:
        served = decoded(client.get(step["image"]["url"]).content)
        assert served.size == (step["image"]["width"], step["image"]["height"])
    assert (job["steps"][2]["image"]["width"], job["steps"][2]["image"]["height"]) == (960, 544)

    history = client.get("/api/history", params={"limit": 50}).json()
    assert [i["id"] for i in history["items"]] == [s["image"]["id"] for s in reversed(job["steps"])]
    assert [i["step"] for i in history["items"]] == [4, 3, 2]
    status = client.get("/api/status").json()
    assert_page_accepts(
        [("plan", plan), ("generate", accepted.json()), ("history", history), ("status", status)]
        + [("job", body) for body in polls]
    )


def test_a_failed_job_as_the_page_reads_it(client, backend, wait_for_job) -> None:
    backend.failures[1] = StepFailed("Prompt outputs failed validation: Value 0 smaller than min of 1")
    request = {
        "first_step": 1,
        "steps": [{"instruction": "画"}, {"instruction": "改", "images": ["$prev"]}, {"instruction": "再画"}],
    }
    job = wait_for_job(client.post("/api/generate", json=request).json()["job_id"])[-1]
    assert job["state"] == "failed"
    assert [(s["state"], s["error"]) for s in job["steps"]][1:] == [
        ("failed", {"raw": "Prompt outputs failed validation: Value 0 smaller than min of 1"}),
        ("skipped", None),
    ]
    assert client.get(job["steps"][0]["image"]["url"]).status_code == 200
    assert_page_accepts([("job", job)])


def test_rejected_generate_carries_the_plan(client) -> None:
    request = {"first_step": 3, "steps": [{"instruction": "画"}, {"instruction": "", "images": ["$prev"]}]}
    response = client.post("/api/generate", json=request)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "rejected" and body["message"]
    assert [s["step"] for s in body["plan"]["steps"]] == [3, 4] and not body["plan"]["ok"]
    rejected = body["plan"]["steps"][1]
    assert rejected["route"] is None and rejected["reject"]["code"] == "empty_instruction"
    assert_page_accepts([("plan", body["plan"])])


def test_unavailable_generate_is_503_with_code_message_raw(client, backend) -> None:
    backend.go_down()
    response = client.post("/api/generate", json={"steps": [{"instruction": "画"}]})
    assert response.status_code == 503
    assert response.json() == {"code": "comfy_down", "message": "后端没开（fake）", "raw": "connection refused (fake)"}
    status = client.get("/api/status").json()
    assert not status["online"] and status["routes"]["t2i"] == {"available": False, "reason": "后端没开（fake）"}
    plan = client.post("/api/plan", json={"steps": [{"instruction": "画"}]}).json()
    assert plan["steps"][0]["unavailable"]["code"] == "comfy_down"
    assert_page_accepts([("status", status), ("plan", plan)])


def test_an_upgrade_turns_generate_into_503_upgrading(client, service) -> None:
    service.version_state.set_upgrading(Upgrading(target="v0.42.5", since=AT))
    response = client.post("/api/generate", json={"steps": [{"instruction": "画"}]})
    assert response.status_code == 503 and response.json()["code"] == "upgrading"
    assert response.json()["message"] and response.json()["raw"]
    status = client.get("/api/status").json()
    assert status["upgrading"] == {"target": "v0.42.5", "since": "2026-10-26T12:00:00Z"}
    assert_page_accepts([("status", status)])


def test_status_with_version_history_as_the_page_reads_it(client, service) -> None:
    old, new = Version(tag=None, commit="0b05cd8"), Version(tag="v0.42.5", commit="483dae4")
    service.version_state.set_last_check(
        LastCheck(at=AT, version=new, items=[CheckItem(name="routes", label="三条路试跑", ok=False, note="原文")])
    )
    service.version_state.append_operation(
        Operation(at=AT, kind=OperationKind.UPGRADE, from_=old, to=new, result=OperationResult.REVERTED, note="原文")
    )
    first = client.get("/api/status").json()
    assert first == client.get("/api/status").json()  # nothing changed, nothing differs
    assert first["last_operation"]["from"] == {"tag": None, "commit": "0b05cd8"}
    assert first["version"] == {"tag": "v0.42.5", "commit": "483dae4"}
    assert_page_accepts([("status", first)])


def test_history_pages_are_exact_until_the_end(client, wait_for_job) -> None:
    request = {"steps": [{"instruction": f"画 {n}", "width": 256, "height": 256} for n in range(5)]}
    wait_for_job(client.post("/api/generate", json=request).json()["job_id"])
    upload(client, png(32, 32))  # uploads are not history
    pages, before = [], None
    while True:
        params = {"limit": 2} | ({"before": before} if before else {})
        items = client.get("/api/history", params=params).json()["items"]
        pages.append([i["instruction"] for i in items])
        if len(items) < 2:
            break
        before = items[-1]["id"]
    assert pages == [["画 4", "画 3"], ["画 2", "画 1"], ["画 0"]]


def test_the_page_and_its_static_files_are_served(client) -> None:
    page = client.get("/")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert "/static/js/main.js" in page.text
    script = client.get("/static/js/main.js")
    assert script.status_code == 200 and "javascript" in script.headers["content-type"]
    assert client.get("/static/css/theme.css").status_code == 200
