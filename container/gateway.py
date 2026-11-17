"""Ollama supervisor and streaming Open WebUI reverse proxy.

Only the WebUI is proxied publicly. The unauthenticated Ollama API stays
on the container network, with no host-published port.
"""
import asyncio
import contextlib
import json
import os
import re
from pathlib import Path

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType, web
from multidict import CIMultiDict

ROOT = Path(__file__).parent
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailer", "transfer-encoding", "upgrade", "host"}


def settings(environ):
    """Validate before starting the child, even on older workspace runtimes."""
    schema = json.loads((ROOT / "config-schema.json").read_text())
    result = {}
    for key, spec in schema["properties"].items():
        name = spec["x-env"]
        raw = environ.get(name, str(spec["default"]))
        kind = spec["type"]
        if kind == "integer":
            if not re.fullmatch(r"[0-9]+", raw):
                raise ValueError(f"{key}: expected an integer")
            value = int(raw)
            if not spec["minimum"] <= value <= spec["maximum"]:
                raise ValueError(f"{key}: outside allowed bounds")
        elif kind == "boolean":
            if raw.lower() not in {"true", "false"}:
                raise ValueError(f"{key}: expected true or false")
            value = raw.lower() == "true"
        else:
            value = raw
            if len(value) > spec.get("maxLength", 256):
                raise ValueError(f"{key}: too long")
            if "pattern" in spec and not re.fullmatch(spec["pattern"], value):
                raise ValueError(f"{key}: invalid format")
            if "enum" in spec and value not in spec["enum"]:
                raise ValueError(f"{key}: unsupported value")
        result[key] = value
    if result["kv_cache_type"] != "f16" and not result["flash_attention"]:
        raise ValueError("Quantized KV cache requires flash attention")
    return result


def child_environment(config):
    # Do not inherit arbitrary OLLAMA_* variables or command text.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OLLAMA_", "AW_OLLAMA_"))}
    schema = json.loads((ROOT / "config-schema.json").read_text())
    for key, spec in schema["properties"].items():
        if spec["x-env"].startswith("OLLAMA_"):
            value = config[key]
            env[spec["x-env"]] = str(value).lower() if isinstance(value, bool) else str(value)
    env.update(OLLAMA_HOST="0.0.0.0:11434", OLLAMA_MODELS="/root/.ollama/models")
    if config["accelerator"] == "cpu":
        env.update(CUDA_VISIBLE_DEVICES="-1", ROCR_VISIBLE_DEVICES="-1", GGML_VK_VISIBLE_DEVICES="-1")
    return env


async def read_json(app, base, path):
    try:
        async with app["session"].get(base + path, timeout=ClientTimeout(total=5)) as response:
            response.raise_for_status()
            return await response.json(), None
    except (ClientError, asyncio.TimeoutError, ValueError):
        return {}, "Unavailable; check app logs and restart the app."


def model_name(name):
    return name if ":" in name.rsplit("/", 1)[-1] else name + ":latest"


async def status(request):
    app = request.app
    (version, ollama_error), (ready, webui_error), (tags, tags_error), (ps, ps_error) = await asyncio.gather(
        read_json(app, app["ollama"], "/api/version"),
        read_json(app, app["webui"], "/ready"),
        read_json(app, app["ollama"], "/api/tags"),
        read_json(app, app["ollama"], "/api/ps"),
    )
    selected = app["config"]["default_model"]
    loaded = [{"name": m.get("name"), "size": m.get("size"),
               "size_vram": m.get("size_vram", 0)} for m in ps.get("models", [])]
    effective = ("unknown" if ps_error else "idle" if not loaded else
                 "gpu" if any(m["size_vram"] > 0 for m in loaded) else "cpu")
    ok = not (ollama_error or webui_error) and ready.get("status") is True
    result = {
        "ok": ok, "command": ["ollama", "serve"],
        "ollama": {"version": version.get("version"), "error": ollama_error},
        "webui": {"version": "0.13.4", "ready": not webui_error and ready.get("status") is True,
                  "error": webui_error},
        "default_model": selected,
        "installed": None if tags_error else bool(selected and any(
            model_name(m.get("name", "")) == model_name(selected) for m in tags.get("models", []))),
        "loaded": loaded, "models_error": tags_error or ps_error,
        "accelerator": {"requested": app["config"]["accelerator"], "effective": effective,
                        "evidence": "Ollama /api/ps size_vram; idle means no loaded model, not CPU fallback."},
        "settings": app["config"],
        "guidance": "Pull explicitly in Open WebUI Admin Settings > Models. No models download automatically. "
                    "Auto acceleration can fall back to CPU; check effective status during inference.",
    }
    return web.json_response(result, status=200 if ok or request.path.endswith("/status") else 503,
                             headers={"Cache-Control": "no-store"})


