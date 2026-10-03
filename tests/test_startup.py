"""Offline startup checks: dummy credentials and owned localhost processes only."""
from __future__ import annotations

import argparse
import errno
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

import main as entry
from autoapi import listener
from autoapi.config import parse_config
from autoapi.repl import Repl
from autoapi.state import RuntimeState

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = sys.platform == "win32"
WINDOWS_ONLY = pytest.mark.skipif(not WINDOWS, reason="Windows launcher integration")
POWERSHELL = (Path(os.environ.get("SystemRoot", "C:/Windows")) /
              "System32/WindowsPowerShell/v1.0/powershell.exe")
CMD = Path(os.environ.get("ComSpec", "C:/Windows/System32/cmd.exe"))
ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1"}


def config_data(host="127.0.0.1", port=18787):
    return {"server": {"host": host, "port": port, "reload_poll_interval": 0},
            "virtual_models": {"auto-test": [{"name": "offline-only",
                "base_url": "https://never-contact.invalid", "api_key": "sk-startup-dummy-secret",
                "model": "offline-model"}]}, "rules": []}


def get_available_port(host="127.0.0.1"):
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def assert_port_released(host, port):
    for _ in range(100):
        try:
            sockets = listener.bind_listeners(host, port)
        except OSError:
            time.sleep(0.05)
        else:
            for sock in sockets:
                sock.close()
            return
    pytest.fail(f"Owned test listener still occupies {host}:{port}")


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", "http://127.0.0.1:18787"),
    ("localhost", "http://localhost:18787"),
    ("::1", "http://[::1]:18787"),
])
def test_displayed_url_is_client_usable(host, expected):
    assert listener.server_url(host, 18787) == expected


@pytest.mark.parametrize("code,text", [
    (errno.EADDRINUSE, "已被占用"), (10048, "已被占用"),
    (errno.EACCES, "系统禁止绑定"), (10013, "系统禁止绑定"),
    (errno.EADDRNOTAVAIL, "custom-failure"),
])
def test_bind_error_is_actionable(code, text):
    message = listener.bind_error_message("127.0.0.1", 18787, OSError(code, "custom-failure"))
    assert text in message
    assert "http://127.0.0.1:18787" in message
    assert "server.port" in message
    assert "不会自动换端口" in message


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_real_sockets_reserve_and_release_configured_address(host):
    if host == "::1":
        try:
            get_available_port(host)
        except OSError:
            pytest.skip("IPv6 loopback unavailable")
    port = get_available_port(host)
    sockets = listener.bind_listeners(host, port)
    try:
        assert sockets
        assert all(s.getsockname()[1] == port for s in sockets)
        assert all(not s.getblocking() for s in sockets)
        assert all(s.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) for s in sockets)
        if WINDOWS:
            assert all(s.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE) for s in sockets)
        with pytest.raises(OSError):
            listener.bind_listeners(host, port)
    finally:
        for s in sockets:
            s.close()
    assert_port_released(host, port)


class FakeSocket:
    def __init__(self, fail=None):
        self.fail = fail
        self.closed = False
        self.options = []
        self.bound = None

    def setsockopt(self, *args):
        self.options.append(args)

    def bind(self, address):
        if self.fail is not None:
            raise self.fail
        self.bound = address

    def listen(self, backlog):
        assert backlog == 2048

    def setblocking(self, value):
        assert value is False

    def close(self):
        self.closed = True


def resolved_addresses():
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 18787)),
            (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("::1", 18787, 0, 0))]


@pytest.mark.parametrize("code", [errno.EADDRINUSE, errno.EACCES])
def test_partial_bind_failure_closes_every_reservation(monkeypatch, code):
    first, second = FakeSocket(), FakeSocket(OSError(code, "blocked"))
    sockets = iter([first, second])
    monkeypatch.setattr(listener.socket, "getaddrinfo", lambda *a: resolved_addresses())
    monkeypatch.setattr(listener.socket, "socket", lambda *a: next(sockets))
    with pytest.raises(OSError):
        listener.bind_listeners("localhost", 18787)
    assert first.closed and second.closed


@pytest.mark.parametrize("code", [errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL])
def test_disabled_address_family_does_not_block_other_family(monkeypatch, code):
    first, second = FakeSocket(), FakeSocket(OSError(code, "disabled"))
    sockets = iter([first, second])
    monkeypatch.setattr(listener.socket, "getaddrinfo", lambda *a: resolved_addresses())
    monkeypatch.setattr(listener.socket, "socket", lambda *a: next(sockets))
    actual = listener.bind_listeners("localhost", 18787)
    assert actual == [first]
    assert second.closed and not first.closed


