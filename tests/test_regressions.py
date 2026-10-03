"""Regressions for HTTP output, configuration reload and request deadlines.

All upstreams are in-memory transports. No API key or external service is used.
"""
from __future__ import annotations

import asyncio
import copy
import json
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
import pytest
import yaml

from autoapi.config import ConfigError, load_config, parse_config
from autoapi.proxy import handle_request
from autoapi.server import _config_reload_loop, create_app
from autoapi.state import RuntimeState
from autoapi.upstream import try_candidate


def config_dict(**server):
    return {
        "server": {
            "reload_poll_interval": 0,
            "auto_hedge_threshold": 0,
            "stream_timeout": 5,
            "nonstream_timeout": 5,
            "stall_timeout": 5,
            "target_mode_max_wait_seconds": 1,
            "target_mode_round_interval_seconds": 5,
            **server,
        },
        "virtual_models": {
            "auto-test": [{
                "name": "mock-upstream",
                "base_url": "https://upstream.test",
                "api_key": "sk-test-placeholder",
                "model": "real-model",
            }],
        },
        "rules": [{"match": {"status": 400}, "action": "passthrough"}],
    }


@asynccontextmanager
async def proxy_client(handler, data=None, *, target=False):
    state = RuntimeState(parse_config(data or config_dict()))
    state.set_target_mode(target)
    app = create_app(state)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
        app.state.http_client = upstream
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://proxy.test",
        ) as client:
            yield client, state


@pytest.mark.parametrize("stream", [False, True])
async def test_passthrough_preserves_http_error_body_and_headers(stream):
    body = '{"error":{"message":"请求格式不对"}}'.encode()
    def handler(request):
        return httpx.Response(400, content=body, headers={
            "content-type": "application/json",
            "x-request-id": "upstream-400",
            "retry-after": "12",
        })
    async with proxy_client(handler) as (client, _):
        response = await client.post("/v1/chat/completions", json={
            "model": "auto-test", "stream": stream,
        })
    assert response.status_code == 400
    assert response.content == body
    assert response.headers["x-request-id"] == "upstream-400"
    assert response.headers["retry-after"] == "12"


@pytest.mark.parametrize("stream", [False, True])
async def test_passthrough_200_error_uses_real_http_status(stream):
    body = (b'data: {"error":{"message":"no quota"}}\n\n' if stream
            else b'{"error":{"message":"no quota"}}')
    content_type = "text/event-stream" if stream else "application/json"
    data = config_dict()
    data["rules"] = [{"match": {"status": "bad_stream"}, "action": "passthrough"}]
    async with proxy_client(
        lambda request: httpx.Response(200, content=body, headers={"content-type": content_type}),
        data,
    ) as (client, _):
        response = await client.post("/v1/chat/completions", json={
            "model": "auto-test", "stream": stream,
        })
    assert response.status_code == 200
    assert response.content == body
    assert response.headers["content-type"].startswith(content_type)


@pytest.mark.parametrize("error,status,expected", [
    (httpx.ConnectError, "network", 502),
    (httpx.ReadTimeout, "timeout", 504),
])
async def test_passthrough_network_failure_returns_valid_http_error(error, status, expected):
    data = config_dict()
    data["rules"] = [{"match": {"status": status}, "action": "passthrough"}]
    def handler(request):
        raise error("mock failure", request=request)
    async with proxy_client(handler, data) as (client, _):
        response = await client.post("/v1/chat/completions", json={"model": "auto-test"})
    assert response.status_code == expected
    assert response.json()["error"]["message"]


@pytest.mark.parametrize("field,value", [
    ("port", "not-a-number"), ("port", 8787.5),
    ("connect_timeout", "not-a-number"), ("connect_timeout", None),
    ("stall_timeout", True), ("stream_timeout", float("nan")),
    ("nonstream_timeout", float("inf")), ("reload_poll_interval", []),
    ("auto_hedge_threshold", False), ("auto_hedge_minutes", {}),
    ("metrics_window_minutes", "nan"), ("min_content_chars", 2.5),
    ("target_mode_max_wait_seconds", "inf"),
    ("target_mode_round_interval_seconds", None),
])
def test_invalid_server_numbers_raise_config_error_with_field(field, value):
    with pytest.raises(ConfigError, match=field):
        parse_config(config_dict(**{field: value}))


@pytest.mark.parametrize("field,value", [
    ("max_attempts", "bad"), ("max_attempts", True),
    ("backoff_base", "nan"), ("freeze_seconds", float("inf")),
    ("freeze_from_group", "bad"),
])
def test_invalid_rule_numbers_raise_config_error(field, value):
    data = config_dict()
    data["rules"][0][field] = value
    with pytest.raises(ConfigError, match=field):
        parse_config(data)


