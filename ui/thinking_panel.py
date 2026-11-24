# -*- coding: utf-8 -*-
"""对话区思考过程：可折叠步骤条 + 中止 / 撤回本轮（接入层展示，不调模型）。"""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QToolButton, QPushButton,
    QScrollArea, QWidget,
)


class ThinkingPanel(QFrame):
    """左侧对话下方的可折叠进度：摘要 + 步骤列表。"""

    abort_clicked = pyqtSignal()
    undo_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("thinkingPanel")
        self._steps: list[str] = []
        self._active = False
        self._expanded = True

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(2)

        head = QHBoxLayout()
        head.setContentsMargins(2, 2, 2, 0)
        head.setSpacing(4)
        self._toggle = QToolButton()
        self._toggle.setObjectName("thinkingToggle")
        self._toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._toggle.setCheckable(True)
        self._toggle.setChecked(True)
        self._toggle.setText("思考过程 ▼")
        self._toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle.toggled.connect(self._on_toggled)
        head.addWidget(self._toggle, 1)

        self.abort_btn = QPushButton("中止")
        self.abort_btn.setObjectName("btnAbortThink")
        self.abort_btn.setFixedSize(52, 24)
        self.abort_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.abort_btn.setToolTip("停止当前任务；若已写文件且有快照，会尽量恢复改前")
        self.abort_btn.clicked.connect(self.abort_clicked.emit)
        self.abort_btn.setEnabled(False)
        self.abort_btn.hide()
        head.addWidget(self.abort_btn)

        self.undo_btn = QPushButton("撤回本轮")
        self.undo_btn.setObjectName("btnUndoRound")
        self.undo_btn.setFixedSize(78, 24)
        self.undo_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.undo_btn.setToolTip(
            "撤回最近一轮：Word 用写前备份盖回；整理挪回；"
            "仅说话/查表则去掉本轮 AI 回应")
        self.undo_btn.clicked.connect(self.undo_clicked.emit)
        self.undo_btn.setEnabled(False)
        head.addWidget(self.undo_btn)

        root.addLayout(head)

        self._body = QWidget()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(8, 2, 6, 6)
        self._body_layout.setSpacing(4)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setMaximumHeight(120)
        self._scroll.setWidget(self._body)
        root.addWidget(self._scroll)

        self.hide()

    def begin(self, first: str = "开始处理…") -> None:
        self._steps = []
        self._active = True
        self._expanded = True
        self._toggle.setChecked(True)
        self._scroll.show()
        self._clear_rows()
        self.set_abort_enabled(True)
        self.push(first)
        self.show()

    def push(self, text: str) -> None:
        msg = (text or "").strip()
        if not msg:
            return
        if self._steps and self._steps[-1] == msg:
            return
        self._steps.append(msg)
        self._rebuild()
        self._toggle.setText(self._header_text())
        self.show()
        if self._expanded:
            self._scroll.show()

    def finish(self, *, ok: bool = True, elapsed: int = 0) -> None:
        self._active = False
        self.set_abort_enabled(False)
        if elapsed > 0:
            mm, ss = divmod(elapsed, 60)
            suffix = " · 用时 %d:%02d" % (mm, ss)
        else:
            suffix = ""
        n = len(self._steps)
        if ok:
            text = "已完成 %d 步%s ▼" % (n, suffix)
        else:
            text = "已结束 · %d 步%s ▼" % (n, suffix)
        self._toggle.blockSignals(True)
        self._toggle.setChecked(False)
        self._toggle.blockSignals(False)
        self._expanded = False
        self._scroll.hide()
        self._rebuild()
        self._toggle.setText(text)

    def set_abort_enabled(self, on: bool) -> None:
        self.abort_btn.setEnabled(bool(on))
        self.abort_btn.setVisible(bool(on))

    def set_undo_enabled(self, on: bool) -> None:
        self.undo_btn.setEnabled(bool(on))

    def _header_text(self) -> str:
        n = len(self._steps)
        if self._active:
            return "思考过程 · %d 步 ▼" % n if self._expanded else "思考过程 · %d 步 ▶" % n
        return "思考过程 · %d 步 ▼" % n

    def _on_toggled(self, checked: bool) -> None:
        self._expanded = bool(checked)
        self._scroll.setVisible(self._expanded)
        if self._active:
            self._toggle.setText(self._header_text())
        elif self._expanded:
            self._toggle.setText(
                self._toggle.text().replace(" ▶", " ▼").replace(" ▸", " ▼"))

    def _clear_rows(self) -> None:
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _rebuild(self) -> None:
        self._clear_rows()
        last = len(self._steps) - 1
        for i, text in enumerate(self._steps):
            row = QLabel()
            row.setWordWrap(True)
            row.setStyleSheet("font-size:11.5px; background:transparent;")
            row.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            is_last = i == last
            if self._active and is_last:
                icon, icon_color, text_color = "●", "#4d6bfe", "#1f2329"
            else:
                icon, icon_color, text_color = "✓", "#9aa0a6", "#6b7280"
            row.setText(
                "<span style='color:%s'>%s</span>"
                "&nbsp;<span style='color:%s'>%s</span>"
                % (icon_color, icon, text_color, _esc(text))
            )
            self._body_layout.addWidget(row)
        self._body_layout.addStretch(1)
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())


def _esc(s: str) -> str:
    import html as _html
    return _html.escape(s or "")