def test_duplicate_resolved_addresses_bind_once(monkeypatch):
    first = FakeSocket()
    addr = resolved_addresses()[0]
    monkeypatch.setattr(listener.socket, "getaddrinfo", lambda *a: [addr, addr])
    monkeypatch.setattr(listener.socket, "socket", lambda *a: first)
    assert listener.bind_listeners("localhost", 18787) == [first]


def test_no_available_address_is_a_bind_failure(monkeypatch):
    monkeypatch.setattr(listener.socket, "getaddrinfo", lambda *a: [])
    with pytest.raises(OSError, match="No usable local address"):
        listener.bind_listeners("localhost", 18787)


@pytest.fixture
def main_context(monkeypatch):
    cfg = parse_config(config_data())
    reservations = [FakeSocket()]
    monkeypatch.setattr(entry, "setup_logging", lambda: None)
    monkeypatch.setattr(entry, "parse_args", lambda: argparse.Namespace(config="unused.yaml", no_repl=True))
    monkeypatch.setattr(entry, "load_config", lambda p: cfg)
    monkeypatch.setattr(entry, "bind_listeners", lambda h, p: reservations)
    monkeypatch.setattr(entry, "create_app", lambda s: object())
    monkeypatch.setattr(entry.uvicorn, "Config", lambda **kwargs: SimpleNamespace(**kwargs))
    return cfg, reservations


def test_main_passes_the_reserved_socket_and_displays_base_url(main_context, monkeypatch, capsys):
    _, reservations = main_context
    class Server:
        started = False
        def __init__(self, config):
            assert config.port == 18787
        def run(self, sockets):
            assert sockets is reservations
            assert not sockets[0].closed
            self.started = True
    monkeypatch.setattr(entry.uvicorn, "Server", Server)
    assert entry.main() == 0
    assert reservations[0].closed
    output = capsys.readouterr().out
    assert "http://127.0.0.1:18787/v1" in output
    assert "auto-test" in output


@pytest.mark.parametrize("code", [errno.EADDRINUSE, errno.EACCES])
def test_main_does_not_start_repl_or_server_when_binding_fails(main_context, monkeypatch, capsys, code):
    def fail_bind(*args):
        raise OSError(code, "blocked")
    monkeypatch.setattr(entry, "bind_listeners", fail_bind)
    monkeypatch.setattr(entry, "parse_args", lambda: argparse.Namespace(config="unused.yaml", no_repl=False))
    monkeypatch.setattr(entry, "start_repl_thread", lambda s: pytest.fail("REPL started on occupied port"))
    monkeypatch.setattr(entry.uvicorn, "Server", lambda c: pytest.fail("server started on occupied port"))
    assert entry.main() == 1
    output = capsys.readouterr()
    assert "启动失败" in output.err
    assert "客户端 Base URL" not in output.out


@pytest.mark.parametrize("failure", ["app", "config", "run", "startup", "interrupt"])
def test_main_releases_socket_after_every_exit_path(main_context, monkeypatch, failure):
    _, reservations = main_context
    def fail(*a, **k):
        raise RuntimeError("fixture-failure")
    class Server:
        started = False
        def __init__(self, config):
            pass
        def run(self, sockets):
            if failure == "run":
                fail()
            if failure == "interrupt":
                raise KeyboardInterrupt
    monkeypatch.setattr(entry.uvicorn, "Server", Server)
    if failure == "app":
        monkeypatch.setattr(entry, "create_app", fail)
    if failure == "config":
        monkeypatch.setattr(entry.uvicorn, "Config", fail)
    assert entry.main() == (0 if failure == "interrupt" else 1)
    assert all(s.closed for s in reservations)



def test_quit_before_startup_waits_for_uvicorn_to_be_ready(main_context, monkeypatch):
    _, reservations = main_context
    requested = threading.Event()
    requested.set()
    monkeypatch.setattr(entry, "parse_args", lambda: argparse.Namespace(config="unused.yaml", no_repl=False))
    monkeypatch.setattr(entry, "start_repl_thread", lambda state: SimpleNamespace(should_exit=requested))
    class Server:
        started = False
        should_exit = False
        def __init__(self, config):
            pass
        def run(self, sockets):
            # A queued quit must not cause Uvicorn to skip lifespan shutdown.
            assert not self.should_exit
            self.started = True
            deadline = time.monotonic() + 3
            while not self.should_exit and time.monotonic() < deadline:
                time.sleep(0.01)
            assert self.should_exit
    monkeypatch.setattr(entry.uvicorn, "Server", Server)
    assert entry.main() == 0
    assert all(sock.closed for sock in reservations)


def test_startup_exception_redacts_configured_secrets(main_context, monkeypatch, capsys):
    cfg, _ = main_context
    secret = cfg.virtual_models["auto-test"][0].api_key
    def fail(*args):
        raise RuntimeError("fixture echoed " + secret)
    monkeypatch.setattr(entry, "create_app", fail)
    assert entry.main() == 1
    assert secret not in capsys.readouterr().err


