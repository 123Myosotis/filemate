# ui/main_window.py
"""
FileMate 主窗口
布局：左侧对话频道（Tab：一频道一工作区 + 对话区 + 输入）
     右侧文件管理器（保留）+ 操作日志
"""
from datetime import datetime  # 给操作日志打时间戳
from pathlib import Path  # 路径处理（日志里显示文件名等）
import os
import shutil  # 撤回操作时用 shutil.move 把文件移回原位

from PyQt6.QtCore import Qt, QEvent, QThread, QTimer, QUrl, pyqtSignal  # Qt 常量 + 后台工作线程 + 定时器
from PyQt6.QtGui import QGuiApplication, QDesktopServices  # 屏幕尺寸 / 打开日志文件
from PyQt6.QtGui import QBrush, QColor, QPalette
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,  # 主窗口与布局/分割器
    QTabWidget, QTabBar, QTextEdit, QLineEdit, QPushButton, QListWidget,  # 频道页、对话区、输入框、按钮、日志列表
    QListWidgetItem, QMenu, QMessageBox, QInputDialog, QLabel, QToolButton,  # 日志条目、右键菜单、弹窗、提示条
    QDialog, QFormLayout, QDoubleSpinBox, QSpinBox, QDialogButtonBox,
    QComboBox, QCheckBox, QGroupBox, QFileDialog, QScrollArea, QFrame,
    QSizePolicy,
)
import re as _re_ui

import config  # 本地配置读写（API Key）
from db import database as db  # SQLite 持久化：频道/消息/日志
from engine.executor import AIFileAssistant  # AI 助手
from engine.dispatcher import IntentDispatcher  # A 级整理走契约；查表/改 Word 走模型
from engine.file_organize_apply import apply_moves, plan_moves, preview_text
from ui.file_explorer import FileExplorer, get_desktop_path  # 文件管理器组件与桌面路径工具
from ui.style import apply_app_style  # 简洁圆角主题
from ui.entry_copy import WELCOME_HTML, INPUT_PLACEHOLDER, HELP_HTML
from ui.chat_attachments import (
    format_attach_note, merge_file_paths, paths_from_strings, paths_from_urls,
)
from ui.thinking_panel import ThinkingPanel
from ui.round_undo import (
    apply_file_undos, collect_from_result, empty_round, has_undoable, merge_rounds,
)
from ui.workspace_resolve import format_ambiguous_message, resolve_names_in_workspace
import time as _time


class _DispatchWorker(QThread):
    """把 dispatcher.dispatch 放到后台线程，避免 UI 假死。"""
    finished_result = pyqtSignal(dict)
    progress = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, dispatcher, text, target, control=None,
                 channel_id=None, resume=False, file_paths=None,
                 ask_history=None, confirmed=False):
        super().__init__()
        self._dispatcher = dispatcher
        self._text = text
        self._target = target
        self._control = control
        self._channel_id = channel_id
        self._resume = resume
        self._file_paths = list(file_paths or [])
        self._ask_history = list(ask_history or []) if ask_history else None
        self._confirmed = bool(confirmed)

    def run(self):
        try:
            result = self._dispatcher.dispatch(
                self._text, self._target, progress=self.progress.emit,
                control=self._control, channel_id=self._channel_id,
                resume=self._resume,
                file_paths=self._file_paths or None,
                ask_history=self._ask_history,
                confirmed=self._confirmed)
            if not isinstance(result, dict):
                result = {
                    "handler": "系统",
                    "message": "内部错误：dispatch 未返回字典（%r）。" % (type(result),),
                    "operations": [],
                    "tool": "error",
                    "reason": "bad_result_type",
                    "ok": False,
                }
            # 缩小跨线程载荷：明细留 message / report_paths 即可
            slim = dict(result)
            for heavy in ("classifications", "sections"):
                if heavy in slim and isinstance(slim[heavy], list) and len(slim[heavy]) > 40:
                    slim[heavy] = slim[heavy][:40]
            self.finished_result.emit(slim)
        except Exception as e:
            self.failed.emit(str(e))


