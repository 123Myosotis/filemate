# ui/file_explorer.py
"""
自定义文件资源管理器组件
模仿 Windows 资源管理器的核心功能：
- 导航工具栏（后退/前进/上级/刷新/视图切换）
- 可编辑地址栏（支持输入路径跳转）
- 图标/列表两种视图模式
- 右键菜单（打开/重命名/删除/复制/剪切/粘贴/属性）
- 双击进入文件夹或系统默认打开文件
- 浏览历史记录（前进/后退）
- 状态栏显示文件/文件夹数量
"""
import os  # 路径拼接、盘符检测、文件重命名/删除等系统操作
import platform  # 判断操作系统（Windows 显示盘符，其他系统从主目录开始）
import shutil  # 查询磁盘用量（shutil.disk_usage）

from pathlib import Path  # 跨平台路径对象

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QToolBar, QComboBox, QListView, QTreeView,
    QStackedWidget, QLabel, QAbstractItemView, QMenu, QMessageBox, QInputDialog,
    QSplitter, QTreeWidget, QTreeWidgetItem, QFileIconProvider, QFrame, QToolButton,
    QScrollArea, QSizePolicy,
)
from PyQt6.QtCore import Qt, QDir, QFileInfo, QSize, QUrl, QMimeData, pyqtSignal, QTimer
from PyQt6.QtGui import QStandardItemModel, QStandardItem, QIcon, QDesktopServices, QAction


class _ExplorerItemModel(QStandardItemModel):
    """支持拖出本地文件 URL，便于拖到对话区附加。"""

    def mimeTypes(self):
        return ["text/uri-list"]

    def mimeData(self, indexes):
        urls = []
        seen = set()
        for idx in indexes or []:
            if idx.column() != 0:
                continue
            item = self.itemFromIndex(idx)
            if item is None:
                continue
            path = item.data(Qt.ItemDataRole.UserRole + 1)
            if not path:
                continue
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            urls.append(QUrl.fromLocalFile(key))
        md = QMimeData()
        if urls:
            md.setUrls(urls)
        return md

    def flags(self, index):
        f = super().flags(index)
        if index.isValid() and index.column() == 0:
            f |= Qt.ItemFlag.ItemIsDragEnabled
        return f


def get_desktop_path() -> Path:
    """获取真实桌面路径（处理 OneDrive 等重定向情况）"""
    try:
        import winreg  # 延迟导入：仅 Windows 上可用
        # 打开注册表「用户外壳文件夹」键，读取桌面真实位置（可能被 OneDrive 重定向）
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        )
        value, _ = winreg.QueryValueEx(key, "Desktop")  # 值里可能含 %USERPROFILE% 等环境变量
        winreg.CloseKey(key)
        return Path(os.path.expandvars(value))  # 展开环境变量后返回
    except Exception:
        # 读注册表失败（如非 Windows）时的降级方案
        onedrive = Path.home() / "OneDrive" / "Desktop"  # 优先 OneDrive 桌面
        if onedrive.exists():
            return onedrive
        return Path.home() / "Desktop"  # 兜底常规桌面


