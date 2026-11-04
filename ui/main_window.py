# ui/main_window.py
"""
FileMate 主窗口
布局：左侧对话频道（Tab，每个频道内含对话区 + 输入框 + 发送按钮）
     右侧文件管理器 + 操作日志
"""
from datetime import datetime  # 给操作日志打时间戳
from pathlib import Path  # 路径处理（日志里显示文件名等）
import os
import shutil  # 撤回操作时用 shutil.move 把文件移回原位

from PyQt6.QtCore import Qt, QThread, QTimer, QUrl, pyqtSignal  # Qt 常量 + 后台工作线程 + 定时器
from PyQt6.QtGui import QGuiApplication, QDesktopServices  # 屏幕尺寸 / 打开日志文件
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,  # 主窗口与布局/分割器
    QTabWidget, QTabBar, QTextEdit, QLineEdit, QPushButton, QListWidget,  # 频道页、对话区、输入框、按钮、日志列表
    QListWidgetItem, QMenu, QMessageBox, QInputDialog, QLabel, QToolButton,  # 日志条目、右键菜单、弹窗、提示条
    QDialog, QFormLayout, QDoubleSpinBox, QSpinBox, QDialogButtonBox,
    QComboBox, QCheckBox,
)

import config  # 本地配置读写（API Key）
from db import database as db  # SQLite 持久化：频道/消息/日志
from engine.executor import AIFileAssistant, FileRuleEngine  # AI 助手与规则引擎
from engine.rules import ExtensionRule, KeywordRule, DateRule  # 「整理」按钮用到的三种规则
from engine.dispatcher import IntentDispatcher  # 意图调度器：规则优先、AI 兜底
from ui.file_explorer import FileExplorer, get_desktop_path  # 文件管理器组件与桌面路径工具
from ui.style import apply_app_style  # 简洁圆角主题