async def panel(request):
    return web.FileResponse(ROOT / "panel.html", headers={"Cache-Control": "no-store"})


def clean_headers(headers):
    excluded = HOP | {s.strip().lower() for s in headers.get("Connection", "").split(",")}
    return CIMultiDict((k, v) for k, v in headers.items() if k.lower() not in excluded)


async def relay(source, destination):
    async for message in source:
        if message.type == WSMsgType.TEXT:
            await destination.send_str(message.data)
        elif message.type == WSMsgType.BINARY:
            await destination.send_bytes(message.data)
        elif message.type in {WSMsgType.CLOSE, WSMsgType.ERROR}:
            break


async def proxy(request):
    if request.path.startswith("/workspace/"):
        raise web.HTTPNotFound()
    target = request.app["webui"] + request.rel_url.raw_path_qs
    headers = clean_headers(request.headers)
    if request.path == "/api/config":
        headers["Accept-Encoding"] = "identity"
    # External identity headers must never become implicit Open WebUI users.
    for key in list(headers):
        if key.lower().startswith(("x-forwarded-user", "x-auth-request-")):
            del headers[key]
    if request.headers.get("Upgrade", "").lower() == "websocket":
        protocols = tuple(p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip())
        for key in list(headers):
            if key.lower().startswith("sec-websocket-"):
                del headers[key]
        try:
            async with request.app["session"].ws_connect(target, headers=headers, protocols=protocols,
                                                        max_msg_size=16 * 1024 * 1024) as upstream:
                downstream = web.WebSocketResponse(protocols=[upstream.protocol] if upstream.protocol else [],
                                                   max_msg_size=16 * 1024 * 1024)
                await downstream.prepare(request)
                tasks = [asyncio.create_task(relay(upstream, downstream)),
                         asyncio.create_task(relay(downstream, upstream))]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await downstream.close()
                return downstream
        except (ClientError, asyncio.TimeoutError):
            raise web.HTTPBadGateway(text="Open WebUI is unavailable; check app logs.")
    try:
        async with request.app["session"].request(
            request.method, target, headers=headers, data=request.content.iter_any(),
            allow_redirects=False,
        ) as upstream:
            response_headers = clean_headers(upstream.headers)
            # Adapt just this response field: keep all WebUI DB-backed admin
            # settings intact, while the workspace default applies after saves.
            if request.path == "/api/config" and upstream.status == 200 and request.app["config"]["default_model"]:
                data = await upstream.json()
                if "default_models" in data:  # Only authenticated config contains this key.
                    data["default_models"] = request.app["config"]["default_model"]
                for key in ("Content-Length", "Content-Type", "Content-Encoding", "ETag"):
                    response_headers.popall(key, None)
                response_headers["Cache-Control"] = "no-store"
                return web.json_response(data, headers=response_headers)
            response = web.StreamResponse(status=upstream.status, headers=response_headers)
            await response.prepare(request)
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
            await response.write_eof()
            return response
    except (ClientError, asyncio.TimeoutError):
        raise web.HTTPBadGateway(text="Open WebUI is unavailable; check app logs.")


async def lifecycle(app):
    async with ClientSession(timeout=ClientTimeout(total=None, sock_connect=10),
                             auto_decompress=False) as session:
        app["session"] = session
        process = None
        watcher = None
        if app["supervise"]:
            process = await asyncio.create_subprocess_exec("ollama", "serve", env=child_environment(app["config"]))
            async def watch():
                await process.wait()
                # Fail the container so its normal lifecycle can recover.
                os._exit(process.returncode or 1)
            watcher = asyncio.create_task(watch())
        try:
            yield
        finally:
            if watcher:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher
            if process and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 15)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()


def create_app(environ=None, *, supervise=True, ollama="http://127.0.0.1:11434", webui="http://aw-app-ollama-webui:8080"):
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.update(config=settings(os.environ if environ is None else environ), supervise=supervise,
               ollama=ollama, webui=webui)
    app.cleanup_ctx.append(lifecycle)
    app.router.add_get("/workspace/health", status)
    app.router.add_get("/workspace/status", status)
    app.router.add_get("/workspace/panel", panel)
    app.router.add_route("*", "/{path:.*}", proxy)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=8081)