@pytest.mark.parametrize("field,value", [
    ("stream_timeout", float("nan")), ("stall_timeout", float("inf")),
    ("api_key", "   "), ("model", "   "), ("base_url", "   "),
    ("base_url", "file:///tmp/api"), ("base_url", "https://user:password@upstream.test"),
    ("base_url", "https://upstream.test?key=secret"),
])
def test_invalid_candidate_values_are_rejected(field, value):
    data = config_dict()
    data["virtual_models"]["auto-test"][0][field] = value
    with pytest.raises(ConfigError, match=field):
        parse_config(data)


def write_config(path: Path, data):
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


async def test_hot_reload_survives_bad_number_and_applies_next_valid_config(tmp_path):
    path = tmp_path / "config.yaml"
    data = config_dict(reload_poll_interval=0.5)
    write_config(path, data)
    state = RuntimeState(load_config(path))
    task = asyncio.create_task(_config_reload_loop(state, path))
    try:
        await asyncio.sleep(0.05)
        bad = copy.deepcopy(data)
        bad["server"]["connect_timeout"] = "broken"
        write_config(path, bad)
        await asyncio.sleep(0.6)
        assert not task.done(), "invalid numeric input killed the reload task"
        assert state.config.server.connect_timeout == 15
        data["server"]["connect_timeout"] = 9
        write_config(path, data)
        async def wait_for_valid_reload():
            while state.config.server.connect_timeout != 9:
                await asyncio.sleep(0.05)
        await asyncio.wait_for(wait_for_valid_reload(), timeout=2)
        assert not task.done()
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError, ValueError):
            await task


@pytest.mark.parametrize("action,expected", [
    ("return_504", 504), ("return_429", 429),
    ("return_502", 502), ("drop_connection", 504),
])
async def test_target_deadline_interrupts_slow_upstream(action, expected):
    cancelled = asyncio.Event()
    async def handler(request):
        try:
            await asyncio.sleep(10)
            return httpx.Response(200, json={"choices": []})
        finally:
            cancelled.set()
    data = config_dict(target_mode_timeout_action=action)
    start = time.monotonic()
    async with proxy_client(handler, data, target=True) as (client, state):
        response = await asyncio.wait_for(client.post(
            "/v1/chat/completions", json={"model": "auto-test"},
        ), timeout=3)
    assert response.status_code == expected
    assert time.monotonic() - start < 2.5
    assert cancelled.is_set()
    assert state.total_exhausted == 1
    assert state.snapshot_virtual_model_health("auto-test").all_time.total == 1


async def test_target_deadline_bounds_round_sleep_and_does_not_start_extra_round():
    hits = []
    def handler(request):
        hits.append(request.url.host)
        return httpx.Response(503, text="unavailable")
    start = time.monotonic()
    async with proxy_client(handler, target=True) as (client, _):
        response = await asyncio.wait_for(client.post(
            "/v1/chat/completions", json={"model": "auto-test"},
        ), timeout=3)
    assert response.status_code == 504
    assert hits == ["upstream.test"]
    assert time.monotonic() - start < 2.5


async def test_target_deadline_includes_retry_backoff():
    hits = []
    data = config_dict()
    data["rules"] = [{"match": {"status": 503}, "action": "retry",
                      "max_attempts": 3, "backoff_base": 3}]
    def handler(request):
        hits.append(request.url.host)
        return httpx.Response(503, text="unavailable")
    async with proxy_client(handler, data, target=True) as (client, _):
        response = await asyncio.wait_for(client.post(
            "/v1/chat/completions", json={"model": "auto-test"},
        ), timeout=3)
    assert response.status_code == 504
    assert hits == ["upstream.test"]