def test_repl_ctrl_c_requests_graceful_shutdown():
    repl = Repl(RuntimeState(parse_config(config_data())))
    class Session:
        def prompt(self, *args):
            raise KeyboardInterrupt
    repl._loop(Session())
    assert repl.should_exit.is_set()


@pytest.fixture
def launcher_project(tmp_path):
    project = tmp_path / "中文项目 with spaces"
    project.mkdir()
    for name in ("main.py", "start.ps1", "启动AutoAPI.cmd"):
        shutil.copy2(ROOT / name, project / name)
    shutil.copytree(ROOT / "autoapi", project / "autoapi", ignore=shutil.ignore_patterns("__pycache__"))
    # Minimal relocatable venv shares installed packages, not the private config.
    scripts = project / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(sys.executable, scripts / "python.exe")
    shutil.copy2(Path(sys.prefix) / "pyvenv.cfg", project / ".venv" / "pyvenv.cfg")
    packages = project / ".venv" / "Lib" / "site-packages"
    packages.mkdir(parents=True)
    (packages / "local-test-dependencies.pth").write_text(
        str(Path(sys.prefix) / "Lib" / "site-packages") + "\n", encoding="utf-8")
    port = get_available_port()
    (project / "config.yaml").write_text(yaml.safe_dump(config_data(port=port)), encoding="utf-8")
    (tmp_path / "config.yaml").write_text("server: unrelated-invalid-config\n", encoding="utf-8")
    return project, port, tmp_path