class _DispatchWorker(QThread):
    """把 dispatcher.dispatch 放到后台线程，避免 UI 假死。"""
    finished_result = pyqtSignal(dict)
    progress = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, dispatcher, text, target, control=None,
                 channel_id=None, resume=False, file_paths=None,
                 ask_history=None):
        super().__init__()
        self._dispatcher = dispatcher
        self._text = text
        self._target = target
        self._control = control
        self._channel_id = channel_id
        self._resume = resume
        self._file_paths = list(file_paths or [])
        self._ask_history = list(ask_history or []) if ask_history else None

    def run(self):
        try:
            result = self._dispatcher.dispatch(
                self._text, self._target, progress=self.progress.emit,
                control=self._control, channel_id=self._channel_id,
                resume=self._resume,
                file_paths=self._file_paths or None,
                ask_history=self._ask_history)
            self.finished_result.emit(result)
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
        self.setMinimumSize(760, 480)  # 最小尺寸下限，防止拖得太小导致右侧面板不可见

        # 全局唯一的 AI 助手实例，API Key 从本地配置读取
        self.ai_assistant = AIFileAssistant(
            api_key=config.get_api_key(),
            base_dir=get_desktop_path()  # 默认操作目录为真实桌面
        )
        # 意图调度器：规则引擎优先（零成本），AI 兜底
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

        # ===== 菜单栏：设置 =====
        settings_menu = self.menuBar().addMenu("设置")  # 创建"设置"菜单
        # M5.2: 模型 / MaiBot / 自学习统一迁至 Web「设置」Tab；PyQt 仅保留启动器与只读提示
        api_action = settings_menu.addAction("聊天模型设置…")
        api_action.triggered.connect(self._edit_chat_model_settings)
        test_action = settings_menu.addAction("测试智谱连通性")
        test_action.triggered.connect(self._test_api)
        budget_action = settings_menu.addAction("预算与重试参数…")
        budget_action.triggered.connect(self._edit_budget_params)
        mem_web_action = settings_menu.addAction("记忆纠错 Web 服务…")
        mem_web_action.triggered.connect(self._edit_memory_correction_web)
        maibot_dir_action = settings_menu.addAction("记忆 / 向量设置…")
        maibot_dir_action.triggered.connect(self._edit_memory_settings)
        clean_action = settings_menu.addAction("表格清洗设置…")
        clean_action.triggered.connect(self._edit_table_cleaning_settings)

        central_widget = QWidget()  # QMainWindow 需要一个中央部件来承载布局
        self.setCentralWidget(central_widget)
        outer_layout = QVBoxLayout(central_widget)  # 最外层垂直布局
        outer_layout.setContentsMargins(10, 10, 10, 10)
        outer_layout.setSpacing(8)

        # ===== 主水平分割器 =====
        self.main_h_splitter = QSplitter(Qt.Orientation.Horizontal)  # 左右可拖动的分割条
        self.main_h_splitter.setHandleWidth(6)
        self.main_h_splitter.setChildrenCollapsible(False)  # 防止面板被拖到完全消失

        # ---------- 左侧：对话频道 ----------
        left_panel = QWidget()  # 左侧面板容器
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        # Tab 栏 + 右侧"新建频道"按钮放在同一行，所以需要一层水平布局
        tab_bar_widget = QWidget()
        tab_bar_layout = QHBoxLayout(tab_bar_widget)
        tab_bar_layout.setContentsMargins(0, 0, 0, 0)
        tab_bar_layout.setSpacing(0)

        self.chat_tabs = QTabWidget()  # 频道 Tab 容器，每个 Tab 是一套完整对话界面
        self.chat_tabs.setTabsClosable(True)  # 每个 Tab 右侧显示 × 关闭按钮
        self.chat_tabs.setMovable(True)  # 允许拖拽调整频道顺序
        self.chat_tabs.setElideMode(Qt.TextElideMode.ElideRight)
        self.chat_tabs.setUsesScrollButtons(True)
        bar = self.chat_tabs.tabBar()
        bar.setExpanding(False)
        bar.setDocumentMode(True)
        self.chat_tabs.tabCloseRequested.connect(self._close_tab)  # 点击 × → 删除频道
        # 启动时从数据库恢复历史频道；没有则新建一个默认频道
        channels = db.list_channels()
        if channels:
            for ch_id, name in channels:
                self.add_chat_tab(name=name, channel_id=ch_id, load_history=True)
        else:
            self.add_chat_tab()  # 首次启动：创建并入库

        add_tab_btn = QPushButton("+")  # 新建频道按钮
        add_tab_btn.setFixedSize(36, 32)
        add_tab_btn.setToolTip("新建频道")
        add_tab_btn.clicked.connect(self.add_chat_tab)  # 点击新增一个频道

        help_btn = QPushButton("?")  # 帮助按钮：弹出完整使用说明
        help_btn.setFixedSize(36, 32)
        help_btn.setToolTip("使用说明")
        help_btn.clicked.connect(self._show_help)

        tab_bar_layout.addWidget(self.chat_tabs)  # Tab 占满左侧
        tab_bar_layout.addWidget(add_tab_btn)  # "+" 按钮贴在 Tab 栏右边
        tab_bar_layout.addWidget(help_btn)  # "❓" 帮助按钮再靠右
        left_layout.addWidget(tab_bar_widget)

        # ---------- 右侧：文件管理器 + 操作日志 ----------
        right_panel = QWidget()  # 右侧面板容器
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        # 操作范围提示条
        self.scope_label = QLabel()  # 显示当前整理操作将作用于哪个目录
        self.scope_label.setObjectName("scopeLabel")
        right_layout.addWidget(self.scope_label)

        self.right_v_splitter = QSplitter(Qt.Orientation.Vertical)  # 右侧上下分割：文件管理器 / 日志
        self.right_v_splitter.setHandleWidth(6)

        self.file_explorer = FileExplorer()  # 文件管理器组件
        self.file_explorer.organize_requested.connect(self._on_organize)  # 点击"整理"按钮 → 执行规则整理
        self.file_explorer.current_path_changed.connect(self._on_path_changed)  # 目录切换 → 更新操作范围提示
        self.log_list = QListWidget()  # 操作日志列表
        self.log_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)  # 启用自定义右键菜单
        self.log_list.customContextMenuRequested.connect(self._show_log_menu)  # 右键 → 撤回/删除/清空菜单
        self._load_logs()  # 启动时恢复历史日志（含已撤回记录）

        self.right_v_splitter.addWidget(self.file_explorer)  # 上半部分：文件管理器
        self.right_v_splitter.addWidget(self.log_list)  # 下半部分：操作日志
        self.right_v_splitter.setSizes([500, 220])  # 初始高度比例 500:220
        right_layout.addWidget(self.right_v_splitter)

        self.main_h_splitter.addWidget(left_panel)  # 左：对话频道
        self.main_h_splitter.addWidget(right_panel)  # 右：文件管理器 + 日志
        self.main_h_splitter.setSizes([500, 790])  # 初始宽度比例 500:790

        outer_layout.addWidget(self.main_h_splitter)  # 分割器铺满整个中央区域

        # 窗口显示后自动聚焦到第一个频道的输入框
        # 使用 QTimer.singleShot 延迟到窗口完全渲染后再设置焦点
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(100, self._focus_first_tab_input)  # 100ms 后聚焦，避免焦点被窗口初始化抢走

    def _focus_first_tab_input(self):
        """窗口启动后聚焦第一个频道的输入框"""
        self._focus_tab_input(self.chat_tabs.currentIndex())

    def add_chat_tab(self, name=None, channel_id=None, load_history=False):
        """
        新增一个聊天频道。
        每个频道内部自带：对话显示区（只读）+ 输入框 + 发送按钮，
        输入框默认获得焦点，点击频道即可直接打字。

        参数:
            name: Tab 显示名（恢复历史频道时传入）
            channel_id: 数据库频道 id；None 表示新建频道，会插入 channels 表
            load_history: 是否从数据库恢复该频道的历史消息
        """
        tab = QWidget()  # 一个频道就是一个 QWidget 页面
        layout = QVBoxLayout(tab)  # 页面内垂直布局：上对话区，下输入行
        layout.setContentsMargins(6, 6, 6, 6)  # 四周留 6px 内边距，避免贴边
        layout.setSpacing(6)

        chat_display = QTextEdit()  # 对话显示区
        chat_display.setReadOnly(True)  # 只读，防止用户误改历史记录
        pal = chat_display.palette()
        pal.setColor(QPalette.ColorRole.Highlight, QColor("#facc15"))
        pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#18181b"))
        chat_display.setPalette(pal)

        # 恢复历史消息；若该频道没有历史（或首次使用），显示欢迎引导
        history = db.list_messages(channel_id) if (load_history and channel_id is not None) else []
        if history:
            for sender, content in history:
                self._append_bubble(chat_display, content, sender)
        else:
            chat_display.setHtml(
                "<div style='color:#6b7280; line-height:1.7; padding:8px'>"
                "<div style='font-size:15px; color:#111827; font-weight:600; margin-bottom:8px'>FileMate</div>"
                "在下方用自然语言下达指令，例如：<br>"
                "· 把 PDF 移到「文档」文件夹<br>"
                "· 按规则合成 Excel / CSV 并导出到本目录<br>"
                "· 帮我把这个目录下的漏扫记录整理成一个表（跨表查询）<br>"
                "<br>右侧进入目标目录后再发送。点右上角 <b>?</b> 看说明。"
                "</div>"
            )

        status_label = QLabel("")
        status_label.setObjectName("queryStatus")
        status_label.setWordWrap(True)
        status_label.setStyleSheet(
            "color:#57534e; font-size:12px; padding:2px 4px;"
        )
        status_label.hide()

        input_row = QHBoxLayout()  # 底部输入行：输入框 + 发送/结束 + 重新 + 导出
        input_box = QLineEdit()
        input_box.setPlaceholderText("输入消息，回车发送...")
        input_box.setMinimumHeight(36)
        send_btn = QPushButton("发送")
        send_btn.setObjectName("btnSend")
        send_btn.setMinimumHeight(36)
        send_btn.setFixedWidth(80)
        resume_btn = QPushButton("重新")
        resume_btn.setObjectName("btnResume")
        resume_btn.setMinimumHeight(36)
        resume_btn.setFixedWidth(80)
        resume_btn.hide()
        export_btn = QPushButton("导出为 Excel")
        export_btn.setObjectName("btnExportQuery")
        export_btn.setMinimumHeight(36)
        export_btn.setFixedWidth(110)
        export_btn.hide()
        export_btn.setToolTip("打开最近一次跨表查询导出的结果表")
        input_row.addWidget(input_box)
        input_row.addWidget(send_btn)
        input_row.addWidget(resume_btn)
        input_row.addWidget(export_btn)

        layout.addWidget(chat_display, 1)
        layout.addWidget(status_label)
        layout.addLayout(input_row)

        tab_name = name or self._next_channel_name()
        index = self.chat_tabs.addTab(tab, tab_name)
        self._attach_tab_close(index)
        self.chat_tabs.setCurrentIndex(index)

        if channel_id is None:
            channel_id = db.add_channel(tab_name)
        tab.setProperty("channel_id", channel_id)
        tab.setProperty("send_btn", send_btn)
        tab.setProperty("resume_btn", resume_btn)
        tab.setProperty("export_btn", export_btn)
        tab.setProperty("input_box", input_box)
        tab.setProperty("chat_display", chat_display)
        tab.setProperty("status_label", status_label)
        tab.setProperty("export_path", "")

        send_btn.clicked.connect(
            lambda: self._on_primary_btn(chat_display, input_box, send_btn, resume_btn))
        resume_btn.clicked.connect(
            lambda: self._on_resume(chat_display, input_box, send_btn, resume_btn))
        input_box.returnPressed.connect(
            lambda: self._on_primary_btn(chat_display, input_box, send_btn, resume_btn))
        export_btn.clicked.connect(
            lambda _checked=False, t=tab: self._open_query_export(t))
        input_box.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.chat_tabs.currentChanged.connect(lambda idx: self._focus_tab_input(idx))

    def _attach_tab_close(self, index: int):
        """关闭钮作为标签内部件，避免原生叉画到标签框外。"""
        btn = QToolButton(self.chat_tabs.tabBar())
        btn.setObjectName("tabClose")
        btn.setText("×")
        btn.setFixedSize(16, 16)
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
        # 全部关闭后自动新建一个空频道（番号会补最小空缺）
        if self.chat_tabs.count() == 0:
            self.add_chat_tab()

    def _append_bubble(self, chat_display, text, sender):
        """以聊天气泡形式往对话区追加一条消息

        参数:
            text: 消息内容（纯文本，内部转义并处理换行）
            sender: "user" 用户消息靠右、黑字加粗；"ai" FileMate 回复靠左
        """
        import html as _html
        body = _html.escape(text).replace("\n", "<br>")  # 转义特殊字符，换行转 <br>
        if sender == "user":
            block = (
                "<table width='100%' cellspacing='0' cellpadding='6'><tr>"
                "<td width='22%'></td>"
                "<td align='right'>"
                "<div style='display:inline-block; background:#18181b; color:#fafafa;"
                " padding:10px 14px; border-radius:14px; text-align:left;"
                " font-weight:600; line-height:1.5'>" + body + "</div>"
                "</td></tr></table>"
            )
        else:
            block = (
                "<table width='100%' cellspacing='0' cellpadding='6'><tr>"
                "<td width='78%' align='left'>"
                "<div style='display:inline-block; background:#f4f4f5; color:#1c1917;"
                " border:1px solid #e4e4e7; padding:10px 14px; border-radius:14px;"
                " line-height:1.55'>" + body + "</div>"
                "</td><td width='22%'></td>"
                "</tr></table>"
            )
        chat_display.append(block)  # QTextEdit.append 支持富文本，气泡逐条堆叠
        # 滚动到底部，保证最新消息可见
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
                resume: bool = False):
        """处理发送：交 AI 代理；dispatch 在后台线程跑，避免前端卡死。"""
        text = input_box.text().strip()
        if not text and not (resume and self._last_task_text):
            return
        if resume and not text:
            text = self._last_task_text
        if self._worker is not None and self._worker.isRunning():
            self._append_bubble(chat_display, "上一条指令还在执行中，可点「结束」停止。", "ai")
            return

        tab = chat_display.parentWidget()
        if send_btn is None and tab is not None:
            send_btn = tab.property("send_btn")
        if resume_btn is None and tab is not None:
            resume_btn = tab.property("resume_btn")

        if not resume:
            self._append_bubble(chat_display, text, "user")
            input_box.clear()
            self.log(f"发送: {text}")
        else:
            self._append_bubble(chat_display, f"（重新/续跑）{text}", "user")
            self.log(f"续跑: {text}")

        channel_id = tab.property("channel_id") if tab is not None else None
        if channel_id is not None:
            db.add_message(channel_id, "user", text if not resume else f"[续跑] {text}")

        def reply_ai(content):
            self._append_bubble(chat_display, content, "ai")
            if channel_id is not None:
                db.add_message(channel_id, "ai", content)

        target = self.file_explorer.current_path
        if target is None:
            if not self.ai_assistant.api_key:
                reply_ai("当前在「此电脑」，请先进入一个具体目录；或配置 API Key 使用 AI。")
                input_box.setFocus()
                return
            target = get_desktop_path()
        if resume and self._last_task_target is not None:
            target = self._last_task_target

        if not config.get_api_key():
            reply_ai("这条指令需要 AI 处理，但尚未配置 API Key（请在记忆纠错 Web「设置」Tab 配置）")
            input_box.setFocus()
            return

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
        # 选中文件 → table_query file_paths；若上轮 needs_user 则带 ask_history 续跑
        selected_paths = []
        try:
            selected_paths = list(self.file_explorer.get_selected_files() or [])
        except Exception:
            selected_paths = []
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
            reply_ai("⏳ 已收到确认，继续跨表查询…")
            input_box.setPlaceholderText("跨表查询追问中，直接回复确认即可…")
        else:
            self._last_task_text = text
            reply_ai("⏳ 处理中…（界面可继续浏览文件；可点黄色「结束」停止）")
            input_box.setPlaceholderText("输入消息，回车发送...")
        self._last_progress_msg = ""
        self._set_tab_status(tab, "正在解析意图…")

        worker = _DispatchWorker(
            self.dispatcher, text, target, control=control,
            channel_id=channel_id, resume=resume,
            file_paths=selected_paths,
            ask_history=ask_history)
        self._worker = worker

        def on_progress(msg):
            text_msg = str(msg or "").strip()
            if not text_msg:
                return
            self.log(f"进度: {text_msg}")
            self._set_tab_status(tab, text_msg)
            # 阶段变化时在对话区追加一条短气泡（避免同文刷屏）
            if text_msg != self._last_progress_msg:
                self._last_progress_msg = text_msg
                # 只把关键节点上屏，细节留在状态条/日志
                key = False
                for needle in (
                    "解析意图",
                    "跨表查询",
                    "QP",
                    "QE",
                    "已锁定",
                    "需要你确认",
                ):
                    if needle in text_msg:
                        key = True
                        break
                if key:
                    self._append_bubble(chat_display, "→ " + text_msg, "ai")

        def on_done(result):
            input_box.setEnabled(True)
            self._set_tab_status(tab, "", visible=False)
            cancelled = bool(result.get("cancelled"))
            tool = str(result.get("tool") or "")
            status = str(result.get("status") or "")
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
                # plan_ready / failed / cancelled：结束追问态，恢复普通输入提示
                self._pending_table_query = None
                input_box.setPlaceholderText("输入消息，回车发送...")
            else:
                # 非查表链路也恢复默认占位（避免残留追问提示）
                if not self._pending_table_query:
                    input_box.setPlaceholderText("输入消息，回车发送...")

            if result.get("handler") == "AI" and not self.ai_assistant.api_key:
                reply_ai("这条指令需要 AI 处理，但尚未配置 API Key（请在记忆纠错 Web「设置」Tab 配置）")
            elif tool == "table_query" and status == "plan_ready":
                preview = result.get("result_preview") or {}
                total = int(preview.get("total_rows") or 0)
                export = result.get("export") if isinstance(result.get("export"), dict) else {}
                export_path = str((export or {}).get("path") or "")
                # 对话区摘要 + 导出按钮；完整预览仍用 dispatcher message
                reply_ai(
                    "查询完成，共 %s 行。点击下方「导出为 Excel」打开结果表。\n\n%s"
                    % (total, result.get("message") or "")
                )
                self._bind_export_button(tab, export_path)
                if export_path:
                    self.log("跨表查询结果已导出: %s" % export_path)
            elif tool == "table_query" and status in ("failed", "error", "cancelled"):
                err = str(result.get("error") or result.get("message") or status)
                reply_ai(
                    "⚠️ 跨表查询失败（%s）：%s"
                    % (result.get("fail_locus") or status, err)
                )
                self._bind_export_button(tab, None)
                self.log("跨表查询失败: %s" % err[:200])
            else:
                reply_ai(f"[{result.get('handler') or 'AI代理'}] {result.get('message') or ''}")
                if tool == "table_query":
                    self._bind_export_button(tab, None)

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
            for op in result.get("operations", []):
                self.log(f"移动 {Path(op['src']).name} → {Path(op['dst']).parent.name}/",
                         undo_info=op)
            if result.get("operations"):
                self.file_explorer.refresh()
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
            self._set_tab_status(tab, "", visible=False)
            # Worker 异常：清追问态、恢复占位、隐藏导出钮
            self._pending_table_query = None
            input_box.setPlaceholderText("输入消息，回车发送...")
            self._bind_export_button(tab, None)
            reply_ai(f"⚠️ 执行异常：{err}")
            self.log("Worker 异常: %s" % err)
            if send_btn and resume_btn:
                self._set_running_ui(send_btn, resume_btn, False, can_resume=True)
            input_box.setFocus()
            self._worker = None

        worker.progress.connect(on_progress)
        worker.finished_result.connect(on_done)
        worker.failed.connect(on_fail)
        worker.start()

    def log(self, message, undo_info=None):
        """添加一条操作日志（同时写入 SQLite，重启后仍在）

        参数:
            message: 日志文本
            undo_info: 可选，dict，描述可撤销的操作。
                       目前支持 {"type": "move", "src": 原路径, "dst": 现路径}
        """
        timestamp = datetime.now().strftime("%H:%M:%S")  # 当前时间，精确到秒
        item = QListWidgetItem(f"[{timestamp}] {message}")  # 日志条目带时间前缀
        if undo_info:
            item.setData(Qt.ItemDataRole.UserRole, undo_info)  # 把撤回所需数据藏进条目，右键菜单时取出
        log_id = db.add_log(message, undo_info)  # 入库，拿到行 id
        item.setData(Qt.ItemDataRole.UserRole + 1, log_id)  # 记下数据库 id，供撤回/删除同步
        self.log_list.addItem(item)
        self.log_list.scrollToBottom()  # 自动滚动到最新一条

    def _load_logs(self):
        """启动时从数据库恢复历史操作日志（含已撤回的，标注状态）"""
        import json as _json
        rows = db.list_logs()
        if not rows:
            self.log_list.addItem("就绪（右键日志可撤回支持的操作）")
            return
        for log_id, message, undo_info_json, undone, created in rows:
            text = f"[{created[11:]}] {message}"  # created_at 的时间部分
            if undone:
                text += "（已撤回）"  # 已撤回的记录保留展示
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole + 1, log_id)
            # 只有"未撤回且带 undo_info"的条目才恢复撤回能力
            if undo_info_json and not undone:
                item.setData(Qt.ItemDataRole.UserRole, _json.loads(undo_info_json))
            self.log_list.addItem(item)
        self.log_list.scrollToBottom()

    # ------------------------------------------------------------------
    # 操作范围提示
    # ------------------------------------------------------------------

    def _on_path_changed(self, path):
        """文件管理器切换目录时，更新操作范围提示"""
        if path == "此电脑":
            # "此电脑"不是真实目录，无法执行整理，提示用户先进入具体目录
            self.scope_label.setText("📂 当前位置：此电脑（请双击进入具体盘符或目录后，再进行整理操作）")
        else:
            # 明确告知用户：整理将作用于当前目录
            self.scope_label.setText(
                f"📂 当前操作范围：{path}\n"
                f"整理功能将自动操作此目录内选中的文件。如需操作其他目录，请先浏览到目标位置，或在对话中说明。"
            )

    # ------------------------------------------------------------------
    # 帮助说明
    # ------------------------------------------------------------------

    def _show_help(self):
        """弹出完整的使用说明对话框"""
        help_text = (
            "<h2>🗂️ FileMate 使用说明</h2>"
            "<p><b>核心模式：</b>AI 全权代理。你只管说大白话，AI 先判断你是要"
            "<b>执行</b>还是<b>咨询</b>、是<b>整理文件</b>还是<b>整理内容</b>，"
            "执行类指令直接操作，咨询类才给方案。需要配置智谱 API Key。</p>"
            "<hr>"
            "<h3>📂 界面布局</h3>"
            "<ul>"
            "<li><b>左侧</b>：对话频道，可开多个。每个频道独立记录对话历史。</li>"
            "<li><b>右侧上方</b>：文件管理器（类似资源管理器），对话操作的范围就是当前目录。</li>"
            "<li><b>右侧下方</b>：操作日志。右键日志可撤回文件移动。</li>"
            "<li><b>顶部黄色提示条</b>：当前操作范围（文件管理器浏览的目录）。</li>"
            "</ul>"
            "<hr>"
            "<h3>💬 怎么对话</h3>"
            "<p>在频道底部的输入框里打字，回车或点「发送」。</p>"
            "<p><b>⚡ 执行模式（直接操作，不出方案）：</b></p>"
            "<ul>"
            "<li>把图书馆文件夹里的 PDF 整理到「文档」</li>"
            "<li>文件名带「合同」的归档到合同类</li>"
            "<li>把上周修改过的文件备份一下</li>"
            "<li>整理这些文档，并整理这些文档的内容（文件+内容一次办完）</li>"
            "<li>把这里的文档全部整理出报告</li>"
            "</ul>"
            "<p><b>🧠 内容整理（读内容不动文件，生成 Word 报告）：</b></p>"
            "<ul>"
            "<li>读一下这份文档，总结关键信息</li>"
            "<li>把这几份文档的内容汇总一下</li>"
            "<li>统计这两个 CSV 的数据并联查</li>"
            "<li>总结一下这份合同的关键条款</li>"
            "</ul>"
            "<p style='color:#666'>内容整理会自动判别文档类型（技术/小说/教程/项目/合同/学术），"
            "按类型分块抽取实体与事实，结果存入本地知识库并生成 Word 报告（当前目录 FileMate报告/ 下）。"
            "失败自动重试，重试不成会降级为摘要并明确标注，绝不静默丢失。</p>"
            "<p><b>📋 咨询模式（只给方案和预览，不动文件）：</b></p>"
            "<ul>"
            "<li>这些 PDF 怎么整理比较好？</li>"
            "<li>生成报告将会涉及哪些文件？</li>"
            "<li>能不能先给我一个分类方案，先别动</li>"
            "</ul>"
            "<p style='color:#666'>看到「📋 方案预览」后，直接说「执行」或改成执行语气即可立即操作。</p>"
            "<hr>"
            "<h3>🔧 基础操作</h3>"
            "<ul>"
            "<li>点击 <b>+</b> 新建对话频道。</li>"
            "<li>在右侧文件管理器中选中文件，再点「🗂 整理」可按扩展名一键整理（本地按钮，不耗 AI）。</li>"
            "<li>右下角日志右键 → 「撤回此操作」可撤销文件移动。</li>"
            "<li>启动记忆纠错 Web 后，在「设置」Tab 配置聊天模型 API Key（AI 代理必需）。</li>"
            "</ul>"
            "<p style='color:#888; font-size:12px'>版本 0.2 | AI 的决策只在当前目录内生效，文件夹名经过安全校验；"
            "整理只会移动文件或生成报告，不会删除原文件。</p>"
        )
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

    def _edit_memory_correction_web(self):
        """
        M4/M4.1：端口 + 一键启停（subprocess）+ 复制命令 + /healthz + 打开日志。
        不侵入查询引擎 / A'/A/B/S；启停走 WebServiceManager，不用 QThread。
        """
        from urllib.request import urlopen

        from web.memory_correction.port_settings import (
            resolve_port,
            save_gui_port,
            startup_command,
        )
        from web.memory_correction.web_service_manager import (
            ServiceStatus,
            get_shared_manager,
        )

        mgr = get_shared_manager()
        dialog = QDialog(self)
        dialog.setWindowTitle("记忆纠错 Web 服务")
        form = QFormLayout(dialog)

        port_box = QSpinBox()
        port_box.setRange(1, 65535)
        port_box.setValue(int(resolve_port()))
        form.addRow("端口（默认 8765）", port_box)

        from pipeline.block_cross_table.memory_rag.merge_settings import (
            resolve_maibot_data_dir,
            save_maibot_data_dir,
        )
        from pipeline.block_cross_table.memory_rag.maibot_ro import (
            discover_maibot_db_path,
        )

        maibot_row = QHBoxLayout()
        maibot_dir_edit = QLineEdit()
        maibot_dir_edit.setText(str(resolve_maibot_data_dir() or ""))
        maibot_dir_edit.setPlaceholderText(
            "请点击右侧按钮选择 MaiBot 的 data 目录…"
        )
        btn_browse_maibot = QPushButton("浏览…")
        maibot_warn = QLabel("")
        maibot_warn.setStyleSheet("color: #9b2226; font-weight: 600;")
        maibot_warn.setWordWrap(True)

        def _refresh_maibot_warn(path_text=None):
            path = (
                maibot_dir_edit.text().strip()
                if path_text is None
                else str(path_text or "").strip()
            )
            if not path:
                maibot_warn.setText("")
                return
            if discover_maibot_db_path(path) is None:
                maibot_warn.setText("未找到数据库文件")
            else:
                maibot_warn.setText("")

        def _browse_maibot():
            from PyQt6.QtWidgets import QFileDialog

            chosen = QFileDialog.getExistingDirectory(
                dialog,
                "选择 MaiBot 数据目录（只读）",
                maibot_dir_edit.text().strip() or "",
            )
            if not chosen:
                return
            maibot_dir_edit.setText(chosen)
            save_maibot_data_dir(chosen)
            _refresh_maibot_warn(chosen)

        btn_browse_maibot.clicked.connect(_browse_maibot)
        maibot_dir_edit.textChanged.connect(lambda _t: _refresh_maibot_warn())
        maibot_row.addWidget(maibot_dir_edit)
        maibot_row.addWidget(btn_browse_maibot)
        form.addRow("MaiBot 数据目录", maibot_row)
        form.addRow(maibot_warn)
        _refresh_maibot_warn()

        hint = QLabel(
            "环境变量 MEMORY_CORRECTION_PORT 优先于本设置。"
            "一键启动使用 subprocess 子进程；仅监听 127.0.0.1。"
            "MaiBot 目录以 mode=ro 只读打开，严禁写入对方库。"
            "完整模型/引擎设置见「设置 → 聊天模型设置 / 记忆·向量设置」。"
        )
        hint.setWordWrap(True)
        form.addRow(hint)

        status_label = QLabel("状态：%s" % mgr.status)
        form.addRow(status_label)

        btn_start = QPushButton("启动 Web 服务")
        btn_stop = QPushButton("停止 Web 服务")
        btn_log = QPushButton("打开日志")
        btn_copy = QPushButton("复制启动命令")
        btn_health = QPushButton("检查服务状态")
        row = QHBoxLayout()
        row.addWidget(btn_start)
        row.addWidget(btn_stop)
        row.addWidget(btn_log)
        form.addRow(row)
        row2 = QHBoxLayout()
        row2.addWidget(btn_copy)
        row2.addWidget(btn_health)
        form.addRow(row2)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        form.addRow(buttons)

        poll_timer = QTimer(dialog)
        poll_timer.setInterval(max(1, int(mgr.poll_interval_s * 1000)))
        start_deadline = {"t": 0.0}

        def _refresh_status():
            status_label.setText("状态：%s" % mgr.status)

        def _copy_cmd():
            cmd = startup_command(int(port_box.value()))
            QGuiApplication.clipboard().setText(cmd)
            self.log("已复制启动命令（需 PYTHONPATH 含 web/）")

        def _health():
            port = int(port_box.value())
            url = "http://127.0.0.1:%s/healthz" % port
            try:
                with urlopen(url, timeout=2) as resp:
                    body = resp.read().decode("utf-8", "replace")
                    ok = resp.status == 200 and "ok" in body
                    status_label.setText(
                        "状态：%s（healthz=%s）"
                        % (mgr.status, "ok" if ok else "fail")
                    )
            except Exception as exc:
                status_label.setText(
                    "状态：%s（healthz fail: %s）" % (mgr.status, exc)
                )

        def _on_poll():
            import time as _time

            outcome = mgr.poll_ready_once()
            if outcome is not None:
                poll_timer.stop()
                _refresh_status()
                if outcome.ok:
                    self.log("记忆纠错 Web 已启动（PID=%s）" % outcome.pid)
                else:
                    QMessageBox.warning(dialog, "启动失败", outcome.detail or mgr.status)
                return
            if _time.monotonic() >= start_deadline["t"]:
                poll_timer.stop()
                mgr.wait_until_ready(timeout_s=0)
                _refresh_status()
                QMessageBox.warning(
                    dialog, "启动失败", "启动超时，查看日志"
                )

        def _start():
            if mgr.is_running:
                _refresh_status()
                return
            port = int(port_box.value())
            save_gui_port(port)
            outcome = mgr.start(port, wait_ready=False)
            _refresh_status()
            if not outcome.ok:
                QMessageBox.warning(
                    dialog, "启动失败", outcome.detail or mgr.status
                )
                return
            if mgr.status_code == ServiceStatus.RUNNING:
                self.log("记忆纠错 Web 已启动（PID=%s）" % mgr.pid)
                return
            import time as _time

            start_deadline["t"] = _time.monotonic() + float(mgr.ready_timeout_s)
            poll_timer.start()

        def _stop():
            poll_timer.stop()
            outcome = mgr.stop()
            _refresh_status()
            if not outcome.ok:
                QMessageBox.warning(dialog, "停止失败", outcome.detail or mgr.status)
            else:
                self.log("记忆纠错 Web 已停止")

        def _open_log():
            path = mgr.open_log_file()
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

        def _save():
            save_gui_port(int(port_box.value()))
            save_maibot_data_dir(maibot_dir_edit.text().strip())
            QMessageBox.information(
                dialog,
                "已保存",
                "端口与 MaiBot 数据目录已写入设置。"
                "模型 / 自学习引擎请在 Web「设置」中配置。",
            )
            dialog.accept()

        def _cleanup():
            poll_timer.stop()

        poll_timer.timeout.connect(_on_poll)
        btn_start.clicked.connect(_start)
        btn_stop.clicked.connect(_stop)
        btn_log.clicked.connect(_open_log)
        btn_copy.clicked.connect(_copy_cmd)
        btn_health.clicked.connect(_health)
        buttons.accepted.connect(_save)
        buttons.rejected.connect(dialog.reject)
        dialog.finished.connect(_cleanup)
        _refresh_status()
        dialog.exec()

    def _open_web_settings_hint(self):
        """M5.2: model / MaiBot / self-learning live in Web Settings tab."""
        from web.memory_correction.port_settings import resolve_port

        port = int(resolve_port())
        url = "http://127.0.0.1:%s/" % port
        QMessageBox.information(
            self,
            "已迁至 Web 设置",
            "聊天模型、MaiBot 目录、自学习引擎与后台自动学习\n"
            "请在记忆纠错 Web 的「设置」Tab 中配置。\n\n"
            "先通过「设置 → 记忆纠错 Web 服务…」启动服务，再打开：\n%s" % url,
        )

    def closeEvent(self, event):
        """主程序关闭：若记忆纠错 Web 子进程仍在跑，询问是否停止。"""
        try:
            from web.memory_correction.web_service_manager import get_shared_manager

            mgr = get_shared_manager()
        except Exception:
            super().closeEvent(event)
            return

        if mgr.is_running:
            pid = mgr.pid
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle("Web 服务还在运行")
            box.setText("Web 服务还在运行，是否停止")
            box.setInformativeText("PID=%s" % pid)
            box.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            box.setDefaultButton(QMessageBox.StandardButton.Yes)
            ans = box.exec()
            if ans == QMessageBox.StandardButton.Yes:
                mgr.stop()
            else:
                QMessageBox.information(
                    self,
                    "保留进程",
                    "进程 PID=%s，可手动停止" % pid,
                )
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

    def _edit_api_key(self):
        """弹出对话框设置智谱 API Key，保存到本地 config.json"""
        key, ok = QInputDialog.getText(
            self, "设置智谱 API Key",
            "请输入智谱开放平台 API Key（留空则清除）:",
            text=config.get_api_key()
        )
        if ok:
            key = key.strip()
            config.set_api_key(key)
            self.ai_assistant.api_key = key
            self.log("智谱 API Key 已更新" if key else "API Key 已清除")
            if key:
                from ai.client import test_connectivity
                result = test_connectivity(key)
                box = QMessageBox.information if result["ok"] else QMessageBox.warning
                box(self, "智谱连通性", result["message"])

    def _test_api(self):
        """菜单：测试智谱连通性"""
        from ai.client import test_connectivity
        result = test_connectivity()
        box = QMessageBox.information if result["ok"] else QMessageBox.warning
        box(self, "智谱连通性", result["message"])

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
        """对文件管理器中选中的文件执行规则引擎整理"""
        # 只处理文件，跳过文件夹
        files = [f for f in files if Path(f).is_file()]
        if not files:
            self.log("整理: 未选中任何文件")
            return

        engine = FileRuleEngine(".")  # 临时引擎（规则以文件自身所在目录为基准，base_dir 不参与目标拼接）
        # 规则 1：按扩展名分类到 文档/图片/视频/音频/压缩包/程序
        engine.add_rule(ExtensionRule({
            '.pdf': '文档', '.doc': '文档', '.docx': '文档', '.txt': '文档',
            '.jpg': '图片', '.jpeg': '图片', '.png': '图片', '.gif': '图片',
            '.mp4': '视频', '.mov': '视频', '.avi': '视频',
            '.mp3': '音频', '.wav': '音频',
            '.zip': '压缩包', '.rar': '压缩包', '.7z': '压缩包',
            '.exe': '程序', '.msi': '程序',
        }))
        # 规则 2：文件名含"合同"的归入 合同类/（优先级低于扩展名）
        engine.add_rule(KeywordRule({'合同': '合同类'}))
        # 规则 3：兜底——其余文件按修改时间归档到 年/月
        engine.add_rule(DateRule("%Y/%m"))

        result = engine.apply_rules(files)  # 执行整理，拿到移动/失败清单

        # 每次移动记录一条带撤回信息的日志
        for src, dst in result["moved"]:
            self.log(
                f"移动 {Path(src).name} → {Path(dst).parent.name}/",
                undo_info={"type": "move", "src": str(src), "dst": str(dst)}
            )
        for f, err in result["errors"]:
            self.log(f"失败 {Path(f).name}: {err}")

        self.log(f"整理完成: 成功 {len(result['moved'])} 个，失败 {len(result['errors'])} 个")
        self.file_explorer.refresh()  # 刷新文件列表，让界面反映最新目录结构

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
            # 数据库同步标记"已撤回"，但记录本身保留（重启后仍能看到这条日志）
            log_id = item.data(Qt.ItemDataRole.UserRole + 1)
            if log_id is not None:
                db.mark_log_undone(log_id)
        except Exception as e:
            QMessageBox.warning(self, "撤回失败", str(e))
