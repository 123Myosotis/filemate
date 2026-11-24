# -*- coding: utf-8 -*-
"""
Subprocess manager for memory_correction uvicorn (M4.1).

GUI uses this module; no QThread. Listen host defaults to loopback only.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return int(default)
    return int(str(raw).strip())


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return float(default)
    return float(str(raw).strip())


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return str(default)
    return str(raw).strip()


# Defaults match M4.1 gate; overridable via env / constructor for tests.
DEFAULT_HOST = _env_str("MEMORY_CORRECTION_BIND_HOST", "127.0.0.1")
DEFAULT_APP_IMPORT = _env_str(
    "MEMORY_CORRECTION_UVICORN_APP", "memory_correction.app:app"
)
DEFAULT_POLL_INTERVAL_S = _env_float("MEMORY_CORRECTION_HEALTHZ_POLL_S", 0.5)
DEFAULT_READY_TIMEOUT_S = _env_float("MEMORY_CORRECTION_READY_TIMEOUT_S", 30.0)
DEFAULT_STOP_TIMEOUT_S = _env_float("MEMORY_CORRECTION_STOP_TIMEOUT_S", 10.0)
LOG_FILE_NAME = _env_str("MEMORY_CORRECTION_UVICORN_LOG_NAME", "uvicorn.log")


class ServiceStatus:
    NOT_STARTED = "未启动"
    STARTING = "启动中"
    RUNNING = "运行中"
    STOPPING = "停止中"
    STOPPED = "已停止"
    FAILED = "失败"


@dataclass
class StartOutcome:
    ok: bool
    status: str
    detail: str = ""
    pid: Optional[int] = None


def package_root() -> Path:
    return Path(__file__).resolve().parent


def default_project_root() -> Path:
    # web/memory_correction -> project root
    return Path(__file__).resolve().parents[2]


def default_log_path(project_root: Path | None = None) -> Path:
    root = project_root if project_root is not None else default_project_root()
    return root / "web" / "memory_correction" / "logs" / LOG_FILE_NAME


def port_is_free(host: str, port: int) -> bool:
    """
    True if port appears free.
    1) If something already accepts TCP → occupied.
    2) Else try bind without SO_REUSEADDR (Windows REUSEADDR masks conflicts).
    """
    port = int(port)
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.3)
        probe.connect((host, port))
        return False
    except (OSError, socket.timeout):
        pass
    finally:
        try:
            probe.close()
        except Exception:
            pass

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Do not set SO_REUSEADDR: on Windows it can allow a false "free".
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except Exception:
            pass


def healthz_ok(host: str, port: int, timeout_s: float = 2.0) -> bool:
    url = "http://%s:%s/healthz" % (host, int(port))
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            body = resp.read().decode("utf-8", "replace")
            return int(resp.status) == 200 and "ok" in body
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


class WebServiceManager:
    """Owns one uvicorn child process for memory_correction."""

    def __init__(
        self,
        project_root: Path | None = None,
        *,
        host: str | None = None,
        app_import: str | None = None,
        log_path: Path | None = None,
        poll_interval_s: float | None = None,
        ready_timeout_s: float | None = None,
        stop_timeout_s: float | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.project_root = Path(
            project_root if project_root is not None else default_project_root()
        ).resolve()
        self.host = str(host if host is not None else DEFAULT_HOST)
        self.app_import = str(
            app_import if app_import is not None else DEFAULT_APP_IMPORT
        )
        self.log_path = Path(
            log_path if log_path is not None else default_log_path(self.project_root)
        )
        self.poll_interval_s = float(
            poll_interval_s
            if poll_interval_s is not None
            else DEFAULT_POLL_INTERVAL_S
        )
        self.ready_timeout_s = float(
            ready_timeout_s
            if ready_timeout_s is not None
            else DEFAULT_READY_TIMEOUT_S
        )
        self.stop_timeout_s = float(
            stop_timeout_s if stop_timeout_s is not None else DEFAULT_STOP_TIMEOUT_S
        )
        self._sleep = sleep_fn if sleep_fn is not None else time.sleep

        self._proc: subprocess.Popen | None = None
        self._log_fp = None
        self._status = ServiceStatus.NOT_STARTED
        self._detail = ""
        self._port: int | None = None

    @property
    def status(self) -> str:
        if self._status == ServiceStatus.FAILED and self._detail:
            return "%s：%s" % (ServiceStatus.FAILED, self._detail)
        return self._status

    @property
    def status_code(self) -> str:
        return self._status

    @property
    def detail(self) -> str:
        return self._detail

    @property
    def pid(self) -> Optional[int]:
        if self._proc is None:
            return None
        return self._proc.pid

    @property
    def port(self) -> Optional[int]:
        return self._port

    @property
    def is_running(self) -> bool:
        return (
            self._status == ServiceStatus.RUNNING
            and self._proc is not None
            and self._proc.poll() is None
        )

    def _set(self, status: str, detail: str = "") -> None:
        self._status = status
        self._detail = detail

    def _build_env(self) -> dict:
        env = dict(os.environ)
        web_dir = str(self.project_root / "web")
        root = str(self.project_root)
        sep = os.pathsep
        existing = env.get("PYTHONPATH", "")
        parts = [root, web_dir]
        if existing:
            parts.append(existing)
        env["PYTHONPATH"] = sep.join(parts)
        return env

    def _close_log(self) -> None:
        if self._log_fp is not None:
            try:
                self._log_fp.flush()
                self._log_fp.close()
            except Exception:
                pass
            self._log_fp = None

    def start(
        self,
        port: int,
        *,
        wait_ready: bool = True,
        app_import: str | None = None,
        ready_timeout_s: float | None = None,
    ) -> StartOutcome:
        """
        Start uvicorn subprocess. If wait_ready, poll /healthz until ok or timeout.
        """
        if self.is_running:
            return StartOutcome(
                ok=True,
                status=self.status,
                detail="already_running",
                pid=self.pid,
            )

        port = int(port)
        if not port_is_free(self.host, port):
            msg = "端口被占用，请改端口或关闭占用进程"
            self._set(ServiceStatus.FAILED, msg)
            return StartOutcome(ok=False, status=self.status, detail=msg)

        self._set(ServiceStatus.STARTING)
        self._port = port
        target = str(app_import if app_import is not None else self.app_import)
        timeout = float(
            ready_timeout_s
            if ready_timeout_s is not None
            else self.ready_timeout_s
        )

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._close_log()
        self._log_fp = open(self.log_path, "a", encoding="utf-8", errors="replace")

        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            target,
            "--host",
            self.host,
            "--port",
            str(port),
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                cwd=str(self.project_root),
                env=self._build_env(),
                stdout=self._log_fp,
                stderr=subprocess.STDOUT,
            )
        except Exception as exc:
            self._close_log()
            self._proc = None
            msg = "启动失败：%s" % exc
            self._set(ServiceStatus.FAILED, msg)
            return StartOutcome(ok=False, status=self.status, detail=msg)

        if not wait_ready:
            return StartOutcome(
                ok=True,
                status=self.status,
                detail="starting",
                pid=self.pid,
            )

        return self.wait_until_ready(timeout_s=timeout)

    def wait_until_ready(self, timeout_s: float | None = None) -> StartOutcome:
        timeout = float(
            timeout_s if timeout_s is not None else self.ready_timeout_s
        )
        if self._proc is None:
            msg = "无子进程"
            self._set(ServiceStatus.FAILED, msg)
            return StartOutcome(ok=False, status=self.status, detail=msg)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                msg = "子进程已退出，查看日志"
                self._close_log()
                self._proc = None
                self._set(ServiceStatus.FAILED, msg)
                return StartOutcome(ok=False, status=self.status, detail=msg)
            if self._port is not None and healthz_ok(self.host, self._port):
                self._set(ServiceStatus.RUNNING)
                return StartOutcome(
                    ok=True, status=self.status, detail="", pid=self.pid
                )
            self._sleep(self.poll_interval_s)

        msg = "启动超时，查看日志"
        # Leave process for user to inspect? Spec: status=失败. Kill orphan.
        try:
            if self._proc.poll() is None:
                self._proc.kill()
                self._proc.wait(timeout=5)
        except Exception:
            pass
        self._close_log()
        self._proc = None
        self._set(ServiceStatus.FAILED, msg)
        return StartOutcome(ok=False, status=self.status, detail=msg)

    def poll_ready_once(self) -> Optional[StartOutcome]:
        """
        One healthz check while STARTING. Returns outcome when decided,
        else None (still starting).
        """
        if self._status != ServiceStatus.STARTING or self._proc is None:
            if self._status == ServiceStatus.RUNNING:
                return StartOutcome(ok=True, status=self.status, pid=self.pid)
            return StartOutcome(
                ok=False, status=self.status, detail=self._detail, pid=self.pid
            )
        if self._proc.poll() is not None:
            msg = "子进程已退出，查看日志"
            self._close_log()
            self._proc = None
            self._set(ServiceStatus.FAILED, msg)
            return StartOutcome(ok=False, status=self.status, detail=msg)
        if self._port is not None and healthz_ok(self.host, self._port):
            self._set(ServiceStatus.RUNNING)
            return StartOutcome(ok=True, status=self.status, pid=self.pid)
        return None

    def mark_start_timeout(self) -> StartOutcome:
        return self.wait_until_ready(timeout_s=0)

    def _kill_proc_tree(self, proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        pid = proc.pid
        if sys.platform == "win32":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
            except Exception:
                pass
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=self.stop_timeout_s)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def stop(self, timeout_s: float | None = None) -> StartOutcome:
        timeout = float(
            timeout_s if timeout_s is not None else self.stop_timeout_s
        )
        if self._proc is None:
            self._set(ServiceStatus.STOPPED)
            return StartOutcome(ok=True, status=self.status, detail="no_proc")

        self._set(ServiceStatus.STOPPING)
        proc = self._proc
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    pass
            # Windows: always tear down the process tree (uvicorn may leave children).
            if sys.platform == "win32":
                self._kill_proc_tree(proc)
            elif proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            if proc.poll() is None:
                self._kill_proc_tree(proc)
        except Exception as exc:
            msg = "停止异常：%s" % exc
            self._set(ServiceStatus.FAILED, msg)
            return StartOutcome(ok=False, status=self.status, detail=msg)
        finally:
            self._close_log()
            self._proc = None

        self._set(ServiceStatus.STOPPED)
        return StartOutcome(ok=True, status=self.status, pid=None)

    def open_log_file(self) -> Path:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.log_path.exists():
            self.log_path.write_text("", encoding="utf-8")
        return self.log_path


_SHARED: WebServiceManager | None = None
_QUERY: WebServiceManager | None = None


def get_shared_manager(project_root: Path | None = None) -> WebServiceManager:
    global _SHARED
    if _SHARED is None:
        _SHARED = WebServiceManager(project_root=project_root)
    return _SHARED


def get_query_ui_manager(project_root: Path | None = None) -> WebServiceManager:
    """Reuse the same subprocess manager; different app / log. Loopback only."""
    global _QUERY
    if _QUERY is None:
        root = Path(
            project_root if project_root is not None else default_project_root()
        ).resolve()
        _QUERY = WebServiceManager(
            project_root=root,
            app_import="query_ui.app:app",
            log_path=root / "web" / "query_ui" / "logs" / LOG_FILE_NAME,
        )
    return _QUERY


def reset_shared_manager_for_tests() -> None:
    global _SHARED, _QUERY
    for mgr in (_SHARED, _QUERY):
        if mgr is not None:
            try:
                mgr.stop()
            except Exception:
                pass
    _SHARED = None
    _QUERY = None