def ps_command(project, *extra):
    return [str(POWERSHELL), "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(project / "start.ps1"), *extra]


def cmd_command(project, *extra):
    arguments = subprocess.list2cmdline(list(extra))
    command = f'""{project / "启动AutoAPI.cmd"}" {arguments}"'
    # list2cmdline escapes embedded quotes for a CRT parser, not CMD. Pass a
    # Windows command string so /s /c receives its required outer quote pair.
    return subprocess.list2cmdline([str(CMD), "/d", "/s", "/c"]) + " " + command


def run_process(command, cwd, input="", timeout=20):
    return subprocess.run(command, cwd=cwd, env=ENV, input=input,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if WINDOWS else 0)


@contextmanager
def running_process(command, cwd):
    proc = subprocess.Popen(command, cwd=cwd, env=ENV, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if WINDOWS else 0)
    try:
        yield proc
    finally:
        if proc.poll() is None:
            try:
                proc.communicate("quit\n", timeout=15)
            except (subprocess.TimeoutExpired, OSError):
                if WINDOWS:
                    subprocess.run(["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
                else:
                    proc.kill()
                proc.communicate(timeout=10)


def wait_health(proc, host, port):
    url = listener.server_url(host, port)
    with httpx.Client(trust_env=False, timeout=0.5) as client:
        for _ in range(200):
            if proc.poll() is not None:
                output = proc.communicate(timeout=10)[0]
                pytest.fail(f"Local fixture exited early ({proc.returncode}): {output}")
            try:
                response = client.get(url + "/healthz")
                if response.status_code == 200 and response.json().get("status") == "ok":
                    models = client.get(url + "/v1/models")
                    assert models.status_code == 200
                    assert [m["id"] for m in models.json()["data"]] == ["auto-test"]
                    assert response.json()["total_requests"] == 0
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
    pytest.fail("Owned localhost test server did not become healthy")


@WINDOWS_ONLY
@pytest.mark.parametrize("kind", ["powershell", "cmd"])
def test_launcher_uses_its_venv_and_config_from_another_directory(launcher_project, kind):
    project, port, outside = launcher_project
    command = ps_command(project) if kind == "powershell" else cmd_command(project)
    with running_process(command, outside) as proc:
        wait_health(proc, "127.0.0.1", port)
        output = proc.communicate("quit\n", timeout=20)[0]
        assert proc.returncode == 0, output
        assert f"http://127.0.0.1:{port}/v1" in output
    assert_port_released("127.0.0.1", port)


@WINDOWS_ONLY
def test_repeated_launch_keeps_existing_instance_healthy(launcher_project):
    project, port, outside = launcher_project
    with running_process(ps_command(project), outside) as first:
        wait_health(first, "127.0.0.1", port)
        second = run_process(cmd_command(project), outside, "x\n")
        assert second.returncode != 0
        assert "启动失败" in second.stdout
        assert "Press any key" in second.stdout
        assert "交互式命令行就绪" not in second.stdout
        wait_health(first, "127.0.0.1", port)
        output = first.communicate("quit\n", timeout=20)[0]
        assert first.returncode == 0, output
    assert_port_released("127.0.0.1", port)


@WINDOWS_ONLY
def test_launcher_does_not_disturb_an_unrelated_port_owner(launcher_project):
    project, port, outside = launcher_project
    owner = listener.bind_listeners("127.0.0.1", port)[0]
    try:
        owner.listen()
        result = run_process(cmd_command(project), outside, "x\n")
        assert result.returncode != 0
        assert "不会自动换端口" in result.stdout
        assert "Press any key" in result.stdout
        assert owner.fileno() >= 0
        with socket.create_connection(("127.0.0.1", port), timeout=1) as client:
            accepted, _ = owner.accept()
            accepted.close()
    finally:
        owner.close()
    assert_port_released("127.0.0.1", port)


@WINDOWS_ONLY
@pytest.mark.parametrize("missing", ["venv", "main", "config"])
def test_double_click_entry_preserves_error_and_nonzero_exit(launcher_project, missing):
    project, port, outside = launcher_project
    path = {"venv": project / ".venv/Scripts/python.exe", "main": project / "main.py",
            "config": project / "config.yaml"}[missing]
    path.unlink()
    result = run_process(cmd_command(project), outside, "x\n")
    assert result.returncode != 0
    assert "启动失败" in result.stdout
    assert "Press any key" in result.stdout
    assert_port_released("127.0.0.1", port)


@WINDOWS_ONLY
def test_invalid_config_keeps_double_click_error_visible(launcher_project):
    project, port, outside = launcher_project
    bad = config_data(port=port)
    bad["server"]["port"] = "not-a-port"
    (project / "config.yaml").write_text(yaml.safe_dump(bad), encoding="utf-8")
    result = run_process(cmd_command(project), outside, "x\n")
    assert result.returncode != 0
    assert "启动失败" in result.stdout
    assert "server.port" in result.stdout
    assert "Press any key" in result.stdout
    assert_port_released("127.0.0.1", port)


@WINDOWS_ONLY
@pytest.mark.parametrize("host", ["localhost", "::1"])
@pytest.mark.parametrize("kind", ["powershell", "cmd"])
def test_no_repl_and_custom_config_preserve_ipv4_ipv6_support(launcher_project, host, kind):
    project, _, outside = launcher_project
    try:
        port = get_available_port(host)
    except OSError:
        pytest.skip("IPv6 not enabled")
    custom = project / "custom config 中文.yaml"
    custom.write_text(yaml.safe_dump(config_data(host=host, port=port)), encoding="utf-8")
    main_path = project / "main.py"
    main_source = main_path.read_text(encoding="utf-8")
    timer = """import signal
_original_startup = uvicorn.Server.startup
async def _stop_fixture_after_startup(self, sockets=None):
    await _original_startup(self, sockets)
    if self.started:
        threading.Timer(2, lambda: signal.raise_signal(signal.SIGINT)).start()
uvicorn.Server.startup = _stop_fixture_after_startup

"""
    main_path.write_text(main_source.replace('if __name__ == "__main__":',
        timer + 'if __name__ == "__main__":'), encoding="utf-8")
    command = (ps_command if kind == "powershell" else cmd_command)(
        project, "-NoRepl", "-Config", custom.name)
    with running_process(command, outside) as proc:
        wait_health(proc, "::1" if host == "::1" else "127.0.0.1", port)
        output = proc.communicate(timeout=20)[0]
        assert proc.returncode == 0, output
        assert "交互式命令行就绪" not in output
        assert "Application shutdown complete" in output
    assert_port_released(host, port)


@WINDOWS_ONLY
def test_real_sigint_shutdown_releases_server_socket(launcher_project):
    project, port, outside = launcher_project
    driver = project / "signal-test.py"
    driver.write_text('''import signal, threading, time\nimport main\nimport uvicorn\noriginal = uvicorn.Server.startup\nasync def startup(self, sockets=None):\n    await original(self, sockets)\n    if self.started:\n        threading.Timer(1, lambda: signal.raise_signal(signal.SIGINT)).start()\nuvicorn.Server.startup = startup\nraise SystemExit(main.main())\n''', encoding="utf-8")
    result = run_process([str(project / ".venv/Scripts/python.exe"), str(driver),
                          "--no-repl", "-c", str(project / "config.yaml")], outside)
    assert result.returncode == 0, result.stdout
    assert "Application shutdown complete" in result.stdout
    assert_port_released("127.0.0.1", port)


def test_launcher_encodings_and_no_global_execution_policy_changes():
    ps = (ROOT / "start.ps1").read_bytes()
    cmd = (ROOT / "启动AutoAPI.cmd").read_bytes().decode("ascii")
    assert ps.startswith(b"\xef\xbb\xbf")
    assert "Set-ExecutionPolicy" not in ps.decode("utf-8-sig")
    assert "-ExecutionPolicy Bypass" in cmd
    assert '"%~dp0start.ps1"' in cmd
    assert "pause >nul" in cmd
    assert "taskkill" not in cmd.lower()
