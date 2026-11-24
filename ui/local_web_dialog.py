# -*- coding: utf-8 -*-
"""设置 → 本地 Web：记忆纠错 + 跨表查询。只启停 127.0.0.1，不决策、不调模型。"""
from __future__ import annotations

from urllib.request import urlopen

from PyQt6.QtCore import QTimer, QUrl
from PyQt6.QtGui import QDesktopServices, QGuiApplication
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)


def open_local_web_dialog(parent) -> None:
    from web.memory_correction.port_settings import (
        resolve_port as resolve_mem_port,
        save_gui_port as save_mem_port,
        startup_command as mem_cmd,
    )
    from web.memory_correction.web_service_manager import (
        ServiceStatus,
        get_query_ui_manager,
        get_shared_manager,
    )
    from web.query_ui.port_settings import (
        resolve_port as resolve_query_port,
        save_gui_port as save_query_port,
        startup_command as query_cmd,
    )

    dialog = QDialog(parent)
    dialog.setWindowTitle("本地 Web")
    root = QVBoxLayout(dialog)
    hint = QLabel(
        "仅本机 127.0.0.1。聊天模型仍在「设置 → 聊天模型设置」。\n"
        "MaiBot / 向量在「设置 → 记忆 / 向量设置」。不必自己找端口。"
    )
    hint.setWordWrap(True)
    root.addWidget(hint)

    mem_box, mem_port, mem_status, mem_timer = _service_group(
        dialog,
        title="记忆纠错",
        mgr=get_shared_manager(),
        port=int(resolve_mem_port()),
        save_port=save_mem_port,
        cmd_fn=mem_cmd,
        log_parent=parent,
        ready_name="记忆纠错 Web",
    )
    query_box, query_port, query_status, query_timer = _service_group(
        dialog,
        title="跨表查询",
        mgr=get_query_ui_manager(),
        port=int(resolve_query_port()),
        save_port=save_query_port,
        cmd_fn=query_cmd,
        log_parent=parent,
        ready_name="跨表查询 Web",
    )
    root.addWidget(mem_box)
    root.addWidget(query_box)

    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close
    )

    def _save():
        save_mem_port(int(mem_port.value()))
        save_query_port(int(query_port.value()))
        if parent is not None and hasattr(parent, "log"):
            parent.log("本地 Web 端口已保存")
        dialog.accept()

    def _cleanup():
        mem_timer.stop()
        query_timer.stop()

    buttons.accepted.connect(_save)
    buttons.rejected.connect(dialog.reject)
    dialog.finished.connect(_cleanup)
    root.addWidget(buttons)
    dialog.resize(520, 420)
    dialog.exec()
    del mem_status, query_status, ServiceStatus


def _service_group(dialog, *, title, mgr, port, save_port, cmd_fn, log_parent, ready_name):
    from web.memory_correction.web_service_manager import ServiceStatus

    box = QGroupBox(title)
    form = QFormLayout(box)
    port_box = QSpinBox()
    port_box.setRange(1, 65535)
    port_box.setValue(int(port))
    form.addRow("端口", port_box)
    status_label = QLabel("状态：%s" % mgr.status)
    status_label.setWordWrap(True)
    form.addRow(status_label)

    btn_start = QPushButton("启动并打开")
    btn_open = QPushButton("打开页面")
    btn_stop = QPushButton("停止")
    btn_log = QPushButton("日志")
    btn_copy = QPushButton("复制命令")
    row = QHBoxLayout()
    for b in (btn_start, btn_open, btn_stop, btn_log, btn_copy):
        row.addWidget(b)
    form.addRow(row)

    poll_timer = QTimer(dialog)
    poll_timer.setInterval(max(1, int(mgr.poll_interval_s * 1000)))
    deadline = {"t": 0.0}

    def _log(msg):
        if log_parent is not None and hasattr(log_parent, "log"):
            log_parent.log(msg)

    def _refresh():
        status_label.setText("状态：%s" % mgr.status)

    def _url():
        return "http://127.0.0.1:%s/" % int(port_box.value())

    def _open_page():
        QDesktopServices.openUrl(QUrl(_url()))

    def _copy():
        QGuiApplication.clipboard().setText(cmd_fn(int(port_box.value())))
        _log("已复制启动命令")

    def _open_log():
        path = mgr.open_log_file()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

    def _health_ok():
        url = "http://127.0.0.1:%s/healthz" % int(port_box.value())
        try:
            with urlopen(url, timeout=2) as resp:
                body = resp.read().decode("utf-8", "replace")
                return int(resp.status) == 200 and "ok" in body
        except Exception:
            return False

    def _on_poll():
        import time as _time

        outcome = mgr.poll_ready_once()
        if outcome is not None:
            poll_timer.stop()
            _refresh()
            if outcome.ok:
                _log("%s 已启动（PID=%s）" % (ready_name, outcome.pid))
                _open_page()
            else:
                QMessageBox.warning(dialog, "启动失败", outcome.detail or mgr.status)
            return
        if _time.monotonic() >= deadline["t"]:
            poll_timer.stop()
            mgr.wait_until_ready(timeout_s=0)
            _refresh()
            QMessageBox.warning(dialog, "启动失败", "启动超时，请看日志")

    def _start():
        if mgr.is_running:
            _refresh()
            _open_page()
            return
        port_i = int(port_box.value())
        save_port(port_i)
        outcome = mgr.start(port_i, wait_ready=False)
        _refresh()
        if not outcome.ok:
            QMessageBox.warning(dialog, "启动失败", outcome.detail or mgr.status)
            return
        if mgr.status_code == ServiceStatus.RUNNING or _health_ok():
            _log("%s 已启动（PID=%s）" % (ready_name, mgr.pid))
            _open_page()
            return
        import time as _time

        deadline["t"] = _time.monotonic() + float(mgr.ready_timeout_s)
        poll_timer.start()

    def _stop():
        poll_timer.stop()
        outcome = mgr.stop()
        _refresh()
        if not outcome.ok:
            QMessageBox.warning(dialog, "停止失败", outcome.detail or mgr.status)
        else:
            _log("%s 已停止" % ready_name)

    poll_timer.timeout.connect(_on_poll)
    btn_start.clicked.connect(_start)
    btn_open.clicked.connect(_open_page)
    btn_stop.clicked.connect(_stop)
    btn_log.clicked.connect(_open_log)
    btn_copy.clicked.connect(_copy)
    return box, port_box, status_label, poll_timer