class FileExplorer(QWidget):

    # 点击"整理"按钮时发出，携带当前选中的文件路径列表
    organize_requested = pyqtSignal(list)
    # 当前浏览目录变化时发出，携带新路径
    current_path_changed = pyqtSignal(str)
    # 右键「附加到当前对话」→ 主窗口把文件塞进当前频道附件
    attach_to_chat_requested = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self.setObjectName("fileExplorer")  # 主题里压密度

        # 浏览历史记录与当前位置
        self.history = []  # 访问过的路径列表（用于后退/前进）
        self.history_index = -1  # 当前位置在 history 中的下标
        self.current_path = None  # 当前浏览目录（"此电脑"时为 None）

        # 系统原生图标提供者（获取 Windows 资源管理器同款图标）
        self.icon_provider = QFileIconProvider()

        self.init_ui()  # 构建全部界面元素

        # 启动时按系统显示初始内容
        if platform.system() == "Windows":
            self.show_drives()  # Windows：先显示"此电脑"盘符页
        else:
            self.load_directory(QDir.homePath())  # 其他系统：从用户主目录开始

    def _emit_organize(self):
        """发出整理请求信号，携带当前选中的文件路径"""
        self.organize_requested.emit(self.get_selected_files())

    def _build_nav_tree(self):
        """构建左侧导航树：快速访问 + 盘符"""
        home = Path.home()  # 用户主目录
        desktop = get_desktop_path()  # 真实桌面路径

        # 快速访问
        quick = QTreeWidgetItem(self.nav_tree, ["⭐ 快速访问"])  # 分组节点
        quick.setData(0, Qt.ItemDataRole.UserRole, None)  # 分组节点本身不可跳转
        for name, path in [
            ("🖥️ 桌面", desktop),
            ("📄 文档", home / "Documents"),
            ("⬇️ 下载", home / "Downloads"),
            ("🖼️ 图片", home / "Pictures"),
        ]:
            if path.exists():  # 只显示真实存在的目录（不同系统目录可能不同名/缺失）
                item = QTreeWidgetItem(quick, [name])
                item.setData(0, Qt.ItemDataRole.UserRole, str(path))  # 把路径藏在节点里，点击时取出
        quick.setExpanded(True)  # 默认展开

        # 启动后默认显示真实桌面
        QTimer.singleShot(0, lambda: self.load_directory(str(desktop)))  # 延迟到事件循环启动后再加载，避免初始化顺序问题

        # 此电脑（盘符）
        pc = QTreeWidgetItem(self.nav_tree, ["💻 此电脑"])
        pc.setData(0, Qt.ItemDataRole.UserRole, "此电脑")  # 特殊标记，点击后显示盘符页
        if platform.system() == "Windows":
            for letter in range(65, 91):  # 遍历 A-Z 的 ASCII 码
                drive = chr(letter) + ":\\"
                if os.path.exists(drive):  # 只列出实际存在的盘符
                    item = QTreeWidgetItem(pc, [f"💽 {chr(letter)}:"])
                    item.setData(0, Qt.ItemDataRole.UserRole, drive)
        pc.setExpanded(True)  # 默认展开

    def _on_nav_clicked(self, item, column):
        """左侧导航树点击时，右侧加载对应目录"""
        path = item.data(0, Qt.ItemDataRole.UserRole)  # 取出节点里藏的路径
        if path:
            self.load_directory(path)

    def get_selected_files(self):
        """获取当前选中的文件路径列表"""
        paths = []

        if self.stacked_widget.currentIndex() == 0:
            indexes = self.list_view.selectedIndexes()  # 图标模式：直接取选中项
        else:
            indexes = [idx for idx in self.tree_view.selectedIndexes() if idx.column() == 0]  # 列表模式一行有 4 列，只取第 0 列（名称列）避免重复

        for index in indexes:
            item = self.model.itemFromIndex(index)  # 索引 → 数据项
            if item:
                path = item.data(Qt.ItemDataRole.UserRole + 1)  # 完整路径存在 UserRole+1 上
                if path:
                    paths.append(path)

        return paths

    def init_ui(self):
        """
        布局结构（从上到下）：
        [工具栏：后退 | 前进 | 上级 | 刷新 | 地址栏 | 视图切换]
        [文件列表区域：图标模式 / 列表模式（二选一）]
        [状态栏：显示文件/文件夹数量]
        """
        layout = QVBoxLayout(self)  # 组件整体垂直布局：工具栏 / 分割器（内含列表）
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        # ========== 1. 工具栏 ==========
        toolbar = QToolBar()
        toolbar.setMovable(False)  # 禁止用户拖动工具栏位置
        toolbar.setIconSize(QSize(14, 14))

        self.back_btn = QAction("◀", self)  # 后退
        self.back_btn.triggered.connect(self.go_back)
        toolbar.addAction(self.back_btn)

        self.forward_btn = QAction("▶", self)  # 前进
        self.forward_btn.triggered.connect(self.go_forward)
        toolbar.addAction(self.forward_btn)

        self.up_btn = QAction("▲", self)  # 返回上一级目录
        self.up_btn.triggered.connect(self.go_up)
        toolbar.addAction(self.up_btn)

        self.refresh_btn = QAction("🔄", self)  # 刷新当前目录
        self.refresh_btn.triggered.connect(self.refresh)
        toolbar.addAction(self.refresh_btn)

        # 可编辑地址栏隐藏保留（程序跳转/粘贴路径）；界面主用面包屑
        self.address_bar = QComboBox()
        self.address_bar.setEditable(True)
        self.address_bar.setPlaceholderText("输入路径...")
        self.address_bar.lineEdit().returnPressed.connect(self.navigate_to_address)
        self.address_bar.currentIndexChanged.connect(self.on_address_selected)
        self.address_bar.hide()

        path_edit_btn = QAction("路径…", self)
        path_edit_btn.setToolTip("输入或粘贴完整路径跳转")
        path_edit_btn.triggered.connect(self._prompt_path_jump)
        toolbar.addAction(path_edit_btn)

        # 视图：默认列表；可切网格
        self.view_mode_btn = QAction("网格", self)
        self.view_mode_btn.setCheckable(True)
        self.view_mode_btn.setToolTip("切换网格 / 列表视图")
        self.view_mode_btn.triggered.connect(self.toggle_view_mode)
        toolbar.addAction(self.view_mode_btn)

        self.organize_btn = QAction("整理", self)
        self.organize_btn.setToolTip("整理当前选中文件")
        self.organize_btn.triggered.connect(self._emit_organize)
        toolbar.addAction(self.organize_btn)

        layout.addWidget(toolbar)

        # 面包屑：桌面 › 内容分类（点击回跳）
        self.breadcrumb = QFrame()
        self.breadcrumb.setObjectName("breadcrumbBar")
        crumb_scroll = QScrollArea()
        crumb_scroll.setWidgetResizable(True)
        crumb_scroll.setFrameShape(QFrame.Shape.NoFrame)
        crumb_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        crumb_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        crumb_scroll.setFixedHeight(28)
        crumb_host = QWidget()
        self._crumb_layout = QHBoxLayout(crumb_host)
        self._crumb_layout.setContentsMargins(2, 0, 2, 0)
        self._crumb_layout.setSpacing(0)
        self._crumb_layout.addStretch(1)
        crumb_scroll.setWidget(crumb_host)
        crumb_outer = QHBoxLayout(self.breadcrumb)
        crumb_outer.setContentsMargins(0, 0, 0, 0)
        crumb_outer.addWidget(crumb_scroll)
        layout.addWidget(self.breadcrumb)

        # ========== 2. 左右分栏：左侧导航树 + 右侧文件列表 ==========
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setHandleWidth(6)
        self.splitter.setChildrenCollapsible(True)  # 导航树可收到最小

        # --- 左侧导航树（轻量自绘，避免触发系统图标缓存） ---
        self.nav_tree = QTreeWidget()
        self.nav_tree.setHeaderHidden(True)
        self.nav_tree.setIndentation(12)
        self.nav_tree.setMinimumWidth(72)
        self.nav_tree.itemClicked.connect(self._on_nav_clicked)
        self._build_nav_tree()
        self.splitter.addWidget(self.nav_tree)

        # --- 右侧：原有文件列表区域 ---
        right_container = QWidget()
        right_layout = QVBoxLayout(right_container)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        self.list_view = QListView()
        self.list_view.setViewMode(QListView.ViewMode.IconMode)
        self.list_view.setIconSize(QSize(24, 24))
        self.list_view.setGridSize(QSize(72, 56))
        self.list_view.setWordWrap(True)
        self.list_view.setSpacing(2)
        self.list_view.setResizeMode(QListView.ResizeMode.Adjust)
        self.list_view.setUniformItemSizes(False)
        self.list_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list_view.setDragEnabled(True)
        self.list_view.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.list_view.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.list_view.setMouseTracking(True)
        self.list_view.doubleClicked.connect(self.on_file_double_click)
        self.list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_view.customContextMenuRequested.connect(self.show_context_menu)

        self.tree_view = QTreeView()
        self.tree_view.setRootIsDecorated(False)
        self.tree_view.setAlternatingRowColors(True)
        self.tree_view.setIconSize(QSize(16, 16))
        self.tree_view.setIndentation(0)
        self.tree_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree_view.setDragEnabled(True)
        self.tree_view.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.tree_view.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.tree_view.setMouseTracking(True)
        self.tree_view.doubleClicked.connect(self.on_file_double_click)
        self.tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree_view.customContextMenuRequested.connect(self.show_context_menu)

        self.stacked_widget = QStackedWidget()
        self.stacked_widget.addWidget(self.list_view)   # 0 = 网格
        self.stacked_widget.addWidget(self.tree_view)   # 1 = 列表（默认）
        self.stacked_widget.setCurrentIndex(1)
        self.view_mode_btn.setChecked(False)
        self.view_mode_btn.setText("网格")
        self.view_mode_btn.setToolTip("当前：列表视图（点击切到网格）")
        right_layout.addWidget(self.stacked_widget, 1)

        # ========== 3. 状态栏 ==========
        self.status_label = QLabel()
        right_layout.addWidget(self.status_label)

        self.splitter.addWidget(right_container)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([110, 320])  # 导航更窄
        layout.addWidget(self.splitter, 1)

        # ========== 4. 数据模型（可拖出文件 URL）==========
        self.model = _ExplorerItemModel()
        self.list_view.setModel(self.model)
        self.tree_view.setModel(self.model)

    # -------------------------------------------------------------------------
    # 核心功能方法
    # -------------------------------------------------------------------------

    def show_drives(self):
        """显示所有盘符（仅Windows系统），相当于资源管理器的"此电脑" """
        self.model.clear()  # 清空文件列表

        # 更新地址栏为"此电脑"；blockSignals 防止 programmatic 修改触发跳转信号造成递归
        self.address_bar.blockSignals(True)
        self.address_bar.clear()
        self.address_bar.addItem("此电脑")
        self.address_bar.setCurrentIndex(0)
        self.address_bar.blockSignals(False)

        if self.stacked_widget.currentIndex() == 1:
            self.model.setHorizontalHeaderLabels(["名称", "大小", "类型", "修改日期"])  # 列表模式需要表头

        drives = []
        for letter in range(65, 91):  # A-Z
            drive = chr(letter) + ":\\"  # 拼出盘符路径，如 C:\
            if os.path.exists(drive):  # 只列出真实存在的盘符
                item = QStandardItem(f"{chr(letter)}:\\")
                item.setIcon(self.icon_provider.icon(QFileInfo(drive)))  # 系统原生磁盘图标
                item.setData("drive", Qt.ItemDataRole.UserRole)  # 条目类型标记：盘符
                item.setData(drive, Qt.ItemDataRole.UserRole + 1)  # 完整路径（供双击/选中时取用）

                try:
                    usage = shutil.disk_usage(drive)  # 查询磁盘容量
                    total_gb = usage.total / (1024 ** 3)
                    free_gb = usage.free / (1024 ** 3)
                    size_str = f"{total_gb:.1f} GB (剩余 {free_gb:.1f} GB)"
                except Exception:
                    size_str = ""  # 个别盘（如空光驱）查询失败时留空

                if self.stacked_widget.currentIndex() == 1:
                    # 列表模式：一行 4 列（名称/大小/类型/修改日期）
                    size_item = QStandardItem(size_str)
                    type_item = QStandardItem("本地磁盘")
                    date_item = QStandardItem("")
                    self.model.appendRow([item, size_item, type_item, date_item])
                else:
                    tip = f"{drive}\n{size_str}".strip() if size_str else drive
                    item.setToolTip(tip)
                    self.model.appendRow(item)

                drives.append(drive)

        self.status_label.setText(f"共 {len(drives)} 个驱动器")
        self.current_path = None
        self.current_path_changed.emit("此电脑")
        self._rebuild_breadcrumb(None)

    def load_directory(self, path):
        """加载指定目录的内容并显示"""
        try:
            if not path or path == "此电脑":
                # 空路径或"此电脑"特殊页：Windows 显示盘符，其他系统回退到主目录
                if platform.system() == "Windows":
                    self.show_drives()
                else:
                    self.load_directory(QDir.homePath())
                return

            path = os.path.normpath(path)  # 规范化路径（消除 .. / 多余分隔符等）
            dir_path = QDir(path)

            if not dir_path.exists():
                QMessageBox.warning(self, "错误", f"路径不存在: {path}")
                return

            # 同步地址栏显示；blockSignals 防止触发 currentIndexChanged 造成重复加载
            self.address_bar.blockSignals(True)
            self.address_bar.clear()
            self.address_bar.addItem(dir_path.absolutePath())
            self.address_bar.setCurrentIndex(0)
            self.address_bar.blockSignals(False)

            # 记录当前目录（调度器执行规则时的操作范围）
            self.current_path = dir_path.absolutePath()
            # 通知主窗口当前操作目录已变化
            self.current_path_changed.emit(self.current_path)

            self.model.clear()  # 清空旧目录内容
            if self.stacked_widget.currentIndex() == 1:
                self.model.setHorizontalHeaderLabels(["名称", "大小", "类型", "修改日期"])

            # 读取目录条目：排除 "." 和 ".."，包含文件夹和文件，按名称排序且文件夹在前
            entries = dir_path.entryInfoList(
                QDir.Filter.NoDotAndDotDot | QDir.Filter.AllDirs | QDir.Filter.Files,
                QDir.SortFlag.Name | QDir.SortFlag.DirsFirst
            )

            for file_info in entries:
                item = QStandardItem(file_info.fileName())  # 显示名 = 文件名
                # 使用 Windows 系统原生图标（文件夹/各类文件）
                item.setIcon(self.icon_provider.icon(file_info))

                if file_info.isDir():
                    item.setData("folder", Qt.ItemDataRole.UserRole)  # 标记类型：文件夹
                else:
                    item.setData("file", Qt.ItemDataRole.UserRole)  # 标记类型：文件

                abs_path = file_info.absoluteFilePath()
                item.setData(abs_path, Qt.ItemDataRole.UserRole + 1)
                # Tooltip：全路径 + 大小（网格截断时也能看全名）
                if file_info.isDir():
                    item.setToolTip(abs_path)
                else:
                    item.setToolTip(
                        f"{abs_path}\n{self.format_size(file_info.size())}"
                    )

                if self.stacked_widget.currentIndex() == 1:
                    size_item = QStandardItem(self.format_size(file_info.size()))
                    type_item = QStandardItem(
                        file_info.suffix() if file_info.isFile() else "文件夹"
                    )
                    date_item = QStandardItem(
                        file_info.lastModified().toString("yyyy-MM-dd hh:mm")
                    )
                    size_item.setToolTip(item.toolTip())
                    self.model.appendRow([item, size_item, type_item, date_item])
                else:
                    self.model.appendRow(item)

            file_count = sum(1 for info in entries if info.isFile())
            dir_count = len(entries) - file_count
            self.status_label.setText(f"{dir_count} 个文件夹，{file_count} 个文件")

            self.add_to_history(dir_path.absolutePath())
            self._rebuild_breadcrumb(dir_path.absolutePath())

        except PermissionError:
            QMessageBox.warning(self, "权限错误", f"没有权限访问: {path}")
        except Exception as e:
            QMessageBox.warning(self, "错误", f"无法访问目录: {str(e)}")

    def format_size(self, size):
        """格式化文件大小为人类可读的字符串"""
        if size < 1024:
            return f"{size} B"  # 不足 1KB 直接显示字节
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        elif size < 1024 * 1024 * 1024:
            return f"{size / 1024 / 1024:.1f} MB"
        else:
            return f"{size / 1024 / 1024 / 1024:.2f} GB"  # GB 级保留两位小数更精确

    # -------------------------------------------------------------------------
    # 导航方法
    # -------------------------------------------------------------------------

    def navigate_to_address(self):
        """地址栏输入路径回车跳转"""
        path = self.address_bar.currentText().strip()  # 取输入内容并去空白
        if path:
            self.load_directory(path)

    def on_address_selected(self, index):
        """从地址栏下拉列表选择历史路径"""
        if index >= 0:
            path = self.address_bar.itemText(index)
            # 与当前显示文本不同才跳转，避免程序自身改地址栏时重复加载
            if path and path != self.address_bar.currentText():
                self.load_directory(path)

    def go_back(self):
        if self.history_index > 0:  # 还有更早的记录才可后退
            self.history_index -= 1
            self.load_directory(self.history[self.history_index])

    def go_forward(self):
        if self.history_index < len(self.history) - 1:  # 还有更晚的记录才可前进
            self.history_index += 1
            self.load_directory(self.history[self.history_index])

    def go_up(self):
        current_path = self.address_bar.currentText()
        if current_path == "此电脑":
            return  # "此电脑"已是最顶层

        parent_dir = QDir(current_path)
        if parent_dir.cdUp():  # 能往上走就进入父目录
            self.load_directory(parent_dir.absolutePath())
        else:
            # 已到盘符根目录（如 C:\）→ 回到"此电脑"
            if platform.system() == "Windows":
                self.show_drives()

    def refresh(self):
        self.load_directory(self.address_bar.currentText())  # 重新加载当前目录

    def toggle_view_mode(self):
        """切换网格 / 列表。"""
        current_index = self.stacked_widget.currentIndex()
        nxt = 1 - current_index
        self.stacked_widget.setCurrentIndex(nxt)
        if nxt == 1:
            self.view_mode_btn.setText("网格")
            self.view_mode_btn.setChecked(False)
            self.view_mode_btn.setToolTip("当前：列表视图（点击切到网格）")
        else:
            self.view_mode_btn.setText("列表")
            self.view_mode_btn.setChecked(True)
            self.view_mode_btn.setToolTip("当前：网格视图（点击切到列表）")
        self.load_directory(self.address_bar.currentText())

    def _prompt_path_jump(self) -> None:
        cur = self.address_bar.currentText() or ""
        text, ok = QInputDialog.getText(self, "跳转路径", "完整路径：", text=cur)
        if ok and str(text).strip():
            self.load_directory(str(text).strip())

    def _clear_crumb_layout(self) -> None:
        while self._crumb_layout.count():
            item = self._crumb_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _rebuild_breadcrumb(self, path) -> None:
        """压缩路径：桌面 › 内容分类；点击段可回跳。"""
        self._clear_crumb_layout()
        if not path or path == "此电脑":
            lab = QLabel("此电脑")
            lab.setObjectName("crumbSep")
            self._crumb_layout.addWidget(lab)
            self._crumb_layout.addStretch(1)
            return

        try:
            p = Path(path).resolve()
        except OSError:
            p = Path(path)

        desktop = get_desktop_path()
        try:
            desktop = desktop.resolve()
        except OSError:
            pass

        segments = []  # (label, target_path_or_None)
        try:
            rel = p.relative_to(desktop)
            segments.append(("桌面", str(desktop)))
            acc = desktop
            for part in rel.parts:
                if part in (".",):
                    continue
                acc = acc / part
                segments.append((part, str(acc)))
        except ValueError:
            parts = p.parts
            if not parts:
                segments.append((str(p), str(p)))
            else:
                # Windows: ('C:\\', 'Users', ...)
                acc = Path(parts[0])
                label0 = parts[0].rstrip("\\/") or parts[0]
                segments.append((label0, str(acc)))
                for part in parts[1:]:
                    acc = acc / part
                    segments.append((part, str(acc)))

        # 过长时只留首尾几段
        if len(segments) > 5:
            head = segments[:2]
            tail = segments[-3:]
            segments = head + [("…", None)] + tail

        for i, (label, target) in enumerate(segments):
            if i > 0:
                sep = QLabel("›")
                sep.setObjectName("crumbSep")
                self._crumb_layout.addWidget(sep)
            if target is None:
                dots = QLabel(label)
                dots.setObjectName("crumbSep")
                self._crumb_layout.addWidget(dots)
                continue
            btn = QToolButton()
            btn.setObjectName("crumbBtn")
            btn.setText(label)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(target)
            btn.setAutoRaise(True)
            btn.clicked.connect(
                lambda _checked=False, t=target: self.load_directory(t)
            )
            self._crumb_layout.addWidget(btn)
        self._crumb_layout.addStretch(1)

    def add_to_history(self, path):
        """添加路径到浏览历史（后退后去新地方会截断后面的历史）"""
        if self.history_index < len(self.history) - 1:
            self.history = self.history[:self.history_index + 1]  # 截掉"前进方向"的旧记录
        self.history.append(path)
        self.history_index = len(self.history) - 1  # 指针指向最新一条

    # -------------------------------------------------------------------------
    # 交互事件处理方法
    # -------------------------------------------------------------------------

    def on_file_double_click(self, index):
        """双击：文件夹进入，文件用系统默认程序打开"""
        item = self.model.itemFromIndex(index)  # 索引 → 数据项
        if not item:
            return

        full_path = item.data(Qt.ItemDataRole.UserRole + 1)  # 取完整路径
        if not full_path:
            return

        file_info = QFileInfo(full_path)
        if file_info.isDir():
            self.load_directory(full_path)  # 文件夹：进入
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(full_path))  # 文件：交给系统默认程序打开

    def _emit_attach_to_chat(self, selected_items):
        """只附加文件（目录忽略）；主窗口负责芯片与 file_paths。"""
        paths = []
        for item in selected_items or []:
            full_path = item.data(Qt.ItemDataRole.UserRole + 1)
            if not full_path:
                continue
            info = QFileInfo(str(full_path))
            if info.isFile():
                paths.append(info.absoluteFilePath())
        if paths:
            self.attach_to_chat_requested.emit(paths)

    def show_context_menu(self, pos):
        """右键菜单：有选中项和点空白处显示不同内容"""
        menu = QMenu(self)

        # 收集当前视图中的选中项
        selected_items = []
        if self.stacked_widget.currentIndex() == 0:
            for index in self.list_view.selectedIndexes():
                item = self.model.itemFromIndex(index)
                if item:
                    selected_items.append(item)
        else:
            # 列表模式一行 4 列，只取第 0 列避免同一个文件被收集 4 次
            for index in self.tree_view.selectedIndexes():
                if index.column() == 0:
                    item = self.model.itemFromIndex(index)
                    if item:
                        selected_items.append(item)

        if selected_items:
            open_action = menu.addAction("打开")
            open_action.triggered.connect(lambda: self.open_selected(selected_items))

            attach_action = menu.addAction("附加到当前对话")
            attach_action.triggered.connect(
                lambda: self._emit_attach_to_chat(selected_items))

            if len(selected_items) == 1:
                rename_action = menu.addAction("重命名")  # 重命名只对单个选中项开放
                rename_action.triggered.connect(lambda: self.rename_selected(selected_items[0]))

            delete_action = menu.addAction("删除")
            delete_action.triggered.connect(lambda: self.delete_selected(selected_items))

            menu.addSeparator()
            menu.addAction("复制")
            menu.addAction("剪切")
            menu.addAction("粘贴")
            menu.addSeparator()

            properties_action = menu.addAction("属性")
            properties_action.triggered.connect(lambda: self.show_properties(selected_items[0]))
        else:
            # 点在空白处：目录级操作
            new_folder_action = menu.addAction("新建文件夹")
            new_folder_action.triggered.connect(self.create_new_folder)
            menu.addSeparator()
            menu.addAction("粘贴")
            menu.addSeparator()
            select_all_action = menu.addAction("全选")
            select_all_action.triggered.connect(self.select_all)

        # 在对应视图的鼠标位置弹出菜单（需把视图内坐标转成全局坐标）
        if self.stacked_widget.currentIndex() == 0:
            menu.exec(self.list_view.mapToGlobal(pos))
        else:
            menu.exec(self.tree_view.mapToGlobal(pos))

    def open_selected(self, items):
        """打开所有选中项：文件夹进入，文件用系统默认程序打开"""
        for item in items:
            full_path = item.data(Qt.ItemDataRole.UserRole + 1)
            if full_path:
                file_info = QFileInfo(full_path)
                if file_info.isDir():
                    self.load_directory(full_path)
                else:
                    QDesktopServices.openUrl(QUrl.fromLocalFile(full_path))

    def rename_selected(self, item):
        """重命名选中的文件/文件夹"""
        old_name = item.text()  # 当前显示名
        new_name, ok = QInputDialog.getText(self, "重命名", "请输入新名称:", text=old_name)  # 预填旧名

        if ok and new_name and new_name != old_name:  # 确认、非空、且确实改了名
            full_path = item.data(Qt.ItemDataRole.UserRole + 1)
            if full_path:
                dir_path = QFileInfo(full_path).absolutePath()  # 取所在目录
                new_path = os.path.join(dir_path, new_name)  # 拼出新完整路径
                try:
                    os.rename(full_path, new_path)  # 执行重命名
                    self.refresh()  # 刷新显示
                except Exception as e:
                    QMessageBox.warning(self, "错误", f"重命名失败: {str(e)}")

    def delete_selected(self, items):
        """删除选中的文件/文件夹（先弹确认框）"""
        reply = QMessageBox.question(
            self, "确认删除",
            f"确定要删除选中的 {len(items)} 个项目吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:  # 用户确认后才执行
            for item in items:
                full_path = item.data(Qt.ItemDataRole.UserRole + 1)
                if full_path:
                    try:
                        file_info = QFileInfo(full_path)
                        if file_info.isDir():
                            os.rmdir(full_path)  # 删除空文件夹（非空会抛异常）
                        else:
                            os.remove(full_path)  # 删除文件
                    except Exception as e:
                        QMessageBox.warning(self, "错误", f"删除失败: {str(e)}")  # 单个失败不影响其余
            self.refresh()

    def create_new_folder(self):
        """在当前目录新建文件夹"""
        current_path = self.address_bar.currentText()
        if current_path == "此电脑":
            QMessageBox.warning(self, "错误", "无法在此电脑下创建文件夹")
            return

        folder_name, ok = QInputDialog.getText(self, "新建文件夹", "文件夹名称:")
        if ok and folder_name:
            new_path = os.path.join(current_path, folder_name)  # 拼出目标路径
            try:
                os.makedirs(new_path, exist_ok=True)  # exist_ok=True：已存在时不报错
                self.refresh()
            except Exception as e:
                QMessageBox.warning(self, "错误", f"创建文件夹失败: {str(e)}")

    def select_all(self):
        """全选当前视图中的所有条目"""
        if self.stacked_widget.currentIndex() == 0:
            self.list_view.selectAll()
        else:
            self.tree_view.selectAll()

    def show_properties(self, item):
        """弹出属性对话框，显示选中项的基本信息"""
        full_path = item.data(Qt.ItemDataRole.UserRole + 1)
        if full_path:
            file_info = QFileInfo(full_path)
            info_text = f"名称: {file_info.fileName()}\n"
            info_text += f"路径: {file_info.absolutePath()}\n"
            info_text += f"大小: {self.format_size(file_info.size())}\n"
            info_text += f"类型: {'文件夹' if file_info.isDir() else file_info.suffix() + ' 文件'}\n"
            info_text += f"修改时间: {file_info.lastModified().toString('yyyy-MM-dd hh:mm:ss')}\n"
            info_text += f"创建时间: {file_info.birthTime().toString('yyyy-MM-dd hh:mm:ss')}"
            QMessageBox.information(self, "属性", info_text)
