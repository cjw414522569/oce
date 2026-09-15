"""ApiCallMetricsMiddleware 单测：记账 endpoint/method/status/latency，豁免 /health，旁路容错。"""
from __future__ import annotations

import httpx
from fastapi import FastAPI, HTTPException, Request

from oce.api.middleware import ApiCallMetricsMiddleware
from oce.shared.metrics import ApiCallRecord


class _RecordingSink:
    def __init__(self) -> None:
        self.calls: list[ApiCallRecord] = []

    def record_api_call(self, record: ApiCallRecord) -> None:
        self.calls.append(record)


def _build_app(sink_provider):
    app = FastAPI()
    app.add_middleware(ApiCallMetricsMiddleware, sink_provider=sink_provider)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/items/{item_id}")
    async def item(item_id: str):
        return {"id": item_id}

    @app.get("/bad")
    async def bad():
        raise HTTPException(status_code=400, detail="nope")

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kaboom")

    return app


def _client(app, *, raise_app_exceptions: bool = True) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    return httpx.AsyncClient(transport=transport, base_url="http://t")


async def test_records_route_template_method_status_latency():
    sink = _RecordingSink()
    async with _client(_build_app(lambda: sink)) as client:
        resp = await client.get("/items/42")

    assert resp.status_code == 200
    assert len(sink.calls) == 1
    rec = sink.calls[0]
    assert rec.method == "GET"
    assert rec.status_code == 200
    assert rec.endpoint == "/items/{item_id}"  # 路由模板，非具体 id，聚合才不炸维度
    assert rec.latency_ms >= 0
    assert rec.error_type is None


async def test_health_is_exempt():
    sink = _RecordingSink()
    async with _client(_build_app(lambda: sink)) as client:
        await client.get("/health")

    assert sink.calls == []


async def test_error_response_status_is_recorded():
    sink = _RecordingSink()
    async with _client(_build_app(lambda: sink)) as client:
        resp = await client.get("/bad")

    assert resp.status_code == 400
    assert sink.calls[0].status_code == 400
    assert sink.calls[0].endpoint == "/bad"


async def test_unhandled_exception_records_500_and_error_type():
    sink = _RecordingSink()
    async with _client(_build_app(lambda: sink), raise_app_exceptions=False) as client:
        resp = await client.get("/boom")

    assert resp.status_code == 500
    assert len(sink.calls) == 1
    assert sink.calls[0].status_code == 500
    assert sink.calls[0].error_type == "RuntimeError"


async def test_none_sink_provider_skips_silently():
    async with _client(_build_app(lambda: None)) as client:
        resp = await client.get("/items/1")

    assert resp.status_code == 200  # provider 返回 None：不记账也不报错


async def test_user_id_taken_from_scope_dict():
    # 身份由依赖写入 scope（BaseHTTPMiddleware 跨任务可见），中间件负责透传
    sink = _RecordingSink()

    def provider():
        return sink

    app = _build_app(provider)

    @app.get("/who")
    async def who(request: Request):
        request.scope["oce_user_id"] = 42
        return {"ok": True}

    async with _client(app) as client:
        resp = await client.get("/who")
    assert resp.status_code == 200
    rec = sink.calls[-1]
    assert rec.user_id == 42


async def test_auth_prefix_exempt_from_metrics():
    sink = _RecordingSink()
    app = _build_app(lambda: sink)

    @app.get("/auth/login")
    async def auth_login():
        return {"ok": True}

    async with _client(app) as client:
        resp = await client.get("/auth/login")
    assert resp.status_code == 200
    assert sink.calls == []


async def test_tagged_static_requests_skipped(tmp_path):
    # Mount.matches 不设置 scope["route"]，靠 TaggedStaticFiles 打标跳过
    import os

    from oce.api.middleware import TaggedStaticFiles

    (tmp_path / "index.html").write_text("<html>x</html>")
    sink = _RecordingSink()
    app = _build_app(lambda: sink)
    app.mount("/", TaggedStaticFiles(directory=str(tmp_path), html=True), name="portal")

    async with _client(app) as client:
        assert (await client.get("/")).status_code == 200

    assert sink.calls == []