class HangingStream(httpx.AsyncByteStream):
    def __init__(self, prefix=b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'):
        self.prefix = prefix
        self.started = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        yield self.prefix
        self.started.set()
        await asyncio.sleep(10)

    async def aclose(self):
        self.closed = True


async def test_cancelled_stream_probe_closes_upstream_response():
    stream = HangingStream()
    config = parse_config(config_dict())
    candidate = config.virtual_models["auto-test"][0]
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream),
    )) as client:
        task = asyncio.create_task(try_candidate(
            client, candidate, "POST", "v1/chat/completions", "", {},
            {"model": "auto-test", "stream": True}, True, config.server,
        ))
        try:
            await asyncio.wait_for(stream.started.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert stream.closed
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def test_target_deadline_closes_pending_stream_probe():
    stream = HangingStream()
    async with proxy_client(
        lambda request: httpx.Response(200, stream=stream), target=True,
    ) as (client, _):
        response = await asyncio.wait_for(client.post(
            "/v1/chat/completions", json={"model": "auto-test", "stream": True},
        ), timeout=3)
    assert response.status_code == 504
    assert stream.closed


async def test_stream_total_budget_includes_waiting_for_headers():
    async def handler(request):
        await asyncio.sleep(10)
        return httpx.Response(200, content=b'data: [DONE]\n\n')
    start = time.monotonic()
    async with proxy_client(handler, config_dict(stream_timeout=1)) as (client, _):
        response = await asyncio.wait_for(client.post(
            "/v1/chat/completions", json={"model": "auto-test", "stream": True},
        ), timeout=3)
    assert response.status_code == 502
    assert time.monotonic() - start < 2.5


async def test_stream_total_budget_bounds_error_body_read():
    stream = HangingStream(b'{"error":')
    start = time.monotonic()
    async with proxy_client(
        lambda request: httpx.Response(429, stream=stream),
        config_dict(stream_timeout=1),
    ) as (client, _):
        response = await asyncio.wait_for(client.post(
            "/v1/chat/completions", json={"model": "auto-test", "stream": True},
        ), timeout=3)
    assert response.status_code == 502
    assert time.monotonic() - start < 2.5
    assert stream.closed


@pytest.mark.parametrize("base,path,expected", [
    ("https://upstream.test", "v1/chat/completions", "/v1/chat/completions"),
    ("https://upstream.test/v1", "v1/chat/completions", "/v1/chat/completions"),
    ("https://upstream.test/openai/v1", "v1/responses", "/openai/v1/responses"),
    ("https://upstream.test/api", "v1/messages", "/api/v1/messages"),
])
async def test_base_url_version_prefix_is_not_duplicated(base, path, expected):
    data = config_dict()
    data["virtual_models"]["auto-test"][0]["base_url"] = base
    urls = []
    def handler(request):
        urls.append(request.url)
        return httpx.Response(200, json={"ok": True})
    state = RuntimeState(parse_config(data))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await handle_request(client, state, "POST", path, "beta=1", {},
                                       json.dumps({"model": "auto-test"}).encode())
    assert outcome.success
    assert urls[0].path == expected
    assert urls[0].query == b"beta=1"


def test_legacy_drop_connection_is_normalized_with_warning():
    config = parse_config(config_dict(target_mode_timeout_action="drop_connection"))
    assert config.server.target_mode_timeout_action == "return_504"
    assert any("drop_connection" in warning and "504" in warning for warning in config.warnings)


def test_atomic_config_save_failure_keeps_file_and_memory_unchanged(tmp_path, monkeypatch):
    from autoapi.repl import Repl
    path = tmp_path / "config.yaml"
    write_config(path, config_dict())
    original = path.read_bytes()
    state = RuntimeState(load_config(path))
    old_config = state.config
    repl = Repl(state)
    def fail_replace(source, destination):
        assert Path(source).parent == path.parent
        assert Path(destination) == path
        raise OSError("mock disk error")
    monkeypatch.setattr("autoapi.repl.os.replace", fail_replace)
    with pytest.raises(ConfigError, match="保存"):
        repl._mutate_config(lambda data: data["server"].update(connect_timeout=9))
    assert state.config is old_config
    assert path.read_bytes() == original
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_config_save_publishes_complete_valid_yaml(tmp_path, monkeypatch):
    from autoapi.repl import Repl
    import os
    path = tmp_path / "config.yaml"
    write_config(path, config_dict())
    state = RuntimeState(load_config(path))
    real_replace = os.replace
    replacements = []
    def checked_replace(source, destination):
        assert state.config.server.connect_timeout == 15
        assert load_config(source).server.connect_timeout == 9
        replacements.append((source, destination))
        return real_replace(source, destination)
    monkeypatch.setattr("autoapi.repl.os.replace", checked_replace)
    Repl(state)._mutate_config(lambda data: data["server"].update(connect_timeout=9))
    assert len(replacements) == 1
    assert state.config.server.connect_timeout == 9
    assert load_config(path).server.connect_timeout == 9
    assert list(tmp_path.glob("*.tmp")) == []


def test_log_filter_redacts_message_and_exception():
    import logging
    import sys
    from autoapi.security import SecretRedactingFilter
    config = parse_config(config_dict())
    key = config.virtual_models["auto-test"][0].api_key
    try:
        raise ValueError(f"upstream echoed {key}")
    except ValueError:
        record = logging.LogRecord("autoapi.test", logging.ERROR, __file__, 1,
                                   "key=%s", (key,), sys.exc_info())
    SecretRedactingFilter(lambda: config).filter(record)
    rendered = logging.Formatter("%(message)s").format(record)
    assert key not in rendered
    assert "***" in rendered
    assert "ValueError" in rendered


def test_log_filter_remembers_keys_after_config_reload():
    import logging
    from autoapi.security import SecretRedactingFilter
    state = RuntimeState(parse_config(config_dict()))
    old_key = state.config.virtual_models["auto-test"][0].api_key
    redactor = SecretRedactingFilter(lambda: state.config)
    redactor.filter(logging.LogRecord("test", logging.INFO, __file__, 1, "ready", (), None))
    changed = config_dict()
    new_key = "sk-new-test-placeholder"
    changed["virtual_models"]["auto-test"][0]["api_key"] = new_key
    state.replace_config(parse_config(changed))
    record = logging.LogRecord("test", logging.ERROR, __file__, 1,
                               "old=%s new=%s", (old_key, new_key), None)
    redactor.filter(record)
    assert old_key not in record.getMessage()
    assert new_key not in record.getMessage()


async def test_proxy_generated_errors_do_not_echo_upstream_key():
    key = config_dict()["virtual_models"]["auto-test"][0]["api_key"]
    async with proxy_client(lambda request: httpx.Response(
        401, json={"error": {"message": f"invalid key: {key}"}},
    )) as (client, _):
        response = await client.post("/v1/chat/completions", json={"model": "auto-test"})
    assert response.status_code == 502
    assert key not in response.text
    assert "***" in response.text


async def test_target_deadline_does_not_cut_off_a_released_healthy_stream():
    first = b'data: {"choices":[{"delta":{"content":"0123456789"}}]}\n\n'
    class HealthyLongStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield first
            await asyncio.sleep(1.1)
            yield b'data: [DONE]\n\n'
    start = time.monotonic()
    async with proxy_client(lambda request: httpx.Response(
        200, stream=HealthyLongStream(), headers={"content-type": "text/event-stream"},
    ), target=True) as (client, _):
        response = await client.post("/v1/chat/completions", json={
            "model": "auto-test", "stream": True,
        })
    assert response.status_code == 200
    assert response.content == first + b'data: [DONE]\n\n'
    # Assert the contract (beyond the 1s target budget), not exact sleep duration.
    assert time.monotonic() - start > 1.0


async def test_post_release_read_error_is_visible_over_real_http():
    """ASGITransport alone cannot prove that an incomplete HTTP body is aborted."""
    import socket
    import uvicorn
    first = b'data: {"choices":[{"delta":{"content":"0123456789"}}]}\n\n'
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield first
            await asyncio.sleep(0.05)
            raise httpx.ReadError("mock upstream disconnected after release")
    state = RuntimeState(parse_config(config_dict()))
    app = create_app(state)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=BrokenStream(),
                                      headers={"content-type": "text/event-stream"}),
    )) as upstream:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            listener.setblocking(False)
            port = listener.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(
                app, host="127.0.0.1", port=port, log_level="critical", access_log=False, ws="none",
            ))
            task = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                async def await_start():
                    while not server.started:
                        if task.done():
                            await task
                            raise RuntimeError("test server exited before startup")
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(await_start(), timeout=3)
                app.state.http_client = upstream
                collected = b""
                async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
                    async with client.stream("POST", f"http://127.0.0.1:{port}/v1/chat/completions",
                                             json={"model": "auto-test", "stream": True}) as response:
                        assert response.status_code == 200
                        with pytest.raises(httpx.RemoteProtocolError):
                            async for chunk in response.aiter_bytes():
                                collected += chunk
                assert collected == first
                assert state.snapshot_candidate_health(state.get_chain("auto-test")[0]).all_time.success == 0
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, timeout=3)


async def test_stream_buffering_headers_override_upstream_without_duplicates():
    body = b'data: {"choices":[{"delta":{"content":"0123456789"}}]}\n\ndata: [DONE]\n\n'
    async with proxy_client(lambda request: httpx.Response(200, content=body, headers={
        "content-type": "text/event-stream",
        "cache-control": "public, max-age=60",
        "x-accel-buffering": "yes",
    })) as (client, _):
        response = await client.post("/v1/chat/completions", json={
            "model": "auto-test", "stream": True,
        })
    assert response.status_code == 200
    assert response.content == body
    assert response.headers.get_list("cache-control") == ["no-cache"]
    assert response.headers.get_list("x-accel-buffering") == ["no"]
