"""The Attention Lab connector, end to end against a fake Attention Lab."""

import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
from mcp import Client

from conftest import admin_headers

import pacer_base.heatmap_mcp as heatmap_mcp

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 500

REVIEW = {
    "status": "complete",
    "summary": "The hero image draws most attention; the price is easy to miss.",
    "landingRecommendations": ["Move the price next to the add to bag button"],
    "overallRecommendations": ["Bring reviews above the fold"],
    "contentReview": {"clarityAssessment": "Clear product name.", "strengths": ["Strong imagery"]},
}


class FakeLab:
    """Just enough of Attention Lab's API."""

    def __init__(self):
        self.created = []
        self.batches = []
        self.polls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer ahm_service_token"
        path = request.url.path
        if request.method == "POST" and path == "/api/analyses":
            self.created.append(json.loads(request.content))
            return httpx.Response(202, json={"id": "a1", "sharePath": "/s/tok1"})
        if request.method == "POST" and path == "/api/analyses/batch":
            body = json.loads(request.content)
            self.batches.append(body)
            ids = [f"b{i}" for i, _ in enumerate(body["urls"])]
            return httpx.Response(202, json={"ids": ids, "sharePaths": [f"/s/t{i}" for i in ids], "count": len(ids)})
        if request.method == "GET" and path == "/api/analyses/a1":
            self.polls += 1
            done = self.polls > 1
            return httpx.Response(200, json={
                "id": "a1", "url": "https://acme.example/", "client": "acme", "sharePath": "/s/tok1",
                "status": "complete" if done else "processing",
                "desktop": {"compositePath": "/uploads/a1/desktop.jpg"} if done else None,
                "mobile": {"compositePath": "/uploads/a1/mobile.jpg"} if done else None,
                "aiReview": REVIEW if done else {"status": "processing"},
                "capture": {"country": "GB", "network": "server", "popupsCleared": ["cookie banner (accepted)"]} if done else None,
            })
        if request.method == "GET" and path.startswith("/uploads/a1/"):
            return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        if request.method == "GET" and path == "/api/analyses":
            return httpx.Response(200, json=[{"id": "a1", "url": "https://acme.example/", "status": "complete", "createdAt": "2026-11-12T10:00:00Z"}])
        if request.method == "GET" and path == "/api/analyses/missing":
            return httpx.Response(404, json={"error": "Analysis not found"})
        return httpx.Response(404, json={"error": "nope"})


@pytest.fixture
def lab_server(settings, monkeypatch):
    import dataclasses

    import uvicorn

    from pacer_base.app import create_app

    monkeypatch.setattr(heatmap_mcp, "POLL_SECONDS", 0.01)
    fake = FakeLab()
    s = dataclasses.replace(settings, attention_lab_url="https://lab.example", attention_lab_token="ahm_service_token")
    app = create_app(s, attention_lab_transport=httpx.MockTransport(fake.handler))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    httpx.post(f"{base}/api/clients", json={"name": "Acme", "aliases": ["AC"], "domains": ["acme.example"]}, headers=admin_headers())
    yield base, fake
    server.should_exit = True
    thread.join(timeout=10)


def key(base, scope="write"):
    r = httpx.post(f"{base}/api/tokens", json={"label": f"{scope}-key", "scope": scope}, headers=admin_headers())
    return r.json()["token"]


def test_run_heatmap_waits_and_returns_review_and_images(lab_server):
    base, fake = lab_server

    async def flow():
        async with Client(f"{base}/heatmap/mcp/{key(base)}") as c:
            tools = {t.name for t in (await c.list_tools()).tools}
            assert tools == {"run_heatmap", "run_heatmaps", "get_heatmap", "list_heatmaps"}
            r = await c.call_tool("run_heatmap", {"url": "https://acme.example/", "client": "AC"})
            assert not r.is_error
            types = [b.type for b in r.content]
            assert types.count("image") == 2 and types[0] == "text"
            text = r.content[0].text
            assert "the price is easy to miss" in text and "Move the price" in text and "Clarity" in text
            assert "https://lab.example/s/tok1" in text and "Logged to Pacer Base for acme" in text
            assert "from a server outside the UK" in text and "Cleared first: cookie banner (accepted)." in text
            assert fake.created[-1] == {"url": "https://acme.example/", "client": "acme"}

            r = await c.call_tool("get_heatmap", {"heatmap_id": "a1", "include_images": False})
            assert [b.type for b in r.content] == ["text"]
            r = await c.call_tool("get_heatmap", {"heatmap_id": "missing"})
            assert r.is_error and "404" in r.content[0].text

            r = await c.call_tool("run_heatmaps", {"urls": ["https://acme.example/a", "https://acme.example/b"]})
            assert "Started 2" in r.content[0].text and "https://lab.example/s/tb0" in r.content[0].text
            assert fake.batches[-1] == {"urls": ["https://acme.example/a", "https://acme.example/b"]}
            r = await c.call_tool("list_heatmaps", {})
            assert "`a1`" in r.content[0].text

    asyncio.run(flow())


def test_still_running_and_permissions(lab_server):
    base, fake = lab_server

    async def flow():
        async with Client(f"{base}/heatmap/mcp/{key(base)}") as c:
            r = await c.call_tool("run_heatmap", {"url": "https://acme.example/", "wait_seconds": 0})
            assert "Still working on it" in r.content[0].text and "Logged to Pacer Base" not in r.content[0].text
            r = await c.call_tool("run_heatmap", {"url": "https://x.example/", "client": "Nobody"})
            assert r.is_error and "No client matches" in r.content[0].text
        async with Client(f"{base}/heatmap/mcp/{key(base, 'read')}") as c:
            r = await c.call_tool("run_heatmap", {"url": "https://acme.example/"})
            assert r.is_error and "write access" in r.content[0].text
            r = await c.call_tool("list_heatmaps", {})
            assert not r.is_error
    asyncio.run(flow())
    assert httpx.post(f"{base}/heatmap/mcp/pb_wrong", json={}).status_code == 401
    r = httpx.get(f"{base}/.well-known/oauth-protected-resource/heatmap/mcp/{key(base)}")
    assert r.status_code == 200 and r.json()["resource_name"] == "Attention Lab"


def test_not_configured(api):
    """Without ATTENTION_LAB_* settings the tools explain what's missing."""
    from fastapi.testclient import TestClient  # noqa: F401
    tok = key_from_api(api)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_heatmaps", "arguments": {}}}
    r = api.post(f"/heatmap/mcp/{tok}", json=body, headers={"Accept": "application/json, text/event-stream", "mcp-protocol-version": "2025-06-18"})
    assert r.status_code == 200 and "isn't connected yet" in r.json()["result"]["content"][0]["text"]


def key_from_api(api):
    return api.post("/api/tokens", json={"label": "k", "scope": "write"}, headers=admin_headers()).json()["token"]


def test_capture_line():
    from pacer_base.heatmap_mcp import _capture_line

    assert _capture_line(None) is None
    assert _capture_line({"country": "GB", "network": "proxy"}) == "Captured as a visitor in the UK, through a network address there."
    line = _capture_line({"country": "GB", "network": "server", "popupsCleared": ["a", "b"]})
    assert line.startswith("Captured with UK browser settings but from a server outside the UK") and line.endswith("Cleared first: a; b.")
