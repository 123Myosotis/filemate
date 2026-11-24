# ui/style.py
"""FileMate 主题：浅色、低对比、单一蓝色强调；留白优先。

色值与 ui/design_tokens.json 对齐（Qt 侧 QSS；若有 Web Tab 应读同一份令牌，禁止各写一套）。
"""
from __future__ import annotations

import json
from pathlib import Path

_TOKENS_PATH = Path(__file__).with_name("design_tokens.json")


def load_design_tokens() -> dict:
    try:
        return json.loads(_TOKENS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


APP_QSS = """
QWidget {
    font-family: "Segoe UI", "Microsoft YaHei UI", "PingFang SC", sans-serif;
    font-size: 12.5px;
    color: #1f2329;
    background: #f7f8fa;
}
QMainWindow, QDialog {
    background: #f7f8fa;
}

/* —— 菜单 —— */
QMenuBar {
    background: #ffffff;
    border-bottom: 1px solid #eceef1;
    padding: 2px 8px;
}
QMenuBar::item {
    background: transparent;
    padding: 5px 10px;
    border-radius: 8px;
    color: #4b5563;
}
QMenuBar::item:selected {
    background: #f2f4f7;
    color: #1f2329;
}
QMenu {
    background: #ffffff;
    border: 1px solid #e6e8eb;
    border-radius: 10px;
    padding: 6px;
}
QMenu::item {
    padding: 7px 16px;
    border-radius: 7px;
    color: #374151;
}
QMenu::item:selected {
    background: #f2f4f7;
    color: #1f2329;
}

/* —— 标签页 —— */
QTabWidget::pane {
    border: 1px solid #eceef1;
    border-radius: 14px;
    background: #ffffff;
    top: -1px;
}
QTabBar {
    qproperty-drawBase: 0;
}
QTabBar::tab {
    background: transparent;
    color: #6b7280;
    border: 1px solid transparent;
    border-radius: 9px;
    padding: 5px 8px 5px 10px;
    margin-right: 4px;
    min-width: 72px;
    max-width: 150px;
}
QTabBar::tab:selected {
    background: #ffffff;
    color: #1f2329;
    font-weight: 600;
    border: 1px solid #e6e8eb;
}
QTabBar::tab:hover:!selected {
    background: #f2f4f7;
    color: #374151;
}
QToolButton#tabClose {
    background: transparent;
    color: #9aa0a6;
    border: none;
    border-radius: 8px;
    padding: 0;
    margin: 0 2px;
    font-size: 13px;
    font-weight: 700;
    min-width: 24px;
    max-width: 24px;
    min-height: 24px;
    max-height: 24px;
}
QToolButton#tabClose:hover {
    background: #eceef1;
    color: #4b5563;
}
QToolButton#tabAdd {
    background: transparent;
    color: #6b7280;
    border: none;
    border-radius: 8px;
    font-size: 16px;
    font-weight: 600;
    min-width: 28px;
    min-height: 28px;
    padding: 0 4px;
}
QToolButton#tabAdd:hover {
    background: #f2f4f7;
    color: #1f2329;
}

/* —— 三档底色：壳 / 侧栏浅灰 / 主对话与右栏白 —— */
QWidget#chatSide {
    background: #f0f2f5;
}
QWidget#fileSide {
    background: #ffffff;
    border-left: 1px solid #eceef1;
}
QFrame#chatCard, QFrame#inputCard {
    background: #ffffff;
    border: 1px solid #eceef1;
    border-radius: 14px;
}
QFrame#inputCard[dropActive="true"] {
    background: #f5f8ff;
    border: 1px dashed #4d6bfe;
}
QFrame#logBar {
    background: #ffffff;
    border-top: 1px solid #eceef1;
}

/* —— 输入类 —— */
QTextEdit, QLineEdit, QListWidget, QTreeWidget, QListView, QTreeView, QComboBox {
    background: #ffffff;
    border: 1px solid #e6e8eb;
    border-radius: 12px;
    padding: 8px 12px;
    color: #1f2329;
    selection-background-color: #dbe4ff;
    selection-color: #1f2329;
}
QTextEdit#chatDisplay {
    background: transparent;
    border: none;
    padding: 0;
}
QLineEdit:focus, QTextEdit:focus, QComboBox:focus {
    border: 1px solid #4d6bfe;
}
QComboBox::drop-down {
    border: none;
    width: 22px;
}
QComboBox QAbstractItemView {
    border: 1px solid #e6e8eb;
    border-radius: 10px;
    background: #ffffff;
    padding: 4px;
    outline: none;
}

/* —— 按钮：默认就是“安静”的浅色按钮 —— */
QPushButton {
    background: #ffffff;
    color: #374151;
    border: 1px solid #e6e8eb;
    border-radius: 10px;
    padding: 6px 14px;
    font-weight: 500;
}
QPushButton:hover {
    background: #f5f6f8;
    border-color: #dcdfe4;
    color: #1f2329;
}
QPushButton:pressed {
    background: #eef0f3;
}
QPushButton:disabled {
    background: #f7f8fa;
    color: #b6bcc4;
    border-color: #eceef1;
}
QPushButton#btnSend {
    background: #4d6bfe;
    color: #ffffff;
    border: 1px solid #4d6bfe;
    border-radius: 10px;
    font-weight: 600;
    padding: 6px 10px;
    min-width: 64px;
    max-width: 64px;
}
QPushButton#btnSend:hover {
    background: #3f5ce0;
    border-color: #3f5ce0;
}
QPushButton#btnSend:disabled {
    background: #c9d3fb;
    border-color: #c9d3fb;
    color: #ffffff;
}
QPushButton#logClear, QPushButton#logExport {
    background: transparent;
    color: #6b7280;
    border: 1px solid transparent;
    border-radius: 7px;
    padding: 2px 8px;
    font-size: 11.5px;
}
QPushButton#logClear:hover, QPushButton#logExport:hover {
    background: #f2f4f7;
    color: #1f2329;
}
QPushButton#btnResume {
    background: #eef2ff;
    color: #3b57d6;
    border: 1px solid #dfe6ff;
    border-radius: 11px;
    font-weight: 600;
}
QPushButton#btnResume:hover {
    background: #e3e9ff;
}
QPushButton#btnEnd {
    background: #fff5f4;
    color: #b42318;
    border: 1px solid #fbe3e0;
}
QPushButton#btnEnd:hover {
    background: #ffecea;
}
QPushButton#btnExportQuery {
    background: #ffffff;
    color: #374151;
}

/* —— 工具栏 / 分栏 —— */
QToolBar {
    background: #ffffff;
    border: 1px solid #eceef1;
    border-radius: 12px;
    spacing: 4px;
    padding: 2px 6px;
    margin: 0 0 4px 0;
}
QToolButton, QToolBar QToolButton {
    background: transparent;
    border: none;
    border-radius: 8px;
    padding: 4px;
}
QToolButton:hover, QToolBar QToolButton:hover {
    background: #f2f4f7;
}
QSplitter::handle {
    background: transparent;
}
QSplitter::handle:horizontal {
    width: 8px;
}
QSplitter::handle:vertical {
    height: 8px;
}
QSplitter::handle:hover {
    background: #eceef1;
    border-radius: 4px;
}

/* —— 滚动条 —— */
QScrollBar:vertical {
    background: transparent;
    width: 8px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: #d9dce1;
    border-radius: 4px;
    min-height: 28px;
}
QScrollBar::handle:vertical:hover {
    background: #c3c7cd;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar:horizontal {
    background: transparent;
    height: 8px;
    margin: 2px;
}
QScrollBar::handle:horizontal {
    background: #d9dce1;
    border-radius: 4px;
    min-width: 28px;
}
QScrollBar::handle:horizontal:hover {
    background: #c3c7cd;
}

/* —— 标签 —— */
QLabel#scopeLabel {
    background: #f2f4f7;
    color: #5b6470;
    border: 1px solid #eceef1;
    border-radius: 8px;
    padding: 4px 9px;
    font-size: 11px;
}
QLabel#workspaceLabel {
    color: #6b7280;
    font-size: 11.5px;
}
QLabel#queryStatus {
    color: #6b7280;
    font-size: 11.5px;
}
QStatusBar, QLabel#statusBar {
    background: #ffffff;
    color: #8a9099;
    border-top: 1px solid #eceef1;
}
QMessageBox, QInputDialog {
    background: #ffffff;
}

/* —— 列表 / 树 —— */
QListWidget {
    padding: 4px;
}
QListWidget::item {
    border-radius: 8px;
    padding: 5px 8px;
}
QListWidget::item:hover, QListView::item:hover, QTreeView::item:hover,
QTreeWidget::item:hover {
    background: #f5f6f8;
    color: #1f2329;
}
QListWidget::item:selected, QListView::item:selected, QTreeView::item:selected,
QTreeWidget::item:selected {
    background: #eef2ff;
    color: #2b3f9e;
}

/* —— 资源管理器：更紧凑 —— */
QWidget#fileExplorer {
    font-size: 11.5px;
}
QWidget#fileExplorer QToolBar {
    padding: 1px 4px;
    margin: 0 0 2px 0;
}
QWidget#fileExplorer QListView,
QWidget#fileExplorer QTreeView,
QWidget#fileExplorer QTreeWidget {
    border-radius: 10px;
    padding: 2px;
    font-size: 11.5px;
}
QWidget#fileExplorer QTreeWidget::item,
QWidget#fileExplorer QTreeView::item {
    padding: 3px 4px;
    min-height: 20px;
}
QWidget#fileExplorer QListView::item {
    padding: 3px;
}
QWidget#fileExplorer QLabel {
    font-size: 11px;
    color: #8a9099;
    padding: 2px 4px;
}
QWidget#fileExplorer QComboBox {
    padding: 2px 8px;
    border-radius: 8px;
    min-height: 24px;
}
QWidget#fileExplorer QFrame#breadcrumbBar {
    background: transparent;
    border: none;
}
QWidget#fileExplorer QToolButton#crumbBtn {
    background: transparent;
    color: #4b5563;
    border: none;
    border-radius: 6px;
    padding: 2px 6px;
    font-size: 11.5px;
}
QWidget#fileExplorer QToolButton#crumbBtn:hover {
    background: #f2f4f7;
    color: #1f2329;
}
QWidget#fileExplorer QLabel#crumbSep {
    color: #9aa0a6;
    font-size: 11px;
    padding: 0 1px;
    background: transparent;
    border: none;
}

/* —— 附件条：轻量单行 —— */
QFrame#attachBar {
    background: #fbfbfc;
    border: 1px solid #eceef1;
    border-radius: 12px;
}
QLabel#attachHint {
    color: #8a9099;
    font-size: 11px;
    border: none;
    background: transparent;
}
QPushButton#attachBtn, QPushButton#attachClear {
    background: transparent;
    color: #6b7280;
    border: 1px solid transparent;
    border-radius: 7px;
    padding: 2px 8px;
    font-size: 11.5px;
    font-weight: 500;
}
QPushButton#attachBtn:hover, QPushButton#attachClear:hover {
    background: #f2f4f7;
    color: #1f2329;
}
QPushButton#attachChip {
    background: #f2f4f7;
    color: #4b5563;
    border: 1px solid #e9ebef;
    border-radius: 999px;
    padding: 2px 10px;
    font-size: 11.5px;
    font-weight: 500;
}
QPushButton#attachChip:hover {
    background: #eceef1;
    color: #1f2329;
}

/* —— 思考过程：安静、可折叠 —— */
QFrame#thinkingPanel {
    background: transparent;
    border: none;
    border-top: 1px solid #f0f1f3;
}
QToolButton#thinkingToggle {
    background: transparent;
    color: #6b7280;
    border: none;
    text-align: left;
    font-size: 12px;
    padding: 2px 2px;
}
QToolButton#thinkingToggle:hover {
    color: #1f2329;
}
QToolButton#thinkingToggle:checked {
    color: #374151;
    font-weight: 600;
}
QPushButton#btnAbortThink, QPushButton#btnUndoRound {
    background: transparent;
    color: #8a9099;
    border: 1px solid transparent;
    border-radius: 7px;
    padding: 2px 8px;
    font-size: 11.5px;
    font-weight: 500;
}
QPushButton#btnAbortThink:hover {
    background: #fff5f4;
    color: #b42318;
}
QPushButton#btnUndoRound:hover {
    background: #f2f4f7;
    color: #374151;
}
QPushButton#btnAbortThink:disabled, QPushButton#btnUndoRound:disabled {
    background: transparent;
    color: #c4c9d0;
}
"""


def apply_app_style(app_or_widget):
    """给 QApplication 或顶层窗口套用主题。选中色走调色板，避免富文本选区发灰。"""
    from PyQt6.QtGui import QColor, QPalette
    app_or_widget.setStyleSheet(APP_QSS)
    pal = app_or_widget.palette()
    pal.setColor(QPalette.ColorRole.Highlight, QColor("#dbe4ff"))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#1f2329"))
    app_or_widget.setPalette(pal)
