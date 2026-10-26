"""Every error body is the contract's {code, message, raw?}: refusals, validation errors, 404, 405 and 500 alike."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest
import uvicorn
from biz_fakes import FakeBackend, encode, make_service
from fastapi import FastAPI
from fastapi.testclient import TestClient
from imagegen.biz.models import ErrorBody, ErrorCode
from PIL import Image
from pydantic import ValidationError

from image_api.web.app import create_app

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10"/></svg>'


def assert_contract_error(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert "detail" not in body
    assert body["code"] == code and isinstance(body["message"], str) and body["message"]
    assert body["code"] in set(ErrorCode)
    assert set(body) <= {"code", "message", "raw", "plan"}
    return body


@pytest.mark.parametrize(
    "data",
    [
        encode(Image.new("RGB", (8, 8)), "GIF"),
        SVG,
        b"\x00\x00\x00\x18ftypheic",  # a HEIC header
        b"not an image at all",
        b"",
    ],
    ids=["gif", "svg", "heic", "text", "empty"],
)
def test_uploads_that_are_not_png_jpeg_webp_are_bad_image(client, data) -> None:
    for filename in ("picture.png", "picture"):  # the file name does not matter
        body = assert_contract_error(client.post("/api/images", files={"file": (filename, data)}), 422, "bad_image")
        assert body["raw"]


def test_request_validation_errors_use_the_contract_body(client) -> None:
    assert_contract_error(client.post("/api/plan", json={"steps": []}), 422, "invalid_request")
    assert_contract_error(
        client.post("/api/plan", content=b"{not json", headers={"content-type": "application/json"}),
        422,
        "invalid_request",
    )
    body = assert_contract_error(
        client.post("/api/generate", json={"steps": [{"instruction": "x", "seed": "abc"}]}), 422, "invalid_request"
    )
    assert "seed" in body["message"]
    assert_contract_error(
        client.post("/api/plan", json={"steps": [{"instruction": "x", "colour": 1}]}), 422, "invalid_request"
    )
    assert_contract_error(client.post("/api/images"), 422, "invalid_request")
    broken_multipart = {"content-type": "multipart/form-data; boundary=xyz"}
    assert_contract_error(
        client.post("/api/images", content=b"garbage", headers=broken_multipart), 400, "invalid_request"
    )
    assert_contract_error(client.get("/api/history", params={"limit": "many"}), 422, "invalid_request")
    assert_contract_error(client.get("/api/history", params={"limit": 0}), 422, "invalid_request")
    assert_contract_error(client.get("/api/history", params={"before": "../etc"}), 422, "invalid_request")


def test_not_found_and_wrong_method_use_the_contract_body(client) -> None:
    assert_contract_error(client.get("/api/nothing-here"), 404, "not_found")
    assert_contract_error(client.get("/static/js/missing.js"), 404, "not_found")
    assert_contract_error(client.get("/api/images/img_0000000000000000000000"), 404, "not_found")
    assert_contract_error(client.get("/api/images/..%2F..%2Fsecret"), 404, "not_found")
    assert_contract_error(client.get("/api/jobs/job_missing"), 404, "not_found")
    assert_contract_error(client.get("/api/generate"), 405, "method_not_allowed")
    assert_contract_error(client.delete("/api/status"), 405, "method_not_allowed")


class BrokenStatusBackend(FakeBackend):
    async def status(self):
        raise RuntimeError("the backend answered something unreadable")


def test_an_unexpected_failure_is_a_500_contract_body(tmp_path) -> None:
    app = create_app(make_service(BrokenStatusBackend(), tmp_path))
    with TestClient(app) as client:  # would re-raise here if the exception escaped the app after the 500
        body = assert_contract_error(client.get("/api/status"), 500, "internal_error")
        assert "unreadable" in body["raw"]


@contextmanager
def served(app: FastAPI) -> Iterator[str]:
    """app on a real local socket (uvicorn in a thread); yields its base URL."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_a_500_leaves_the_keep_alive_connection_usable(tmp_path) -> None:
    with served(create_app(make_service(BrokenStatusBackend(), tmp_path))) as url:
        for _ in range(5):
            with httpx.Client(base_url=url) as client:  # keep-alive: both requests share one connection
                assert_contract_error(client.get("/api/status"), 500, "internal_error")
                assert client.get("/api/history").status_code == 200


def test_error_bodies_take_only_the_contracts_codes() -> None:
    assert {c.value for c in ErrorCode} == {
        "rejected",
        "bad_image",
        "invalid_request",
        "not_found",
        "method_not_allowed",
        "internal_error",
        "comfy_down",
        "missing_file",
        "missing_node",
        "upgrading",
    }
    with pytest.raises(ValidationError):
        ErrorBody(code="detail", message="x")
