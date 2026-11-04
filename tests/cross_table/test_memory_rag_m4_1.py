# -*- coding: utf-8 -*-
"""
M4.1 WebServiceManager (subprocess start/stop).

页眉：M4 技术测试通过、可用性由本批补丁修复；真实第三方 embeddings 未验证；
CURRENT 数值槽仍 null；不接真实第三方 API。
"""
from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _pid_alive(pid: int) -> bool:
    """True if OS still lists the process. Avoid os.kill(pid,0) on Windows (false positives)."""
    if pid is None:
        return False
    if os.name == "nt":
        r = subprocess.run(
            ["tasklist", "/FI", "PID eq %s" % int(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        out = (r.stdout or "") + (r.stderr or "")
        # When found, stdout contains the pid digits on a process line.
        for line in out.splitlines():
            if str(int(pid)) in line.split() and "PID" not in line:
                return True
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


class TestMemoryRagM41(unittest.TestCase):
    def setUp(self):
        from web.memory_correction import web_service_manager as wsm

        wsm.reset_shared_manager_for_tests()
        self.wsm = wsm
        fd, log_path = tempfile.mkstemp(suffix=".log")
        os.close(fd)
        self.log_path = Path(log_path)
        # Ephemeral free port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        self.port = int(sock.getsockname()[1])
        sock.close()
        self.mgr = wsm.WebServiceManager(
            project_root=ROOT,
            log_path=self.log_path,
            poll_interval_s=0.2,
            ready_timeout_s=30.0,
            stop_timeout_s=10.0,
        )
        os.environ.setdefault("MEMORY_CORRECTION_EMBEDDING_DIM", "1536")

    def tearDown(self):
        try:
            self.mgr.stop()
        except Exception:
            pass
        try:
            self.log_path.unlink(missing_ok=True)
        except Exception:
            pass
        self.wsm.reset_shared_manager_for_tests()

    def test_start_success_healthz(self):
        outcome = self.mgr.start(self.port, wait_ready=True)
        self.assertTrue(outcome.ok, msg=outcome.detail or self.mgr.status)
        self.assertEqual(self.mgr.status_code, self.wsm.ServiceStatus.RUNNING)
        self.assertTrue(self.wsm.healthz_ok("127.0.0.1", self.port))
        self.assertIsNotNone(self.mgr.pid)

    def test_stop_success_process_gone(self):
        outcome = self.mgr.start(self.port, wait_ready=True)
        self.assertTrue(outcome.ok, msg=outcome.detail)
        pid = self.mgr.pid
        self.assertIsNotNone(pid)
        stop = self.mgr.stop()
        self.assertTrue(stop.ok, msg=stop.detail)
        self.assertEqual(self.mgr.status_code, self.wsm.ServiceStatus.STOPPED)
        self.assertIsNone(self.mgr.pid)
        # Process should be gone (Windows: allow brief reaping window)
        deadline = time.time() + 3.0
        alive = True
        while time.time() < deadline:
            alive = _pid_alive(pid)
            if not alive:
                break
            time.sleep(0.1)
        self.assertFalse(alive, msg="pid %s still alive after stop" % pid)
        self.assertFalse(self.wsm.healthz_ok("127.0.0.1", self.port))
        self.assertTrue(self.wsm.port_is_free("127.0.0.1", self.port))

    def test_port_occupied_fails(self):
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", self.port))
        holder.listen(1)
        try:
            outcome = self.mgr.start(self.port, wait_ready=True)
            self.assertFalse(outcome.ok)
            self.assertIn("端口被占用", outcome.detail)
            self.assertIn("端口被占用", self.mgr.status)
        finally:
            holder.close()

    def test_start_timeout_fails(self):
        # Invalid app import: process dies or never serves healthz.
        mgr = self.wsm.WebServiceManager(
            project_root=ROOT,
            log_path=self.log_path,
            poll_interval_s=0.1,
            ready_timeout_s=2.0,
            stop_timeout_s=5.0,
            app_import="memory_correction.__no_such_app__:app",
        )
        try:
            outcome = mgr.start(self.port, wait_ready=True, ready_timeout_s=2.0)
            self.assertFalse(outcome.ok)
            self.assertEqual(mgr.status_code, self.wsm.ServiceStatus.FAILED)
            # Either timeout message or child exited early — both are failure.
            self.assertTrue(
                ("超时" in mgr.status)
                or ("退出" in mgr.status)
                or ("超时" in outcome.detail)
                or ("退出" in outcome.detail),
                msg=mgr.status,
            )
        finally:
            mgr.stop()

    def test_stdout_stderr_written_to_log(self):
        outcome = self.mgr.start(self.port, wait_ready=True)
        self.assertTrue(outcome.ok, msg=outcome.detail)
        # Allow flush
        time.sleep(0.2)
        self.mgr.stop()
        text = self.log_path.read_text(encoding="utf-8", errors="replace")
        self.assertTrue(
            ("Uvicorn" in text)
            or ("uvicorn" in text)
            or ("Started" in text)
            or ("Application startup" in text)
            or len(text.strip()) > 0,
            msg="log empty or missing uvicorn markers: %r" % text[:500],
        )


if __name__ == "__main__":
    unittest.main()
