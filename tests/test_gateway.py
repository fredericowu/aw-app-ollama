import asyncio
import gzip
import json
from pathlib import Path

import pytest
from aiohttp import ClientSession, WSMsgType, web
from aiohttp.test_utils import TestServer
from jsonschema import Draft202012Validator

from gateway import child_environment, create_app, settings

ROOT = Path(__file__).parents[1]


def test_manifest_and_runtime_share_one_schema():
    manifest = json.loads((ROOT / "aw-app.json").read_text())
    schema = json.loads((ROOT / "container/config-schema.json").read_text())
    assert manifest["config_schema"] == schema
    Draft202012Validator(schema).validate(settings({}))
    for key, spec in schema["properties"].items():
        assert manifest["runtime"]["env"][spec["x-env"]] == "${config." + key + "}"


@pytest.mark.parametrize("env", [
    {"AW_OLLAMA_DEFAULT_MODEL": "foo; touch /tmp/pwn"},
    {"AW_OLLAMA_DEFAULT_MODEL": "../../foo"},
    {"AW_OLLAMA_DEFAULT_MODEL": "a" * 129},
    {"OLLAMA_NUM_PARALLEL": "0"}, {"OLLAMA_NUM_PARALLEL": "5"},
    {"OLLAMA_NUM_PARALLEL": "1.0"}, {"OLLAMA_MAX_QUEUE": "99999"},
    {"OLLAMA_KEEP_ALIVE": "-1"}, {"OLLAMA_KEEP_ALIVE": "100m"},
    {"OLLAMA_LOAD_TIMEOUT": "forever"}, {"OLLAMA_FLASH_ATTENTION": "maybe"},
    {"OLLAMA_KV_CACHE_TYPE": "q4_0"}, {"AW_OLLAMA_ACCELERATOR": "nvidia;evil"},
])
def test_invalid_runtime_rejected_before_launch(env):
    with pytest.raises(ValueError):
        settings(env)


def test_fixed_environment_and_cpu_choice(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "evil:11434")
    monkeypatch.setenv("OLLAMA_RUNNERS_DIR", "/malicious")
    config = settings({"AW_OLLAMA_ACCELERATOR": "cpu"})
    env = child_environment(config)
    assert env["OLLAMA_HOST"] == "0.0.0.0:11434"
    assert env["OLLAMA_NO_CLOUD"] == "true"
    assert env["CUDA_VISIBLE_DEVICES"] == "-1"
    assert "OLLAMA_RUNNERS_DIR" not in env
    assert "AW_OLLAMA_ACCELERATOR" not in env


def test_streaming_proxy_status_and_websockets():
    async def scenario():
        state = {"ready": True, "models": [], "vram": 0}

        async def ollama(request):
            data = {"/api/version": {"version": "0.40.16"},
                    "/api/tags": {"models": [{"name": "smollm2:135m"}]},
                    "/api/ps": {"models": state["models"]}}
            return web.json_response(data[request.path])

        async def ui(request):
            if request.path == "/ready":
                return web.json_response({"status": state["ready"]}, status=200 if state["ready"] else 503)
            if request.path == "/api/config":
                assert request.headers.get("Accept-Encoding") == "identity"
                return web.json_response({"default_models": "old", "features": {"auth": True}},
                                         headers={"Set-Cookie": "test=ok; HttpOnly"})
            if request.path == "/ws":
                ws = web.WebSocketResponse()
                await ws.prepare(request)
                async for msg in ws:
                    if msg.type == WSMsgType.TEXT:
                        await ws.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await ws.send_bytes(msg.data)
                return ws
            if request.path == "/stream":
                response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
                await response.prepare(request)
                await response.write(b"data: first\n\n")
                await asyncio.sleep(0.05)
                await response.write(b"data: second\n\n")
                return response
            if request.path == "/compressed":
                return web.Response(body=gzip.compress(b"hello"), headers={"Content-Encoding": "gzip"})
            if request.path == "/redirect":
                raise web.HTTPFound("/auth")
            return web.Response(body=await request.read(), headers={"Set-Cookie": "session=test; HttpOnly"})

        ollama_app, ui_app = web.Application(), web.Application()
        ollama_app.router.add_route("*", "/{path:.*}", ollama)
        ui_app.router.add_route("*", "/{path:.*}", ui)
        async with TestServer(ollama_app) as engine, TestServer(ui_app) as webui:
            app = create_app({"AW_OLLAMA_DEFAULT_MODEL": "smollm2:135m"}, supervise=False,
                             ollama=str(engine.make_url("")).rstrip("/"),
                             webui=str(webui.make_url("")).rstrip("/"))
            async with TestServer(app) as gateway, ClientSession() as client:
                async with client.get(gateway.make_url("/workspace/health")) as response:
                    data = await response.json()
                    assert response.status == 200 and data["ok"]
                    assert data["installed"] is True
                    assert data["accelerator"]["effective"] == "idle"
                for vram, expected in [(0, "cpu"), (10, "gpu")]:
                    state["models"] = [{"name": "smollm2:135m", "size": 100, "size_vram": vram}]
                    async with client.get(gateway.make_url("/workspace/status")) as response:
                        assert (await response.json())["accelerator"]["effective"] == expected
                async with client.get(gateway.make_url("/api/config")) as response:
                    assert (await response.json()) == {"default_models": "smollm2:135m", "features": {"auth": True}}
                    assert "test=ok" in response.headers["Set-Cookie"]
                async with client.post(gateway.make_url("/echo?q=1"), data=b"body") as response:
                    assert await response.read() == b"body"
                    assert "session=test" in response.headers["Set-Cookie"]
                async with client.get(gateway.make_url("/compressed")) as response:
                    assert await response.read() == b"hello"
                async with client.get(gateway.make_url("/redirect"), allow_redirects=False) as response:
                    assert response.status == 302 and response.headers["Location"] == "/auth"
                async with client.get(gateway.make_url("/stream")) as response:
                    assert await response.content.readline() == b"data: first\n"
                    assert b"data: second" in await response.read()
                async with client.ws_connect(gateway.make_url("/ws")) as ws:
                    await ws.send_str("hello")
                    assert (await ws.receive()).data == "hello"
                    await ws.send_bytes(b"binary")
                    assert (await ws.receive()).data == b"binary"
                state["ready"] = False
                async with client.get(gateway.make_url("/workspace/health")) as response:
                    assert response.status == 503 and not (await response.json())["ok"]
                async with client.get(gateway.make_url("/workspace/unknown")) as response:
                    assert response.status == 404
    asyncio.run(scenario())


def test_unavailable_services_and_proxy_failure():
    async def scenario():
        app = create_app({}, supervise=False, ollama="http://127.0.0.1:1", webui="http://127.0.0.1:1")
        async with TestServer(app) as gateway, ClientSession() as client:
            async with client.get(gateway.make_url("/workspace/health")) as response:
                assert response.status == 503
                data = await response.json()
                assert data["accelerator"]["effective"] == "unknown"
                assert data["installed"] is None
            async with client.get(gateway.make_url("/")) as response:
                assert response.status == 502
    asyncio.run(scenario())