class MainWindow(QMainWindow):
    """
    整体布局：
    ┌─────────────────────────────────────────────┐
    │  [水平分割器]                                │
    │  ┌──────────────┬──────────────────────────┐│
    │  │ 左侧对话频道  │ 右侧垂直分割器            ││
    │  │ ┌──────────┐ │ ┌──────────────────────┐││
    │  │ │ 对话记录  │ │ │ 文件管理器            │││
    │  │ ├──────────┤ │ ├──────────────────────┤││
    │  │ │ 输入+发送 │ │ │ 操作日志              │││
    │  │ └──────────┘ │ └──────────────────────┘││
    │  └──────────────┴──────────────────────────┘│
    └─────────────────────────────────────────────┘
    """

    def __init__(self):
        super().__init__()
        db.init_db()  # 初始化 SQLite（幂等），保证频道/消息/日志表存在
        self.setWindowTitle("FileMate")  # 窗口标题
        apply_app_style(self)  # 简洁圆角主题
        # 根据屏幕可用区域自适应窗口大小，避免超出屏幕被遮住
        # 取屏幕可用宽高的 85%，并不超过 1350x830
        screen = QGuiApplication.primaryScreen().availableGeometry()  # 主屏幕可用区域（排除任务栏）
        width = min(1350, int(screen.width() * 0.85))  # 宽度：屏幕的 85%，上限 1350
        height = min(830, int(screen.height() * 0.85))  # 高度：屏幕的 85%，上限 830
        self.resize(width, height)  # 应用计算出的窗口尺寸
        self.setMinimumSize(720, 440)  # 允许更小窗口；分割条仍可自由拖

        # 全局唯一的 AI 助手实例，API Key 从本地配置读取
        self.ai_assistant = AIFileAssistant(
            api_key=config.get_api_key(),
            base_dir=get_desktop_path()  # 默认操作目录为真实桌面
        )
        # 意图调度器：A 级整理走契约规则（0 token）；查表/改 Word 走模型
        self.dispatcher = IntentDispatcher(self.ai_assistant)

        self._worker = None  # 当前后台任务（防止重复发送）
        self._task_control = None  # 当前 TaskControl（结束/暂停/续跑）
        self._last_task_text = ""
        self._last_task_target = None
        self._last_channel_id = None
        self._active_tab = None
        # table_query needs_user 续跑态（原输入框回复，不另开查询按钮）
        self._pending_table_query = None
        self._last_progress_msg = ""
        self._last_export_path = None
        # P1：处理中秒表（单定时器刷新当前任务频道）
        self._run_timer = QTimer(self)
        self._run_timer.setInterval(1000)
        self._run_timer.timeout.connect(self._on_run_tick)
        self._run_tab = None
        self._run_started_at = None

        # ===== 菜单栏：设置 =====
        settings_menu = self.menuBar().addMenu("设置")  # 创建"设置"菜单
        # M5.2: 模型 / MaiBot / 自学习统一迁至 Web「设置」Tab；PyQt 仅保留启动器与只读提示
        api_action = settings_menu.addAction("聊天模型设置…")
        api_action.triggered.connect(self._edit_chat_model_settings)
        test_action = settings_menu.addAction("测试模型连通性")
        test_action.triggered.connect(self._test_api)
        budget_action = settings_menu.addAction("预算与重试参数…")
        budget_action.triggered.connect(self._edit_budget_params)
        web_action = settings_menu.addAction("本地 Web…")
        web_action.triggered.connect(self._edit_local_web)  # 记忆纠错 + 跨表查询，主窗口入口
        maibot_dir_action = settings_menu.addAction("记忆 / 向量设置…")
        maibot_dir_action.triggered.connect(self._edit_memory_settings)
        clean_action = settings_menu.addAction("表格清洗设置…")
        clean_action.triggered.connect(self._edit_table_cleaning_settings)
        cache_action = settings_menu.addAction("清理分类缓存…")
        cache_action.triggered.connect(self._cleanup_content_cache)

        central_widget = QWidget()  # QMainWindow 需要一个中央部件来承载布局
        self.setCentralWidget(central_widget)
        outer_layout = QVBoxLayout(central_widget)  # 最外层垂直布局
        outer_layout.setContentsMargins(10, 10, 10, 10)
        outer_layout.setSpacing(8)

        # ===== 主水平分割器（对话为主，资源管理可大幅拖窄/拖宽）=====
        self.main_h_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_h_splitter.setHandleWidth(7)
        self.main_h_splitter.setChildrenCollapsible(True)  # 允许收到极窄，自由缩放

        # ---------- 左侧：对话频道（浅灰壳） ----------
        left_panel = QWidget()
        left_panel.setObjectName("chatSide")
        left_panel.setMinimumWidth(280)  # 对话下限，仍可拖得很窄
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(8, 8, 4, 8)
        left_layout.setSpacing(0)

        self.chat_tabs = QTabWidget()  # 多频道能力保留；单频道时隐藏 Tab 栏
        self.chat_tabs.setTabsClosable(True)
        self.chat_tabs.setMovable(True)
        self.chat_tabs.setElideMode(Qt.TextElideMode.ElideRight)
        self.chat_tabs.setUsesScrollButtons(True)
        bar = self.chat_tabs.tabBar()
        bar.setExpanding(False)
        bar.setDocumentMode(True)
        self.chat_tabs.tabCloseRequested.connect(self._close_tab)
        self.chat_tabs.currentChanged.connect(self._on_channel_changed)
        # 新建频道「+」放在 Tab 尾部，不再占每频道工作区条
        self._tab_add_btn = QToolButton()
        self._tab_add_btn.setObjectName("tabAdd")
        self._tab_add_btn.setText("+")
        self._tab_add_btn.setToolTip("新建频道（先选工作区）")
        self._tab_add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._tab_add_btn.clicked.connect(lambda: self.add_chat_tab())
        self.chat_tabs.setCornerWidget(self._tab_add_btn, Qt.Corner.TopRightCorner)
        left_layout.addWidget(self.chat_tabs)

        # ---------- 右侧：文件管理器 + 操作日志（白底） ----------
        right_panel = QWidget()
        right_panel.setObjectName("fileSide")
        right_panel.setMinimumWidth(140)  # 可收到很窄；想放大资源管理时也能拖很宽
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        # 操作范围提示条
        self.scope_label = QLabel()
        self.scope_label.setObjectName("scopeLabel")
        right_layout.addWidget(self.scope_label)

        self.right_v_splitter = QSplitter(Qt.Orientation.Vertical)
        self.right_v_splitter.setHandleWidth(7)
        self.right_v_splitter.setChildrenCollapsible(True)

        self.file_explorer = FileExplorer()  # 文件管理器组件
        self.file_explorer.organize_requested.connect(self._on_organize)  # 点击"整理"按钮 → 执行规则整理
        self.file_explorer.current_path_changed.connect(self._on_path_changed)  # 目录切换 → 更新操作范围提示
        self.file_explorer.attach_to_chat_requested.connect(self._on_explorer_attach)

        # P3：操作日志可折叠；分级着色 + 清空/导出
        self.log_list = QListWidget()
        self.log_list.setObjectName("logList")
        self.log_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.log_list.customContextMenuRequested.connect(self._show_log_menu)
        self._load_logs()

        self._log_wrap = QFrame()
        self._log_wrap.setObjectName("logBar")
        log_wrap_layout = QVBoxLayout(self._log_wrap)
        log_wrap_layout.setContentsMargins(6, 4, 6, 4)
        log_wrap_layout.setSpacing(2)
        log_head = QHBoxLayout()
        log_head.setSpacing(4)
        self._log_toggle_btn = QToolButton()
        self._log_toggle_btn.setText("操作日志 ▾")
        self._log_toggle_btn.setCheckable(True)
        self._log_toggle_btn.setChecked(True)
        self._log_toggle_btn.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._log_toggle_btn.setStyleSheet(
            "QToolButton { color:#57534e; font-size:12px; padding:2px 4px;"
            " border:none; text-align:left; }"
            "QToolButton:checked { font-weight:600; }")
        self._log_toggle_btn.setToolTip("折叠/展开操作日志（撤回入口在这里）")
        self._log_toggle_btn.toggled.connect(self._on_log_toggled)
        log_clear_btn = QPushButton("清空")
        log_clear_btn.setObjectName("logClear")
        log_clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        log_clear_btn.setToolTip("清空全部操作日志")
        log_clear_btn.clicked.connect(self._clear_logs)
        log_export_btn = QPushButton("导出")
        log_export_btn.setObjectName("logExport")
        log_export_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        log_export_btn.setToolTip("导出为操作日志.docx")
        log_export_btn.clicked.connect(self._export_logs_docx)
        log_head.addWidget(self._log_toggle_btn, 1)
        log_head.addWidget(log_clear_btn)
        log_head.addWidget(log_export_btn)
        log_wrap_layout.addLayout(log_head)
        log_wrap_layout.addWidget(self.log_list, 1)

        self.right_v_splitter.addWidget(self.file_explorer)
        self.right_v_splitter.addWidget(self._log_wrap)
        self.file_explorer.setMinimumHeight(80)
        self._log_wrap.setMinimumHeight(28)
        self.right_v_splitter.setStretchFactor(0, 4)
        self.right_v_splitter.setStretchFactor(1, 1)
        self.right_v_splitter.setSizes([420, 100])
        self._log_expanded_sizes = [420, 100]
        right_layout.addWidget(self.right_v_splitter)

        self.main_h_splitter.addWidget(left_panel)
        self.main_h_splitter.addWidget(right_panel)
        # 默认对话约 68%、资源管理约 32%；拉伸时对话优先变宽
        self.main_h_splitter.setStretchFactor(0, 3)
        self.main_h_splitter.setStretchFactor(1, 1)
        chat_w = max(int(width * 0.68), 420)
        right_w = max(width - chat_w - 24, 220)
        self.main_h_splitter.setSizes([chat_w, right_w])

        outer_layout.addWidget(self.main_h_splitter)

        # 启动恢复频道（批量恢复时不弹工作区框，避免连弹）
        self._restoring_channels = True
        try:
            channels = db.list_channels()
            if channels:
                for row in channels:
                    ch_id, name = row[0], row[1]
                    ws = row[2] if len(row) > 2 else None
                    self.add_chat_tab(
                        name=name, channel_id=ch_id, load_history=True,
                        workspace_path=ws, pick_workspace=False)
            else:
                self.add_chat_tab(pick_workspace=True)
        finally:
            self._restoring_channels = False

        # 当前频道：缺工作区则打开就弹；再同步资源管理器
        QTimer.singleShot(0, self._bootstrap_active_workspace)
        QTimer.singleShot(100, self._focus_first_tab_input)

    def _focus_first_tab_input(self):
        """窗口启动后聚焦第一个频道的输入框"""
        self._focus_tab_input(self.chat_tabs.currentIndex())

    def _bootstrap_active_workspace(self):
        """启动后：当前频道缺工作区则立刻弹选；再同步资源管理器。"""
        idx = self.chat_tabs.currentIndex()
        tab = self.chat_tabs.widget(idx)
        if tab is None:
            return
        self._ensure_tab_workspace(tab, required=True)
        self._sync_explorer_to_tab(tab)

    def _on_channel_changed(self, idx: int):
        """切 Tab：聚焦 + 缺工作区则弹选 + 资源管理器跟到该频道工作区。"""
        self._focus_tab_input(idx)
        if getattr(self, "_restoring_channels", False):
            return
        tab = self.chat_tabs.widget(idx)
        if tab is None:
            return
        self._ensure_tab_workspace(tab, required=False)
        self._sync_explorer_to_tab(tab)

    def _pick_workspace_dir(self, title: str, *, required: bool = False) -> Path | None:
        """选目录作为工作区。required=True 时取消会再弹，直到选中或窗口关掉。"""
        start = str(get_desktop_path())
        while True:
            path = QFileDialog.getExistingDirectory(self, title, start)
            if path:
                p = Path(path)
                if p.is_dir():
                    return p
                QMessageBox.warning(self, "工作区无效", "请选择一个存在的文件夹。")
                continue
            if not required:
                return None
            # 必须有工作区才能用：再提示一次
            ans = QMessageBox.question(
                self, "需要工作区",
                "每个频道必须先选择工作区才能对话。\n是否重新选择？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if ans != QMessageBox.StandardButton.Yes:
                # 仍 required：继续循环，避免无工作区空窗
                continue

    def _ensure_tab_workspace(self, tab, *, required: bool = False) -> bool:
        """频道无有效工作区则弹选；取消则禁用发送。"""
        ws = str(tab.property("workspace_path") or "").strip()
        if ws and Path(ws).is_dir():
            self._apply_workspace_gate(tab)
            return True
        picked = self._pick_workspace_dir(
            "请为本频道选择工作区",
            required=required,
        )
        if not picked:
            tab.setProperty("workspace_path", "")
            self._refresh_workspace_label(tab)
            self._apply_workspace_gate(tab)
            return False
        ws = str(picked)
        tab.setProperty("workspace_path", ws)
        channel_id = tab.property("channel_id")
        if channel_id is not None:
            db.set_channel_workspace(channel_id, ws)
        self._refresh_workspace_label(tab)
        self._apply_workspace_gate(tab)
        return True

    def _change_workspace(self, tab):
        """更换本频道工作区（不新建频道）。"""
        picked = self._pick_workspace_dir("更换本频道工作区", required=False)
        if not picked:
            return
        ws = str(picked)
        tab.setProperty("workspace_path", ws)
        channel_id = tab.property("channel_id")
        if channel_id is not None:
            db.set_channel_workspace(channel_id, ws)
        self._refresh_workspace_label(tab)
        self._apply_workspace_gate(tab)
        if tab is self.chat_tabs.currentWidget():
            self._sync_explorer_to_tab(tab)

    def _refresh_workspace_label(self, tab):
        label = tab.property("workspace_label")
        ws = str(tab.property("workspace_path") or "").strip()
        if label is None:
            return
        if ws and Path(ws).is_dir():
            label.setText("工作区：%s" % ws)
        else:
            label.setText("工作区：未选择（请先选择文件夹）")

    def _apply_workspace_gate(self, tab):
        """无工作区：禁用输入/发送（打开就闸住，避免误发）。"""
        ws = str(tab.property("workspace_path") or "").strip()
        ok = bool(ws and Path(ws).is_dir())
        input_box = tab.property("input_box")
        send_btn = tab.property("send_btn")
        if input_box is not None:
            input_box.setEnabled(ok)
            input_box.setPlaceholderText(
                INPUT_PLACEHOLDER if ok else "请先选择本频道工作区…")
        if send_btn is not None:
            send_btn.setEnabled(ok)
        status = tab.property("status_label")
        if status is not None and not ok:
            status.setText("请先选择工作区，才能发送指令。")
            status.show()
        elif status is not None and ok and "工作区" in (status.text() or ""):
            status.hide()

    def _sync_explorer_to_tab(self, tab):
        """资源管理器跳到该频道工作区根（保留资源管理器，只同步范围）。"""
        ws = str(tab.property("workspace_path") or "").strip()
        if ws and Path(ws).is_dir():
            self.file_explorer.load_directory(ws)
            self._on_path_changed(ws)
        else:
            self._on_path_changed("此电脑")

    def add_chat_tab(self, name=None, channel_id=None, load_history=False,
                     workspace_path=None, pick_workspace=True):
        """新增聊天频道。新建时强迫先选工作区；取消且已有其它频道则不建。

        参数:
            name / channel_id / load_history: 恢复历史用
            workspace_path: 已存工作区（恢复时传入）
            pick_workspace: 新建时是否立刻弹目录框（默认 True）
        """
        tab_name = name or self._next_channel_name()
        ws_path = str(workspace_path).strip() if workspace_path else ""
        if ws_path and not Path(ws_path).is_dir():
            ws_path = ""

        # 新建：先选工作区，取消则不建（若已无频道则死循环直到选中）
        if channel_id is None and pick_workspace:
            picked = self._pick_workspace_dir(
                "选择本频道工作区",
                required=(self.chat_tabs.count() == 0),
            )
            if not picked:
                return None
            ws_path = str(picked)

        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        # 工作区顶条：路径 | 新建频道 | 使用说明 | 定位 | 更换
        ws_row = QHBoxLayout()
        ws_row.setContentsMargins(0, 0, 0, 0)
        ws_row.setSpacing(6)
        ws_label = QLabel()
        ws_label.setObjectName("workspaceLabel")
        ws_label.setWordWrap(True)
        ws_label.setStyleSheet("color:#6b7280; font-size:11.5px; padding:2px 0;")
        help_btn = QPushButton("使用说明")
        help_btn.setFixedHeight(28)
        help_btn.setToolTip("查看完整使用说明")
        ws_home_btn = QPushButton("定位工作区")
        ws_home_btn.setFixedHeight(28)
        ws_home_btn.setToolTip("资源管理器回到本频道工作区根目录")
        ws_btn = QPushButton("更换工作区")
        ws_btn.setFixedHeight(28)
        ws_btn.setToolTip("只改本频道工作区；不新建频道")
        ws_row.addWidget(ws_label, 1)
        ws_row.addWidget(help_btn)
        ws_row.addWidget(ws_home_btn)
        ws_row.addWidget(ws_btn)

        chat_card = QFrame()
        chat_card.setObjectName("chatCard")
        chat_card_layout = QVBoxLayout(chat_card)
        chat_card_layout.setContentsMargins(8, 8, 8, 4)
        chat_card_layout.setSpacing(4)

        chat_display = QTextEdit()
        chat_display.setObjectName("chatDisplay")
        chat_display.setReadOnly(True)
        chat_display.setFrameShape(QFrame.Shape.NoFrame)
        chat_display.document().setDocumentMargin(12)
        chat_display.setViewportMargins(8, 4, 8, 4)
        pal = chat_display.palette()
        pal.setColor(QPalette.ColorRole.Highlight, QColor("#dbe4ff"))
        pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#1f2329"))
        chat_display.setPalette(pal)

        history = db.list_messages(channel_id) if (load_history and channel_id is not None) else []
        if history:
            for sender, content in history:
                self._append_bubble(chat_display, content, sender)
        else:
            chat_display.setHtml(WELCOME_HTML)

        thinking = ThinkingPanel()
        # 信号在 tab 建好后连接（见下方）

        status_label = QLabel("")
        status_label.setObjectName("queryStatus")
        status_label.setWordWrap(True)
        status_label.setStyleSheet(
            "color:#64748b; font-size:11.5px; padding:2px 4px;"
        )
        status_label.hide()

        chat_card_layout.addWidget(chat_display, 1)
        chat_card_layout.addWidget(thinking)
        chat_card_layout.addWidget(status_label)

        # 输入区卡片：平时无虚线；拖入时高亮 Drop Zone
        input_card = QFrame()
        input_card.setObjectName("inputCard")
        input_card.setProperty("dropActive", False)
        # 供拖拽高亮精确定位（平时无虚线框）
        tab.setProperty("input_card", input_card)
        input_outer = QVBoxLayout(input_card)
        input_outer.setContentsMargins(8, 6, 8, 8)
        input_outer.setSpacing(4)

        # P2：附件条（拖入对话 / 附加按钮）
        attach_frame = QFrame()
        attach_frame.setObjectName("attachBar")
        attach_outer = QVBoxLayout(attach_frame)
        attach_outer.setContentsMargins(10, 4, 8, 4)
        attach_outer.setSpacing(2)
        attach_head = QHBoxLayout()
        attach_head.setSpacing(4)
        attach_hint = QLabel("拖入文件，或点「附加」")
        attach_hint.setObjectName("attachHint")
        attach_btn = QPushButton("附加")
        attach_btn.setObjectName("attachBtn")
        attach_btn.setFixedHeight(24)
        attach_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        attach_clear = QPushButton("清空")
        attach_clear.setObjectName("attachClear")
        attach_clear.setFixedHeight(24)
        attach_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        attach_head.addWidget(attach_hint, 1)
        attach_head.addWidget(attach_btn)
        attach_head.addWidget(attach_clear)
        chips_host = QWidget()
        chips_layout = QHBoxLayout(chips_host)
        chips_layout.setContentsMargins(0, 0, 0, 0)
        chips_layout.setSpacing(4)
        chips_scroll = QScrollArea()
        chips_scroll.setWidgetResizable(True)
        chips_scroll.setFrameShape(QFrame.Shape.NoFrame)
        chips_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        chips_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        chips_scroll.setFixedHeight(30)
        chips_scroll.setWidget(chips_host)
        attach_outer.addLayout(attach_head)
        attach_outer.addWidget(chips_scroll)

        input_row = QHBoxLayout()
        input_row.setSpacing(8)
        input_box = QLineEdit()
        input_box.setPlaceholderText(INPUT_PLACEHOLDER)
        input_box.setMinimumHeight(40)
        send_btn = QPushButton("发送")
        send_btn.setObjectName("btnSend")
        send_btn.setMinimumHeight(40)
        send_btn.setFixedWidth(64)
        send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        resume_btn = QPushButton("重新")
        resume_btn.setObjectName("btnResume")
        resume_btn.setMinimumHeight(40)
        resume_btn.setFixedWidth(64)
        resume_btn.hide()
        export_btn = QPushButton("导出 Excel")
        export_btn.setObjectName("btnExportQuery")
        export_btn.setMinimumHeight(40)
        export_btn.setFixedWidth(104)
        export_btn.hide()
        export_btn.setToolTip("打开最近一次跨表查询导出的结果表")
        input_row.addWidget(input_box)
        input_row.addWidget(send_btn)
        input_row.addWidget(resume_btn)
        input_row.addWidget(export_btn)

        input_outer.addWidget(attach_frame)
        input_outer.addLayout(input_row)

        layout.addLayout(ws_row)
        layout.addWidget(chat_card, 1)
        layout.addWidget(input_card)

        index = self.chat_tabs.addTab(tab, tab_name)
        self._attach_tab_close(index)
        self.chat_tabs.setCurrentIndex(index)
        self._sync_tab_bar_visibility()

        if channel_id is None:
            channel_id = db.add_channel(tab_name, workspace_path=ws_path or None)
        elif ws_path:
            db.set_channel_workspace(channel_id, ws_path)

        tab.setProperty("channel_id", channel_id)
        tab.setProperty("workspace_path", ws_path or "")
        tab.setProperty("workspace_label", ws_label)
        tab.setProperty("send_btn", send_btn)
        tab.setProperty("resume_btn", resume_btn)
        tab.setProperty("export_btn", export_btn)
        tab.setProperty("input_box", input_box)
        tab.setProperty("chat_display", chat_display)
        tab.setProperty("status_label", status_label)
        tab.setProperty("thinking_panel", thinking)
        tab.setProperty("attach_chips_layout", chips_layout)
        tab.setProperty("input_card", input_card)
        tab.setProperty("export_path", "")
        tab.attach_paths = []  # 本频道待发送附件（不入库）
        tab.round_undo = empty_round()  # 最近一轮可撤回快照（接入层）

        self._refresh_workspace_label(tab)
        self._apply_workspace_gate(tab)
        self._rebuild_attach_chips(tab)

        thinking.abort_clicked.connect(lambda t=tab: self._on_abort_thinking(t))
        thinking.undo_clicked.connect(lambda t=tab: self._on_undo_round(t))

        send_btn.clicked.connect(
            lambda: self._on_primary_btn(chat_display, input_box, send_btn, resume_btn))
        resume_btn.clicked.connect(
            lambda: self._on_resume(chat_display, input_box, send_btn, resume_btn))
        input_box.returnPressed.connect(
            lambda: self._on_primary_btn(chat_display, input_box, send_btn, resume_btn))
        export_btn.clicked.connect(
            lambda _checked=False, t=tab: self._open_query_export(t))
        help_btn.clicked.connect(self._show_help)
        ws_btn.clicked.connect(lambda _checked=False, t=tab: self._change_workspace(t))
        ws_home_btn.clicked.connect(lambda _checked=False, t=tab: self._sync_explorer_to_tab(t))
        attach_btn.clicked.connect(lambda _checked=False, t=tab: self._pick_attachments(t))
        attach_clear.clicked.connect(lambda _checked=False, t=tab: self._clear_attachments(t))
        input_box.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # 拖入：对话区 / 视口 / 附件条 / 输入框 / 输入卡片（须 DragMove）
        drop_targets = [
            chat_display, chat_display.viewport(), attach_frame,
            chips_scroll, chips_scroll.viewport(), input_box, input_card,
        ]
        for w in drop_targets:
            w.setAcceptDrops(True)
            w.installEventFilter(self)
        return tab

    def _sync_tab_bar_visibility(self) -> None:
        """单频道隐藏 Tab 栏；多频道才显示（+ 始终在角上）。"""
        n = self.chat_tabs.count()
        self.chat_tabs.tabBar().setVisible(n > 1)

    def _attach_tab_close(self, index: int):
        """关闭钮作为标签内部件；热区 24px。"""
        btn = QToolButton(self.chat_tabs.tabBar())
        btn.setObjectName("tabClose")
        btn.setText("×")
        btn.setFixedSize(24, 24)
        btn.setAutoRaise(True)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(self._close_from_button)
        self.chat_tabs.tabBar().setTabButton(
            index, QTabBar.ButtonPosition.RightSide, btn)

    def _close_from_button(self):
        btn = self.sender()
        bar = self.chat_tabs.tabBar()
        for i in range(self.chat_tabs.count()):
            if bar.tabButton(i, QTabBar.ButtonPosition.RightSide) is btn:
                self._close_tab(i)
                return

    def _next_channel_name(self) -> str:
        """取当前 Tab 中未占用的最小正整数番号（缺 1 补 1，有 2 则开 3）。"""
        import re as _re
        used = set()
        for i in range(self.chat_tabs.count()):
            title = self.chat_tabs.tabText(i) or ""
            m = _re.search(r"(\d+)", title)
            if m:
                used.add(int(m.group(1)))
        n = 1
        while n in used:
            n += 1
        return f"频道 {n}"

    def _focus_tab_input(self, idx):
        """当 Tab 切换到指定频道时，将焦点设到该频道的输入框"""
        tab = self.chat_tabs.widget(idx)  # 按索引取出频道页面
        if tab:
            box = tab.findChild(QLineEdit)  # 在页面内查找输入框
            if box:
                box.setFocus()

    def _close_tab(self, idx):
        """关闭指定频道：从数据库删除频道及消息，并移除 Tab。
        至少保留一个频道，全部关闭时自动新建一个。"""
        tab = self.chat_tabs.widget(idx)
        if tab is None:
            return
        channel_id = tab.property("channel_id")
        # 先移除 UI，再删数据库，避免 UI 残留
        self.chat_tabs.removeTab(idx)
        if channel_id is not None:
            db.delete_channel(channel_id)
        # 全部关闭后自动新建（会强迫选工作区）
        if self.chat_tabs.count() == 0:
            self.add_chat_tab(pick_workspace=True)
        else:
            self._sync_tab_bar_visibility()

    @staticmethod
    def _format_chat_body(text: str) -> str:
        """路径 → code chip；数量加粗。不做多色。"""
        import html as _html
        import re as _re

        escaped = _html.escape(text or "")

        def _chip(m):
            return (
                "<span style='font-family:Consolas,Cascadia Mono,monospace;"
                "font-size:12px;background:#f3f4f6;color:#374151;"
                "padding:1px 6px;border-radius:6px;'>"
                f"{m.group(0)}</span>"
            )

        # Windows 盘符 / UNC / 常见成稿相对段
        escaped = _re.sub(
            r"(?:[A-Za-z]:\\|\\\\)[^\s<>\"|?*]+"
            r"|(?:桌面|内容分类|内容拆分|模板成稿)(?:[/\\][^\s<>\"|?*]+)+",
            _chip,
            escaped,
        )
        escaped = _re.sub(
            r"(?<!\w)(\d+)(\s*(?:份|个|条|次|篇|项))",
            r"<b>\1</b>\2",
            escaped,
        )
        return escaped.replace("\n", "<br>")

    @staticmethod
    def _is_blocking_error(text: str) -> bool:
        t = str(text or "")
        keys = ("⚠️", "🔑", "失败", "无法", "未安装", "请先选", "请配置", "错误：")
        return any(k in t for k in keys)

    def _append_bubble(self, chat_display, text, sender):
        """往对话区追加消息：用户贴右；助手无底框；阻断错误浅条+竖线。"""
        body = self._format_chat_body(text)
        if sender == "user":
            long = len(text or "") > 40 or "\n" in (text or "")
            w_attr = " width='78%'" if long else ""
            block = (
                "<table width='100%' cellspacing='0' cellpadding='0'><tr>"
                "<td align='right' style='padding:4px 2px 4px 12px;'>"
                "<table" + w_attr + " cellspacing='0' cellpadding='0' align='right' "
                "bgcolor='#f2f3f5'>"
                "<tr><td style='padding:10px 14px; color:#1f2329; font-size:13px;"
                " font-weight:400; line-height:1.65;'>" + body + "</td></tr>"
                "</table></td></tr></table>"
            )
        elif self._is_blocking_error(text):
            block = (
                "<table width='100%' cellspacing='0' cellpadding='0' "
                "style='margin:6px 0;'><tr>"
                "<td width='3' bgcolor='#f04438'></td>"
                "<td bgcolor='#fff5f4' style='padding:10px 12px; color:#1f2329;"
                " line-height:1.75; font-size:13.5px;'>" + body + "</td>"
                "</tr></table>"
            )
        else:
            block = (
                "<div style='max-width:720px; padding:10px 4px 18px 2px; color:#1f2329;"
                " line-height:1.75; font-size:13.5px'>" + body + "</div>"
            )
        chat_display.append(block)
        scrollbar = chat_display.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _set_tab_status(self, tab, text: str, *, visible: bool = True) -> None:
        """频道内进度条（不刷屏气泡）；进度同时写入操作日志。"""
        if tab is None:
            return
        label = tab.property("status_label")
        if label is None:
            return
        msg = (text or "").strip()
        if visible and msg:
            label.setText("⏳ " + msg)
            label.show()
        else:
            label.setText("")
            label.hide()

    # ------------------------------------------------------------------
    # P1：处理中计时（只本地秒表，不调模型）
    # ------------------------------------------------------------------

    def _start_run_timer(self, tab, base: str = "处理中") -> None:
        self._run_tab = tab
        self._run_started_at = _time.monotonic()
        if tab is not None:
            tab.setProperty("run_base_status", base)
        if not self._run_timer.isActive():
            self._run_timer.start()
        self._refresh_run_status()

    def _on_run_tick(self) -> None:
        self._refresh_run_status()

    def _refresh_run_status(self) -> None:
        tab = self._run_tab
        if tab is None or self._run_started_at is None:
            return
        elapsed = max(0, int(_time.monotonic() - self._run_started_at))
        mm, ss = divmod(elapsed, 60)
        base = str(tab.property("run_base_status") or "处理中").strip() or "处理中"
        # 去掉旧的「· m:ss」后缀，避免叠加
        if " · " in base:
            base = base.split(" · ", 1)[0].strip()
        self._set_tab_status(tab, "%s · %d:%02d" % (base, mm, ss))

    def _stop_run_timer(self, tab, *, ok: bool = True) -> int:
        """停止秒表；状态条定格「完成/结束 · 用时」。返回秒数。"""
        if self._run_timer.isActive():
            self._run_timer.stop()
        elapsed = 0
        if self._run_started_at is not None:
            elapsed = max(0, int(_time.monotonic() - self._run_started_at))
        mm, ss = divmod(elapsed, 60)
        if tab is not None:
            self._set_tab_status(
                tab, "%s · 用时 %d:%02d" % ("完成" if ok else "结束", mm, ss))
            panel = tab.property("thinking_panel")
            if panel is not None:
                panel.finish(ok=ok, elapsed=elapsed)
        self._run_tab = None
        self._run_started_at = None
        return elapsed

    def _thinking_begin(self, tab, first: str = "开始处理…") -> None:
        if tab is None:
            return
        panel = tab.property("thinking_panel")
        if panel is not None:
            panel.begin(first)

    def _thinking_push(self, tab, text: str) -> None:
        if tab is None:
            return
        panel = tab.property("thinking_panel")
        if panel is not None:
            panel.push(text)

    def _infer_target_from_text(self, text: str) -> str:
        """未选目录时的兜底：只认「桌面 / 下载」，其余不猜。"""
        t = str(text or "").lower()
        if "桌面" in t or "desktop" in t:
            candidate = get_desktop_path()
        elif "下载" in t or "downloads" in t:
            candidate = Path.home() / "Downloads"
        else:
            return ""
        try:
            return str(candidate) if Path(candidate).is_dir() else ""
        except OSError:
            return ""

    def _on_abort_thinking(self, tab) -> None:
        """中止当前任务：复用 TaskControl.cancel；写失败路径仍靠 Word 快照自动还原。"""
        if tab is None:
            return
        if self._worker is None or not self._worker.isRunning():
            self._thinking_push(tab, "当前没有进行中的任务")
            return
        if self._task_control is not None:
            self._task_control.request_cancel()
        self.log("进度: ⏹ 用户中止思考")
        self._thinking_push(tab, "用户中止思考…")
        chat = tab.property("chat_display")
        send_btn = tab.property("send_btn")
        resume_btn = tab.property("resume_btn")
        if chat is not None:
            self._append_bubble(chat, "已请求中止当前任务…", "ai")
            channel_id = tab.property("channel_id")
            if channel_id is not None:
                mid = db.add_message(channel_id, "ai", "已请求中止当前任务…")
                acc = getattr(tab, "round_undo", None) or empty_round()
                ids = list(acc.get("ai_msg_ids") or [])
                if mid:
                    ids.append(mid)
                acc["ai_msg_ids"] = ids
                tab.round_undo = acc
        if send_btn and resume_btn:
            self._set_running_ui(send_btn, resume_btn, False, can_resume=True)

    def _on_undo_round(self, tab) -> None:
        """撤回最近一轮：备份盖回 Word / 挪回文件 / 删本轮产物 / 去掉本轮 AI 回应。"""
        if tab is None:
            return
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(
                self, "撤回本轮", "请先中止当前思考，再撤回。")
            return
        rec = getattr(tab, "round_undo", None) or empty_round()
        if not has_undoable(rec):
            QMessageBox.information(
                self, "撤回本轮", "没有可撤回的本轮操作（或已撤回过）。")
            return
        oks, errs = apply_file_undos(rec)
        for m in list(rec.get("moves") or []):
            lid = m.get("log_id")
            if lid:
                try:
                    db.mark_log_undone(int(lid))
                except Exception:  # noqa: BLE001
                    pass
            # 同步右侧日志展示
            self._mark_log_item_undone(lid)

        channel_id = tab.property("channel_id")
        n_msg = 0
        if channel_id is not None:
            n_msg = db.delete_messages_by_ids(
                channel_id, list(rec.get("ai_msg_ids") or []))
        self._reload_chat_from_db(tab)

        rec["undone"] = True
        tab.round_undo = rec
        panel = tab.property("thinking_panel")
        if panel is not None:
            panel.set_undo_enabled(False)
            panel.push("已撤回本轮")
            panel.show()

        summary = []
        if oks:
            summary.append("；".join(oks[:6]))
        if n_msg:
            if oks or rec.get("moves") or rec.get("word_restores"):
                summary.append("已去掉本轮 %d 条 AI 回应" % n_msg)
            else:
                summary.append("本轮没有产生可撤回的文件，已去掉 %d 条 AI 回应" % n_msg)
        if errs:
            summary.append("部分失败：" + "；".join(errs[:4]))
        msg = "撤回完成。" if not summary else ("撤回完成。" + " ".join(summary))
        self.log("撤回本轮: %s" % msg)
        chat = tab.property("chat_display")
        if chat is not None:
            self._append_bubble(chat, msg, "ai")
            if channel_id is not None:
                # 撤回说明本身不再记入可再撤账本
                db.add_message(channel_id, "ai", msg)
        if oks or any(rec.get("moves") or rec.get("word_restores")):
            self.file_explorer.refresh()
        self._bind_export_button(tab, None)

    def _mark_log_item_undone(self, log_id) -> None:
        if log_id is None:
            return
        try:
            lid = int(log_id)
        except (TypeError, ValueError):
            return
        for i in range(self.log_list.count()):
            item = self.log_list.item(i)
            if item is None:
                continue
            if item.data(Qt.ItemDataRole.UserRole + 1) == lid:
                text = item.text() or ""
                if "（已撤回）" not in text:
                    item.setText(text + "（已撤回）")
                item.setData(Qt.ItemDataRole.UserRole, None)
                break

    def _reload_chat_from_db(self, tab) -> None:
        if tab is None:
            return
        chat = tab.property("chat_display")
        channel_id = tab.property("channel_id")
        if chat is None:
            return
        chat.clear()
        if channel_id is None:
            chat.setHtml(WELCOME_HTML)
            return
        rows = db.list_messages(channel_id)
        if not rows:
            chat.setHtml(WELCOME_HTML)
            return
        for sender, content in rows:
            self._append_bubble(chat, content, sender)

    # ------------------------------------------------------------------
    # P2：对话附件（拖入 / 附加按钮 → file_paths；不猜意图）
    # ------------------------------------------------------------------

    def _set_input_drop_active(self, tab, active: bool) -> None:
        """拖入时浅蓝底+虚线；平时不常驻大虚线框。"""
        if tab is None:
            return
        card = tab.property("input_card")
        if card is None:
            return
        want = bool(active)
        if bool(card.property("dropActive")) == want:
            return
        card.setProperty("dropActive", want)
        sty = card.style()
        sty.unpolish(card)
        sty.polish(card)
        card.update()

    def eventFilter(self, obj, event):
        et = event.type()
        if et in (QEvent.Type.DragEnter, QEvent.Type.DragMove):
            md = event.mimeData()
            if md and md.hasUrls():
                if any(u.isLocalFile() for u in md.urls()):
                    tab = self._tab_for_widget(obj)
                    self._set_input_drop_active(tab, True)
                    event.setDropAction(Qt.DropAction.CopyAction)
                    event.acceptProposedAction()
                    return True
        if et == QEvent.Type.DragLeave:
            tab = self._tab_for_widget(obj)
            self._set_input_drop_active(tab, False)
        if et == QEvent.Type.Drop:
            md = event.mimeData()
            tab = self._tab_for_widget(obj)
            self._set_input_drop_active(tab, False)
            if md and md.hasUrls():
                if tab is not None:
                    added = self._add_attachments(tab, paths_from_urls(md.urls()))
                    if added:
                        event.setDropAction(Qt.DropAction.CopyAction)
                        event.acceptProposedAction()
                        return True
                    event.acceptProposedAction()
                    return True
        return super().eventFilter(obj, event)

    def _tab_for_widget(self, w):
        cur = w
        while cur is not None:
            try:
                if self.chat_tabs.indexOf(cur) >= 0:
                    return cur
            except Exception:  # noqa: BLE001
                break
            cur = cur.parentWidget() if hasattr(cur, "parentWidget") else None
        return self.chat_tabs.currentWidget()

    def _add_attachments(self, tab, paths) -> list[str]:
        """追加附件芯片；只收文件。返回新加入的路径。"""
        if tab is None:
            return []
        cur = list(getattr(tab, "attach_paths", []) or [])
        before = set(cur)
        merged = paths_from_strings(cur + list(paths or []))
        tab.attach_paths = merged
        self._rebuild_attach_chips(tab)
        return [p for p in merged if p not in before]

    def _remove_attachment(self, tab, path: str) -> None:
        if tab is None:
            return
        key = str(Path(path).resolve()) if path else ""
        cur = []
        for p in list(getattr(tab, "attach_paths", []) or []):
            try:
                if str(Path(p).resolve()) == key:
                    continue
            except OSError:
                if p == path:
                    continue
            cur.append(p)
        tab.attach_paths = cur
        self._rebuild_attach_chips(tab)

    def _clear_attachments(self, tab) -> None:
        if tab is None:
            return
        tab.attach_paths = []
        self._rebuild_attach_chips(tab)

    def _pick_attachments(self, tab) -> None:
        ws = str(tab.property("workspace_path") or "").strip() if tab else ""
        start = ws if ws and Path(ws).is_dir() else str(get_desktop_path())
        files, _ = QFileDialog.getOpenFileNames(
            self, "附加到对话（精确指定文件）", start)
        if files:
            self._add_attachments(tab, files)

    def _on_explorer_attach(self, paths) -> None:
        """资源管理器右键「附加到当前对话」。"""
        tab = self.chat_tabs.currentWidget()
        if tab is None:
            return
        added = self._add_attachments(tab, paths)
        if added:
            self.log("已附加到对话：%s" % "、".join(Path(p).name for p in added[:5]))

    def _rebuild_attach_chips(self, tab) -> None:
        layout = tab.property("attach_chips_layout") if tab else None
        if layout is None:
            return
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for path in list(getattr(tab, "attach_paths", []) or []):
            name = Path(path).name
            chip = QPushButton(name + " ×")
            chip.setObjectName("attachChip")
            chip.setToolTip(path)
            chip.setFixedHeight(26)
            chip.setStyleSheet(
                "QPushButton#attachChip { background:#475569; color:#f8fafc;"
                " border:1px solid #334155; border-radius:12px;"
                " padding:0 10px; font-size:12px; font-weight:600; }"
                "QPushButton#attachChip:hover { background:#334155; color:#ffffff; }")
            chip.clicked.connect(
                lambda _=False, t=tab, p=path: self._remove_attachment(t, p))
            layout.addWidget(chip)
        layout.addStretch(1)

    def _bind_export_button(self, tab, export_path: str | None) -> None:
        """plan_ready 后显示「导出为 Excel」；点击用系统默认程序打开 xlsx。"""
        if tab is None:
            return
        btn = tab.property("export_btn")
        path = str(export_path or "").strip()
        tab.setProperty("export_path", path)
        self._last_export_path = path or None
        if btn is None:
            return
        if path and Path(path).is_file():
            btn.setToolTip(path)
            btn.show()
        else:
            btn.hide()

    def _open_query_export(self, tab=None) -> None:
        """打开当前频道绑定的跨表查询结果表。"""
        tab = tab or self.chat_tabs.currentWidget()
        path = ""
        if tab is not None:
            path = str(tab.property("export_path") or "")
        if not path:
            path = str(self._last_export_path or "")
        if not path or not Path(path).is_file():
            QMessageBox.information(self, "导出", "还没有可打开的查询结果表。")
            return
        try:
            os.startfile(path)  # noqa: S606 — Windows 用户主动打开本地导出
            self.log("已打开查询结果表: %s" % Path(path).name)
        except OSError as exc:
            # 回退到 Qt 打开
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
                QMessageBox.warning(
                    self, "导出", "无法打开结果表：%s\n%s" % (path, exc)
                )

    def _set_running_ui(self, send_btn, resume_btn, running: bool, can_resume: bool = False):
        """运行中：黄「结束」；暂停/结束后可「重新」。"""
        if running:
            send_btn.setText("结束")
            send_btn.setObjectName("btnEnd")
            send_btn.setEnabled(True)
            send_btn.setStyleSheet(
                "QPushButton { background:#eab308; color:#18181b; border:none;"
                " border-radius:12px; font-weight:600; }")
            resume_btn.hide()
        else:
            send_btn.setText("发送")
            send_btn.setObjectName("btnSend")
            send_btn.setEnabled(True)
            send_btn.setStyleSheet("")
            if can_resume:
                resume_btn.setStyleSheet(
                    "QPushButton { background:#16a34a; color:#ffffff; border:none;"
                    " border-radius:12px; font-weight:600; }")
                resume_btn.show()
            else:
                resume_btn.hide()
        # 刷新 objectName 样式
        send_btn.style().unpolish(send_btn)
        send_btn.style().polish(send_btn)
        resume_btn.style().unpolish(resume_btn)
        resume_btn.style().polish(resume_btn)

    def _on_primary_btn(self, chat_display, input_box, send_btn, resume_btn):
        """发送 或 结束（取消当前任务）。"""
        if self._worker is not None and self._worker.isRunning():
            if self._task_control is not None:
                self._task_control.request_cancel()
                self.log("进度: ⏹ 用户结束当前任务")
                self._append_bubble(chat_display, "已请求结束当前任务…", "ai")
                self._set_running_ui(send_btn, resume_btn, False, can_resume=True)
            return
        self.on_send(chat_display, input_box, send_btn, resume_btn, resume=False)

    def _on_resume(self, chat_display, input_box, send_btn, resume_btn):
        """绿色「重新」：有检查点则续跑，否则用上一条指令重开。"""
        if self._worker is not None and self._worker.isRunning():
            if self._task_control is not None:
                self._task_control.request_resume()
                self._set_running_ui(send_btn, resume_btn, True)
                self._append_bubble(chat_display, "继续执行…", "ai")
            return
        text = self._last_task_text or input_box.text().strip()
        if not text:
            self._append_bubble(chat_display, "没有可继续的任务，请重新输入指令。", "ai")
            return
        input_box.setText(text)
        self.on_send(chat_display, input_box, send_btn, resume_btn, resume=True)

    def on_send(self, chat_display, input_box, send_btn=None, resume_btn=None,
                resume: bool = False, confirmed: bool = False):
        """处理发送：交 AI 代理；dispatch 在后台线程跑，避免前端卡死。"""
        text = input_box.text().strip()
        if not text and (resume or confirmed) and self._last_task_text:
            text = self._last_task_text
        if not text:
            return
        if self._worker is not None and self._worker.isRunning():
            self._append_bubble(chat_display, "上一条指令还在执行中，可点「结束」停止。", "ai")
            return

        tab = self._tab_for_widget(chat_display)
        if send_btn is None and tab is not None:
            send_btn = tab.property("send_btn")
        if resume_btn is None and tab is not None:
            resume_btn = tab.property("resume_btn")

        channel_id = tab.property("channel_id") if tab is not None else None

        def reply_ai(content):
            self._append_bubble(chat_display, content, "ai")
            if channel_id is not None:
                mid = db.add_message(channel_id, "ai", content)
                if tab is not None and mid:
                    acc = getattr(tab, "round_undo", None) or empty_round()
                    ids = list(acc.get("ai_msg_ids") or [])
                    ids.append(mid)
                    acc["ai_msg_ids"] = ids
                    tab.round_undo = acc

        ws = str(tab.property("workspace_path") or "").strip() if tab is not None else ""
        if not ws or not Path(ws).is_dir():
            reply_ai("本频道还没有工作区。请先点「更换工作区」选一个文件夹。")
            if tab is not None:
                self._ensure_tab_workspace(tab, required=False)
            input_box.setFocus()
            return

        # P3：文内文件名（仅工作区；多命中不猜——在清空输入前拦截）
        named_hits: list[str] = []
        named_missing: list[str] = []
        if not resume and not self._pending_table_query:
            resolved = resolve_names_in_workspace(ws, text)
            if resolved.ambiguous:
                reply_ai(format_ambiguous_message(resolved))
                input_box.setFocus()
                return
            named_hits = list(resolved.hits or [])
            named_missing = list(resolved.missing or [])

        attach_now = list(getattr(tab, "attach_paths", []) or []) if tab is not None else []
        # 附件 ∪ 文内唯一命中（附件优先顺序在 merge 里）
        path_note_src = merge_file_paths(attach_now, named_hits)
        attach_note = format_attach_note(path_note_src)
        if not resume:
            shown = text if not attach_note else (text + "\n" + attach_note)
            self._append_bubble(chat_display, shown, "user")
            input_box.clear()
            extra = []
            if attach_now:
                extra.append("附件%d" % len(attach_now))
            if named_hits:
                extra.append("文内%d" % len(named_hits))
            self.log(f"发送: {text}" + ((" +" + "+".join(extra)) if extra else ""))
        else:
            self._append_bubble(chat_display, f"（重新/续跑）{text}", "user")
            self.log(f"续跑: {text}")

        if channel_id is not None:
            persist = text if not resume else f"[续跑] {text}"
            if attach_note and not resume:
                persist = persist + "\n" + attach_note
            db.add_message(channel_id, "user", persist)

        # 浏览进了子目录 → 用当前目录；否则用频道工作区根
        target = self.file_explorer.current_path or ws
        if not target or not Path(str(target)).is_dir():
            # 没选目录时，按指令里的「桌面 / 下载」兜底（只认这两处，不猜别的）
            inferred = self._infer_target_from_text(text)
            if inferred:
                target = inferred
                if tab is not None:
                    self._thinking_push(tab, "未选择目录，按指令使用：%s" % inferred)
        if resume and self._last_task_target is not None:
            target = self._last_task_target

        from agent_core.session_state import TaskControl
        control = TaskControl()
        control.reset()
        self._task_control = control
        self._last_task_target = target
        self._last_channel_id = channel_id
        self._active_tab = tab

        input_box.setEnabled(False)
        if send_btn and resume_btn:
            self._set_running_ui(send_btn, resume_btn, True)
        # 附件 → 文内唯一命中 → 资源管理器选中
        selected_paths = []
        try:
            selected_paths = list(self.file_explorer.get_selected_files() or [])
        except Exception:
            selected_paths = []
        selected_paths = merge_file_paths(
            merge_file_paths(attach_now, named_hits), selected_paths)
        ask_history = None
        pending = self._pending_table_query
        user_reply_text = text
        if pending and not resume:
            from pipeline.block_cross_table.query_engine.run_query_tool import (
                build_ask_history_continuation,
            )
            ask_history = build_ask_history_continuation(
                prior_five_part=pending.get("five_part"),
                prior_fail_locus=pending.get("fail_locus"),
                user_reply=user_reply_text,
                prior_ask_history=pending.get("ask_history"),
            )
            selected_paths = list(pending.get("file_paths") or selected_paths)
            # 续问时仍用原 utterance 作为查询主体（不重新意图分发）
            text = str(pending.get("utterance") or text)
            self._last_task_text = text
            self._thinking_begin(tab, "已收到确认，继续跨表查询…")
            input_box.setPlaceholderText("跨表查询追问中，直接回复确认即可…")
        else:
            self._last_task_text = text
            soft = ""
            if named_missing:
                soft = (
                    "（工作区未找到：%s —— 未锁定；可用「附加」）"
                    % "、".join(named_missing[:5])
                )
            self._thinking_begin(tab, "解析意图…")
            if soft:
                self._thinking_push(tab, soft)
            input_box.setPlaceholderText("输入消息，回车发送...")
        # 附件先保留：失败时用户可直接重发；成功/正常结束后由 on_done 清空
        self._sent_attachments = (tab, list(attach_now)) if attach_now and tab is not None else None
        self._last_progress_msg = ""
        self._start_run_timer(tab, "正在解析意图…")
        # 新一轮开始：清空本轮撤回账本（上一轮已结束才可撤）
        if tab is not None:
            tab.round_undo = empty_round()
            panel = tab.property("thinking_panel")
            if panel is not None:
                panel.set_undo_enabled(False)

        worker = _DispatchWorker(
            self.dispatcher, text, target, control=control,
            channel_id=channel_id, resume=resume,
            file_paths=selected_paths,
            ask_history=ask_history, confirmed=confirmed)
        self._worker = worker

        def on_progress(msg):
            text_msg = str(msg or "").strip()
            if not text_msg:
                return
            self.log(f"进度: {text_msg}")
            if tab is not None:
                tab.setProperty("run_base_status", text_msg)
            self._refresh_run_status()
            # 进度进思考过程面板，不再刷对话气泡
            if text_msg != self._last_progress_msg:
                self._last_progress_msg = text_msg
                self._thinking_push(tab, text_msg)

        def on_done(result):
            input_box.setEnabled(True)
            if not isinstance(result, dict):
                result = {
                    "handler": "系统",
                    "message": "内部错误：任务没有返回结果字典。",
                    "operations": [],
                }
            cancelled = bool(result.get("cancelled"))
            tool = str(result.get("tool") or "")
            status = str(result.get("status") or "")

            # 本轮附件：失败/取消/待确认时保留，方便原样重发；其余情况清空
            failed = cancelled or status in (
                "failed", "error", "rollback_failed",
                "compensation_failed", "failed_postcheck",
            ) or str(result.get("reason") or "") == "model_unavailable"
            sent = getattr(self, "_sent_attachments", None)
            if (sent and sent[0] is tab and not failed
                    and status != "needs_confirmation"):
                self._clear_attachments(tab)
                self._sent_attachments = None

            # 先贴对话气泡，再停表/收尾——避免后段抛错时只见「完成」不见回复
            try:
                if status == "needs_user" and tool == "table_query":
                    self._pending_table_query = {
                        "utterance": self._last_task_text,
                        "five_part": result.get("five_part"),
                        "fail_locus": result.get("fail_locus"),
                        "ask_history": result.get("ask_history") or [],
                        "file_paths": result.get("file_paths") or selected_paths,
                    }
                    input_box.setPlaceholderText(
                        "跨表查询需要确认：在此直接回复（不会重新分发意图）…"
                    )
                elif tool == "table_query":
                    self._pending_table_query = None
                    input_box.setPlaceholderText(INPUT_PLACEHOLDER)
                else:
                    if not self._pending_table_query:
                        input_box.setPlaceholderText(INPUT_PLACEHOLDER)

                if result.get("handler") == "AI" and not config.get_api_key():
                    reply_ai("这条需要聊天模型。A 级整理不需要 Key（如「按类型整理」）。"
                             "查表 / 改 Word 请到「设置 → 聊天模型设置」。")
                elif tool == "table_query" and status == "plan_ready":
                    preview = result.get("result_preview") or {}
                    total = int(preview.get("total_rows") or 0)
                    export = result.get("export") if isinstance(result.get("export"), dict) else {}
                    export_path = str((export or {}).get("path") or "")
                    reply_ai(
                        "查询完成，共 %s 行。点击下方「导出为 Excel」打开结果表。\n\n%s"
                        % (total, result.get("message") or "")
                    )
                    self._bind_export_button(tab, export_path)
                    if export_path:
                        self.log("跨表查询结果已导出: %s" % export_path)
                elif tool in ("word_office", "text_office") and status == "needs_confirmation":
                    from PyQt6.QtWidgets import QMessageBox as _QMB
                    reply = _QMB.question(
                        self, "确认执行",
                        str(result.get("message") or "这是危险操作，确认执行？"),
                        _QMB.StandardButton.Yes | _QMB.StandardButton.No,
                    )
                    if reply == _QMB.StandardButton.Yes:
                        self.on_send(chat_display, input_box, send_btn, resume_btn,
                                     confirmed=True)
                    else:
                        reply_ai("已取消。")
                elif tool in ("word_office", "text_office") and status in (
                        "rollback_failed", "compensation_failed", "failed_postcheck"):
                    prefix = {
                        "rollback_failed": "⚠️ 回滚失败",
                        "compensation_failed": "⚠️ 链式补偿失败",
                        "failed_postcheck": "⚠️ 后置校验失败（已尝试还原）",
                    }.get(status, "⚠️ 文件写入失败")
                    reply_ai("%s：%s" % (prefix, result.get("message") or status))
                    self.log("%s %s: %s" % (tool, status, (result.get("message") or "")[:200]))
                elif tool == "table_query" and status in ("failed", "error", "cancelled"):
                    err = str(result.get("error") or result.get("message") or status)
                    reply_ai(
                        "⚠️ 跨表查询失败（%s）：%s"
                        % (result.get("fail_locus") or status, err)
                    )
                    self._bind_export_button(tab, None)
                    self.log("跨表查询失败: %s" % err[:200])
                else:
                    body = str(result.get("message") or "").strip() or "（无详细说明）"
                    reply_ai("[%s] %s" % (result.get("handler") or "AI代理", body))
                    if tool == "table_query":
                        self._bind_export_button(tab, None)
                    # 成稿族：跳到输出目录，避免「桌面已写、资源管理器仍停在别处」
                    if tool in (
                        "content_split", "structure_split", "template_fill",
                    ):
                        jump = str(
                            result.get("output_dir")
                            or result.get("work_dir")
                            or ""
                        ).strip()
                        if jump and Path(jump).is_dir():
                            try:
                                self.file_explorer.load_directory(jump)
                            except Exception:  # noqa: BLE001
                                self.file_explorer.refresh()
                        else:
                            self.file_explorer.refresh()
            except Exception as exc:  # noqa: BLE001
                try:
                    reply_ai("⚠️ 结果展示失败：%s" % exc)
                except Exception:  # noqa: BLE001
                    pass
                self.log("on_done 展示异常: %s" % exc)

            elapsed = self._stop_run_timer(tab, ok=not cancelled)
            if elapsed > 0:
                self.log("本轮用时 %d 秒" % elapsed)
            if tab is not None:
                self._thinking_push(
                    tab,
                    "已返回结果（%s）" % (result.get("reason") or tool or "done"),
                )

            conflicts = result.get("instruction_conflicts") or []
            if conflicts and not cancelled:
                lines = ["检查完表格后发现互相打架的要求或来源。是否保留这张表？"]
                for c in conflicts:
                    if c.get("kind") == "data":
                        lines.append(
                            f"列「{c.get('column')}」同一对象有两个来源："
                            + "、".join(c.get("values") or [])
                            + f"，先写入的是「{c.get('applied')}」。"
                        )
                    elif c.get("kind") == "keep_drop":
                        lines.append("同一对象既要保留又要去掉：" + "、".join(c.get("values") or []))
                    elif c.get("resolved"):
                        lines.append(
                            f"列「{c.get('column')}」有多种取值，带先后词的是「{c.get('applied')}」。"
                        )
                    else:
                        lines.append(
                            f"列「{c.get('column')}」有多种取值，没有先后词，没有改写成其中某一句。"
                        )
                answer = QMessageBox.question(
                    self, "表格有冲突",
                    "\n".join(lines),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    removed = 0
                    for p in result.get("report_paths") or []:
                        try:
                            Path(p).unlink()
                            removed += 1
                        except OSError:
                            pass
                    reply_ai(f"已按你的选择撤销导出（{removed} 个文件）。")
                    self.file_explorer.refresh()
            log_ids = []
            for op in result.get("operations", []):
                if isinstance(op, dict) and op.get("src") and op.get("dst"):
                    lid = self.log(
                        f"移动 {Path(op['src']).name} → {Path(op['dst']).parent.name}/",
                        undo_info=op)
                    log_ids.append(lid)
                else:
                    log_ids.append(None)
            if result.get("operations"):
                self.file_explorer.refresh()
            # 记下本轮可撤回项（Word 备份 / 移动 / 产物 + 已记下的 AI 气泡）
            if tab is not None:
                collected = collect_from_result(result, log_ids=log_ids)
                base = getattr(tab, "round_undo", None) or empty_round()
                merged = merge_rounds(base, collected)
                # export 可能只在 UI 绑定路径里
                ep = str(tab.property("export_path") or "").strip()
                if ep and ep not in merged["extra_files"]:
                    merged["extra_files"].append(ep)
                tab.round_undo = merged
                panel = tab.property("thinking_panel")
                if panel is not None:
                    panel.set_undo_enabled(has_undoable(merged))
                    panel.show()
            if send_btn and resume_btn:
                self._set_running_ui(send_btn, resume_btn, False, can_resume=cancelled)
            input_box.setFocus()
            self._worker = None
            if self._task_control and self._task_control.cancelled():
                pass
            else:
                self._task_control = None

        def on_fail(err):
            input_box.setEnabled(True)
            elapsed = self._stop_run_timer(tab, ok=False)
            # Worker 异常：清追问态、恢复占位、隐藏导出钮
            self._pending_table_query = None
            input_box.setPlaceholderText("输入消息，回车发送...")
            self._bind_export_button(tab, None)
            reply_ai(f"⚠️ 执行异常：{err}")
            self.log("Worker 异常: %s（用时 %d 秒）" % (err, elapsed))
            if tab is not None:
                panel = tab.property("thinking_panel")
                if panel is not None:
                    panel.set_undo_enabled(has_undoable(getattr(tab, "round_undo", None)))
                    panel.show()
            if send_btn and resume_btn:
                self._set_running_ui(send_btn, resume_btn, False, can_resume=True)
            input_box.setFocus()
            self._worker = None

        worker.progress.connect(on_progress)
        worker.finished_result.connect(on_done)
        worker.failed.connect(on_fail)
        worker.start()

    @staticmethod
    def _paint_log_item(item: QListWidgetItem, message: str, *, undone: bool = False) -> None:
        """成功绿 / 失败红 / 撤回灰；普通默认色。"""
        msg = str(message or "")
        if undone or "已撤回" in msg:
            item.setForeground(QBrush(QColor("#9aa0a6")))
        elif any(k in msg for k in ("失败", "错误", "无法", "拒绝", "取消")):
            item.setForeground(QBrush(QColor("#b42318")))
        elif any(k in msg for k in ("完成", "成功", "已分类", "已移动", "已写入", "整理完成")):
            item.setForeground(QBrush(QColor("#067647")))
        else:
            item.setForeground(QBrush(QColor("#4b5563")))

    def log(self, message, undo_info=None):
        """添加一条操作日志（同时写入 SQLite，重启后仍在）

        参数:
            message: 日志文本
            undo_info: 可选，dict，描述可撤销的操作。
                       目前支持 {"type": "move", "src": 原路径, "dst": 现路径}
        """
        timestamp = datetime.now().strftime("%H:%M:%S")
        item = QListWidgetItem(f"[{timestamp}] {message}")
        if undo_info:
            item.setData(Qt.ItemDataRole.UserRole, undo_info)
        log_id = db.add_log(message, undo_info)
        item.setData(Qt.ItemDataRole.UserRole + 1, log_id)
        self._paint_log_item(item, message, undone=False)
        self.log_list.addItem(item)
        self.log_list.scrollToBottom()
        return log_id

    def _load_logs(self):
        """启动时从数据库恢复历史操作日志（含已撤回的，标注状态）"""
        import json as _json
        rows = db.list_logs()
        if not rows:
            ready = QListWidgetItem("就绪（右键日志可撤回支持的操作）")
            self._paint_log_item(ready, "就绪", undone=True)
            self.log_list.addItem(ready)
            return
        for log_id, message, undo_info_json, undone, created in rows:
            text = f"[{created[11:]}] {message}"
            if undone:
                text += "（已撤回）"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole + 1, log_id)
            if undo_info_json and not undone:
                item.setData(Qt.ItemDataRole.UserRole, _json.loads(undo_info_json))
            self._paint_log_item(item, text, undone=bool(undone))
            self.log_list.addItem(item)
        self.log_list.scrollToBottom()

    def _export_logs_docx(self) -> None:
        """导出操作日志为 Word（复用 word_renderer）。"""
        from export.word_renderer import render_report

        lines = []
        for i in range(self.log_list.count()):
            it = self.log_list.item(i)
            if it is None:
                continue
            lines.append(("p", it.text()))
        if not lines:
            QMessageBox.information(self, "导出日志", "当前没有可导出的日志。")
            return
        tab = self.chat_tabs.currentWidget()
        ws = str(tab.property("workspace_path") or "").strip() if tab else ""
        out_dir = Path(ws) if ws and Path(ws).is_dir() else get_desktop_path()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = out_dir / f"操作日志_{stamp}.docx"
        try:
            path = render_report(out, "操作日志", lines)
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", str(exc))
            return
        self.log(f"已导出操作日志 → {path}")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    # ------------------------------------------------------------------
    # 操作范围提示
    # ------------------------------------------------------------------

    def _on_log_toggled(self, expanded: bool) -> None:
        """折叠操作日志，把垂直空间让给资源管理器。"""
        self.log_list.setVisible(expanded)
        self._log_toggle_btn.setText(
            "操作日志 ▾" if expanded else "操作日志 ▸（点击展开 · 撤回在这里）")
        if expanded:
            sizes = getattr(self, "_log_expanded_sizes", None) or [560, 140]
            self.right_v_splitter.setSizes(sizes)
        else:
            cur = self.right_v_splitter.sizes()
            if len(cur) >= 2 and cur[1] > 48:
                self._log_expanded_sizes = list(cur)
            total = sum(self.right_v_splitter.sizes()) or 700
            self.right_v_splitter.setSizes([max(total - 36, 100), 36])

    def _on_path_changed(self, path):
        """文件管理器切换目录时，更新操作范围提示（工作区仍以频道绑定为准）。"""
        tab = self.chat_tabs.currentWidget()
        ws = ""
        if tab is not None:
            ws = str(tab.property("workspace_path") or "").strip()
        if path == "此电脑":
            self.scope_label.setText(
                "📂 当前位置：此电脑。"
                + ("本频道工作区：%s（可点资源管理器回到该目录）" % ws if ws else "请先为本频道选择工作区。")
            )
        else:
            tip = f"📂 浏览位置：{path}"
            if ws:
                tip += f"\n工作区根：{ws}（整理默认当前浏览目录；换范围请「更换工作区」或进入子目录）"
            else:
                tip += "\n请先为本频道选择工作区。"
            self.scope_label.setText(tip)

    # ------------------------------------------------------------------
    # 帮助说明
    # ------------------------------------------------------------------

    def _show_help(self):
        """弹出完整的使用说明对话框"""
        help_text = HELP_HTML
        # 用带滚动条的对话框替代 QMessageBox，内容再多也能上下滑动查看
        from PyQt6.QtWidgets import QDialog, QTextBrowser

        dialog = QDialog(self)
        dialog.setWindowTitle("FileMate 使用说明")
        dialog.resize(620, 560)  # 固定合理尺寸，超出部分滚动查看

        layout = QVBoxLayout(dialog)
        browser = QTextBrowser()  # QTextBrowser 自带滚动条，支持富文本
        browser.setHtml(help_text)
        browser.setOpenExternalLinks(False)
        layout.addWidget(browser)

        dialog.exec()

    # ------------------------------------------------------------------
    # 设置
    # ------------------------------------------------------------------

    def _edit_local_web(self):
        """设置 → 本地 Web：记忆纠错 + 跨表查询，主窗口一键启停/打开。"""
        from ui.local_web_dialog import open_local_web_dialog
        open_local_web_dialog(self)

    def _edit_memory_correction_web(self):
        """旧入口：并入「设置 → 本地 Web」，避免两套启停。"""
        self._edit_local_web()

    def _open_web_settings_hint(self):
        """M5.2: model / MaiBot / self-learning live in Web Settings tab."""
        from web.memory_correction.port_settings import resolve_port

        port = int(resolve_port())
        url = "http://127.0.0.1:%s/" % port
        QMessageBox.information(
            self,
            "已迁至 Web 设置",
            "聊天模型请用「设置 → 聊天模型设置」。\n"
            "记忆纠错 / 跨表查询请用「设置 → 本地 Web」启动后再打开：\n%s" % url,
        )

    def closeEvent(self, event):
        """主程序关闭：若记忆纠错 Web 子进程仍在跑，询问是否停止。"""
        try:
            from web.memory_correction.web_service_manager import (
                get_query_ui_manager,
                get_shared_manager,
            )

            running = []
            for name, mgr in (
                ("记忆纠错", get_shared_manager()),
                ("跨表查询", get_query_ui_manager()),
            ):
                if mgr.is_running:
                    running.append((name, mgr))
        except Exception:
            super().closeEvent(event)
            return

        if running:
            pids = "、".join("%s PID=%s" % (n, m.pid) for n, m in running)
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle("Web 服务还在运行")
            box.setText("本地 Web 还在运行，是否停止")
            box.setInformativeText(pids)
            box.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            box.setDefaultButton(QMessageBox.StandardButton.Yes)
            ans = box.exec()
            if ans == QMessageBox.StandardButton.Yes:
                for _n, mgr in running:
                    mgr.stop()
            else:
                QMessageBox.information(self, "保留进程", pids)
        super().closeEvent(event)

    def _edit_chat_model_settings(self):
        """PyQt6 聊天模型设置：Base URL / Model / API Key（写 config.json）。"""
        dialog = QDialog(self)
        dialog.setWindowTitle("聊天模型设置")
        form = QFormLayout(dialog)
        base_edit = QLineEdit(str(config.get_param("api.base_url", "") or ""))
        base_edit.setPlaceholderText(
            "https://api.deepseek.com/v1 或 https://open.bigmodel.cn/api/paas/v4")
        model_edit = QLineEdit(str(config.get_param("api.model", "") or ""))
        model_edit.setPlaceholderText("deepseek-chat / glm-4-flash")
        key_edit = QLineEdit(str(config.get_api_key() or ""))
        key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        key_edit.setPlaceholderText("留空则清除")
        form.addRow("Base URL", base_edit)
        form.addRow("Model", model_edit)
        form.addRow("API Key", key_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            config.set_param("api.base_url", base_edit.text().strip())
            config.set_param("api.model", model_edit.text().strip())
            config.set_api_key(key_edit.text().strip())
            if hasattr(self, "ai_assistant"):
                self.ai_assistant.api_key = key_edit.text().strip()
            self.log("聊天模型设置已保存")

    def _edit_memory_settings(self):
        """PyQt6 记忆/向量设置：向量来源、外部 embedding、MaiBot 目录、外发策略、对话复盘。"""
        from pipeline.block_cross_table.memory_rag.dense_provider import (
            resolve_dense_provider,
        )
        from pipeline.block_cross_table.memory_rag.external_embedding_config import (
            resolve_external_embedding,
        )
        from pipeline.block_cross_table.memory_rag.merge_settings import (
            resolve_maibot_data_dir,
            save_maibot_data_dir,
            save_merge_settings_patch,
        )

        prov = resolve_dense_provider(persist_migration=False).dense_provider
        ext = resolve_external_embedding()

        dialog = QDialog(self)
        dialog.setWindowTitle("记忆 / 向量设置")
        form = QFormLayout(dialog)

        provider_box = QComboBox()
        for label, val in (("关闭", "off"), ("本地模型", "local"), ("外部 API", "external")):
            provider_box.addItem(label, val)
        idx = provider_box.findData(prov)
        if idx >= 0:
            provider_box.setCurrentIndex(idx)
        form.addRow("向量来源", provider_box)
        from ai import embed as _embed
        recall_hint = QLabel(
            "Word 召回：FTS5 + LIKE + 向量"
            if _embed.is_available()
            else "Word 召回：仅 FTS5 + LIKE（未配置 api.embedding_key，无语义向量）"
        )
        recall_hint.setWordWrap(True)
        form.addRow(recall_hint)

        local_edit = QLineEdit(
            str(config.get_param("memory_rag.embedded_model_path", "") or ""))
        local_edit.setPlaceholderText("本地 embedding 模型目录（向量来源=本地时用）")
        form.addRow("本地模型路径", local_edit)

        base_edit = QLineEdit(str(ext.base_url or ""))
        base_edit.setPlaceholderText("如 https://api.siliconflow.cn/v1")
        form.addRow("外部 Base URL", base_edit)
        model_edit = QLineEdit(str(ext.model or ""))
        model_edit.setPlaceholderText("如 BAAI/bge-m3")
        form.addRow("外部 Model", model_edit)
        key_edit = QLineEdit("")
        key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        key_edit.setPlaceholderText("留空保留；填新 Key 覆盖")
        form.addRow("外部 API Key", key_edit)

        maibot_edit = QLineEdit(str(resolve_maibot_data_dir() or ""))
        maibot_edit.setPlaceholderText("MaiBot 的 data 目录（只读对接）")
        form.addRow("MaiBot 数据目录", maibot_edit)

        egress_box = QComboBox()
        for label, val in (("允许外部模型", "allow_external"),
                           ("本地模式（禁止外传）", "local_only")):
            egress_box.addItem(label, val)
        eg = str(config.get_param("memory_rag.egress_policy", "allow_external") or "")
        ei = egress_box.findData(eg)
        if ei >= 0:
            egress_box.setCurrentIndex(ei)
        form.addRow("数据外带策略", egress_box)

        dlg_check = QCheckBox("对话复盘学习")
        dlg_check.setChecked(
            bool(config.get_param("memory_rag.dialogue_review_enabled", True)))
        form.addRow(dlg_check)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            patch = {
                "dense_provider": provider_box.currentData(),
                "embedded_model_path": local_edit.text().strip(),
                "egress_policy": egress_box.currentData(),
                "dialogue_review_enabled": dlg_check.isChecked(),
                "external_embedding": {
                    "base_url": base_edit.text().strip(),
                    "model": model_edit.text().strip(),
                    "weight": 1.0 if provider_box.currentData() == "external" else 0.0,
                },
            }
            key = key_edit.text().strip()
            if key:
                patch["external_embedding"]["api_key"] = key
            save_merge_settings_patch(patch)
            save_maibot_data_dir(maibot_edit.text().strip())
            self.log("记忆/向量设置已保存")

    def _edit_table_cleaning_settings(self):
        """表格清洗设置：清洗开关 + 洗成空时模型恢复（默认先问后洗）。"""
        dialog = QDialog(self)
        dialog.setWindowTitle("表格清洗设置")
        form = QFormLayout(dialog)

        clean_check = QCheckBox("启用二次清洗（只对认不出的表；默认先问后洗）")
        clean_current = bool(config.get_param("table_normalizer.enabled", False))
        clean_check.setChecked(clean_current)

        recover_check = QCheckBox("洗成空时让模型尝试恢复表头（可选；会调 LLM）")
        recover_current = bool(config.get_param("table_recover.on_wash_rejected", False))
        recover_check.setChecked(recover_current)
        recover_check.setEnabled(clean_current)

        def _sync_deps(_checked=None):
            recover_check.setEnabled(clean_check.isChecked())

        _sync_deps()
        clean_check.toggled.connect(_sync_deps)

        hint = QLabel(
            "默认「先问后洗」：正常表不动；空表不动；认不出但没数据的表也不动。\n"
            "只有「认不出 + 有数据」的表，才会先问你是否二次洗；确认后才洗，且洗前备份原表。\n"
            "洗后非空 → 用洗后的；洗成空 → 保留原表 + 报警 + 再问。\n"
            "勾「模型恢复」= 洗成空时，多调一次模型尝试恢复表头（成本 + 延迟）。")
        hint.setWordWrap(True)
        form.addRow(clean_check)
        form.addRow(recover_check)
        form.addRow(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        want_clean = clean_check.isChecked()
        want_recover = recover_check.isChecked() and want_clean
        if want_recover and not recover_current:
            reply = QMessageBox.warning(
                self, "开启代价",
                "开启「模型恢复」后：\n"
                "· 二次洗成空时，会多调一次模型恢复表头（成本 + 延迟）。\n"
                "· 恢复不了时仍保留原表并报警，不会毁数据。\n"
                "· 默认关闭，可随时关掉。\n\n"
                "确认开启？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
        config.set_param("table_normalizer.enabled", want_clean)
        config.set_param("table_recover.on_wash_rejected", want_recover)
        self.log("表格清洗设置已保存：清洗=%s，模型恢复=%s"
                 % ("开" if want_clean else "关", "开" if want_recover else "关"))

    def _cleanup_content_cache(self):
        """设置菜单：清理内容分类 storage 孤儿索引（预览→确认，与聊天同强度）。"""
        from pipeline.content_split import cleanup_orphan_indexes

        preview = cleanup_orphan_indexes(dry_run=True) or {}
        n = len(preview.get("removed") or [])
        kept = len(preview.get("kept") or [])
        deferred = len(preview.get("deferred") or [])
        grace = int(preview.get("missing_root_grace_days") or 7)
        defer_tip = (
            f"\n另有 {deferred} 个暂不删（目录暂不可见未满 {grace} 天，或盘符断开）。"
            if deferred
            else ""
        )
        if n <= 0:
            QMessageBox.information(
                self,
                "清理分类缓存",
                f"没有可清理的孤儿索引（仍保留 {kept} 个有效）。"
                f"{defer_tip}\n位置：{preview.get('state_root') or ''}",
            )
            return
        reply = QMessageBox.question(
            self,
            "清理分类缓存",
            f"将删除 {n} 个孤儿索引（id 已换，或 root 缺失且超过 {grace} 天），"
            f"仍保留 {kept} 个有效。"
            f"{defer_tip}\n不碰你的文档。确认清理？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        res = cleanup_orphan_indexes(dry_run=False) or {}
        done = len(res.get("removed") or [])
        self.log(
            f"分类缓存清理：删除 {done} 个孤儿，保留 {len(res.get('kept') or [])}，"
            f"暂缓 {len(res.get('deferred') or [])}"
        )
        QMessageBox.information(
            self,
            "清理分类缓存",
            f"已清理 {done} 个孤儿索引。",
        )

    def _edit_api_key(self):
        """兼容旧入口：转到聊天模型设置。"""
        self._edit_chat_model_settings()

    def _test_api(self):
        """菜单：测试当前配置的聊天模型连通性"""
        from ai.client import test_connectivity
        result = test_connectivity()
        box = QMessageBox.information if result["ok"] else QMessageBox.warning
        box(self, "模型连通性", result["message"])

    def _edit_budget_params(self):
        """预算开销、单请求重试、疑问回复提示阈值（一律写 params，不写死）。"""
        dialog = QDialog(self)
        dialog.setWindowTitle("预算与重试参数")
        form = QFormLayout(dialog)

        fill = QDoubleSpinBox()
        fill.setRange(0.05, 1.0)
        fill.setSingleStep(0.05)
        fill.setDecimals(2)
        fill.setValue(float(config.get_param("llm.context_fill_ratio")))
        form.addRow("fill_ratio（窗口占用比例）", fill)

        overhead = QSpinBox()
        overhead.setRange(0, 1_000_000)
        overhead.setValue(int(config.get_param("llm.prompt_overhead_tokens")))
        form.addRow("prompt_overhead_tokens（模板预留）", overhead)

        reserved = QSpinBox()
        reserved.setRange(1, 1_000_000)
        reserved.setValue(int(config.get_param("llm.reserved_output_tokens")))
        form.addRow("reserved_output_tokens（输出预留）", reserved)

        retries = QSpinBox()
        retries.setRange(1, 50)
        retries.setValue(int(config.get_param("api.max_retries_per_request")))
        form.addRow("api.max_retries_per_request（速率/网络重试上限）", retries)

        threshold = QSpinBox()
        threshold.setRange(1, 100)
        threshold.setValue(int(config.get_param("question.repeat_prompt_threshold")))
        form.addRow("question.repeat_prompt_threshold（同疑问提示次数）", threshold)

        reanalyze_max = QSpinBox()
        reanalyze_max.setRange(0, 20)
        reanalyze_max.setValue(int(config.get_param("review.reanalyze_max")))
        form.addRow("review.reanalyze_max（打回后重分析上限）", reanalyze_max)

        invalid_n = QSpinBox()
        invalid_n.setRange(1, 50)
        invalid_n.setValue(int(config.get_param("review.invalid_threshold")))
        form.addRow("review.invalid_threshold（连续无效复核次数）", invalid_n)

        from PyQt6.QtWidgets import QLineEdit

        review_model = QLineEdit()
        review_model.setPlaceholderText("默认 GLM-4-Flash-250414；清空则沿用 api.model")
        review_model.setText(str(config.get_param("review.model") or ""))
        form.addRow("review.model（复核专用模型）", review_model)

        review_ctx = QSpinBox()
        review_ctx.setRange(0, 2_000_000)
        review_ctx.setSpecialValueText("沿用模型窗口表 / llm.context_tokens")
        rc = config.get_param("review.context_tokens")
        review_ctx.setValue(int(rc) if rc not in (None, "") else 0)
        form.addRow("review.context_tokens（0=沿用）", review_ctx)

        small_ratio = QDoubleSpinBox()
        small_ratio.setRange(0.05, 1.0)
        small_ratio.setSingleStep(0.05)
        small_ratio.setDecimals(2)
        small_ratio.setValue(float(config.get_param("review.small_table_ratio") or 0.5))
        form.addRow("review.small_table_ratio（小表整包阈值）", small_ratio)

        max_packs = QSpinBox()
        max_packs.setRange(1, 500)
        max_packs.setValue(int(config.get_param("review.max_packages") or 30))
        form.addRow("review.max_packages（包数硬上限）", max_packs)

        pack_delay = QDoubleSpinBox()
        pack_delay.setRange(0.0, 60.0)
        pack_delay.setSingleStep(0.5)
        pack_delay.setDecimals(1)
        pack_delay.setValue(float(config.get_param("review.inter_package_delay_sec") or 1))
        form.addRow("review.inter_package_delay_sec（包间间隔）", pack_delay)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        config.set_param("llm.context_fill_ratio", float(fill.value()))
        config.set_param("llm.prompt_overhead_tokens", int(overhead.value()))
        config.set_param("llm.reserved_output_tokens", int(reserved.value()))
        config.set_param("api.max_retries_per_request", int(retries.value()))
        config.set_param("question.repeat_prompt_threshold", int(threshold.value()))
        config.set_param("review.reanalyze_max", int(reanalyze_max.value()))
        config.set_param("review.invalid_threshold", int(invalid_n.value()))
        config.set_param("review.model", review_model.text().strip())
        config.set_param(
            "review.context_tokens",
            int(review_ctx.value()) if int(review_ctx.value()) > 0 else None,
        )
        config.set_param("review.small_table_ratio", float(small_ratio.value()))
        config.set_param("review.max_packages", int(max_packs.value()))
        config.set_param("review.inter_package_delay_sec", float(pack_delay.value()))
        self.log("预算与重试参数已保存到 config.json params")

    # ------------------------------------------------------------------
    # 文件整理（规则引擎）
    # ------------------------------------------------------------------

    def _on_organize(self, files):
        """文件树「整理」：无口令，先 dry-run 预览再确认。聊天 A 级仍不弹窗。"""
        plan = plan_moves(files)
        if not plan.get("files"):
            self.log("整理: 未选中任何文件")
            return
        text = preview_text(plan)
        if not plan.get("moved"):
            QMessageBox.information(self, "整理预览", text)
            return
        reply = QMessageBox.question(
            self, "确认整理",
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            self.log("整理: 已取消")
            return
        result = apply_moves([str(p) for p in plan["files"]])
        for src, dst in result["moved"]:
            self.log(
                f"移动 {Path(src).name} → {Path(dst).parent.name}/",
                undo_info={"type": "move", "src": str(src), "dst": str(dst)}
            )
        for f, err in result["errors"]:
            self.log(f"失败 {Path(f).name}: {err}")
        self.log(f"整理完成: 成功 {len(result['moved'])} 个，失败 {len(result['errors'])} 个")
        self.file_explorer.refresh()

    # ------------------------------------------------------------------
    # 日志右键菜单与撤回
    # ------------------------------------------------------------------

    def _show_log_menu(self, pos):
        """日志列表右键菜单：撤回此操作 / 删除此条 / 清空"""
        item = self.log_list.itemAt(pos)  # 右键点中了哪条日志（空白处为 None）
        menu = QMenu(self)

        if item is not None:
            undo_info = item.data(Qt.ItemDataRole.UserRole)  # 取出该条目携带的撤回数据
            if undo_info:
                undo_action = menu.addAction("撤回此操作")
                undo_action.triggered.connect(lambda: self._undo_log_item(item))

            del_action = menu.addAction("删除此条记录")
            del_action.triggered.connect(lambda: self._delete_log_item(item))
            menu.addSeparator()

        clear_action = menu.addAction("清空全部记录")
        clear_action.triggered.connect(self._clear_logs)

        menu.exec(self.log_list.mapToGlobal(pos))  # 在鼠标位置弹出菜单（坐标需转成全局坐标）

    def _delete_log_item(self, item):
        """删除单条日志：界面移除 + 数据库同步删除"""
        log_id = item.data(Qt.ItemDataRole.UserRole + 1)
        if log_id is not None:
            db.delete_log(log_id)  # 从数据库真正删除
        self.log_list.takeItem(self.log_list.row(item))

    def _clear_logs(self):
        """清空全部日志：界面清空 + 数据库清空"""
        self.log_list.clear()
        db.clear_logs()

    def _undo_log_item(self, item):
        """撤回该条日志对应的操作"""
        undo_info = item.data(Qt.ItemDataRole.UserRole)  # 取出撤回数据
        if not undo_info:
            return

        try:
            op_type = undo_info.get("type")
            if op_type == "move":
                # 反向移动：把文件从 dst 移回 src
                src, dst = Path(undo_info["src"]), Path(undo_info["dst"])
                if not dst.exists():
                    raise FileNotFoundError(f"文件已不在: {dst}")  # 文件可能已被用户手动移走/删除
                src.parent.mkdir(parents=True, exist_ok=True)  # 原目录可能已被清空删除，确保存在
                shutil.move(str(dst), str(src))  # 执行反向移动
                self.file_explorer.refresh()  # 刷新界面
            else:
                QMessageBox.information(self, "提示", f"暂不支持撤回类型: {op_type}")
                return

            # 撤回成功：标记日志并清除撤销数据，防止重复撤回
            item.setText(item.text() + "（已撤回）")
            item.setData(Qt.ItemDataRole.UserRole, None)
            self._paint_log_item(item, item.text(), undone=True)
            log_id = item.data(Qt.ItemDataRole.UserRole + 1)
            if log_id is not None:
                db.mark_log_undone(log_id)
        except Exception as e:
            QMessageBox.warning(self, "撤回失败", str(e))
