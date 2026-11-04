# ui/style.py
"""FileMate 简洁圆角主题：浅灰底、墨色字、少装饰。"""

APP_QSS = """
QWidget {
    font-family: "Segoe UI", "Microsoft YaHei UI", "PingFang SC", sans-serif;
    font-size: 13px;
    color: #1c1917;
    background: #f4f4f5;
}
QMainWindow, QDialog {
    background: #f4f4f5;
}
QMenuBar {
    background: #ffffff;
    border-bottom: 1px solid #e4e4e7;
    padding: 2px 8px;
}
QMenuBar::item {
    background: transparent;
    padding: 6px 12px;
    border-radius: 8px;
}
QMenuBar::item:selected {
    background: #f4f4f5;
}
QMenu {
    background: #ffffff;
    border: 1px solid #e4e4e7;
    border-radius: 12px;
    padding: 6px;
}
QMenu::item {
    padding: 8px 18px;
    border-radius: 8px;
}
QMenu::item:selected {
    background: #facc15;
    color: #18181b;
}
QTabWidget::pane {
    border: 1px solid #e4e4e7;
    border-radius: 14px;
    background: #ffffff;
    top: -1px;
}
QTabBar {
    qproperty-drawBase: 0;
}
QTabBar::tab {
    background: transparent;
    color: #71717a;
    border: none;
    border-radius: 10px;
    padding: 6px 8px 6px 10px;
    margin-right: 4px;
    min-width: 88px;
    max-width: 148px;
}
QToolButton#tabClose {
    background: transparent;
    color: #52525b;
    border: none;
    border-radius: 8px;
    padding: 0;
    margin: 0;
    font-size: 13px;
    font-weight: 700;
    min-width: 16px;
    max-width: 16px;
    min-height: 16px;
    max-height: 16px;
}
QToolButton#tabClose:hover {
    background: #facc15;
    color: #18181b;
}
QTabBar::tab:selected {
    background: #ffffff;
    color: #18181b;
    font-weight: 600;
    border: 1px solid #e4e4e7;
}
QTabBar::tab:hover:!selected {
    background: #ececef;
    color: #3f3f46;
}
QTabBar::close-button {
    subcontrol-origin: padding;
    subcontrol-position: right;
    width: 14px;
    height: 14px;
    margin: 0 6px 0 0;
    border-radius: 7px;
    background: transparent;
    padding: 0;
}
QTabBar::close-button:hover {
    background: #e4e4e7;
}
QTextEdit, QLineEdit, QListWidget, QTreeWidget, QListView, QTreeView, QComboBox {
    background: #ffffff;
    border: 1px solid #e4e4e7;
    border-radius: 12px;
    padding: 8px 12px;
    selection-background-color: #facc15;
    selection-color: #18181b;
}
QTextEdit {
    selection-background-color: #fde047;
    selection-color: #09090b;
}
QTextEdit:focus, QLineEdit:focus, QComboBox:focus {
    border: 1px solid #52525b;
}
QPushButton {
    background: #18181b;
    color: #fafafa;
    border: none;
    border-radius: 12px;
    padding: 8px 16px;
    font-weight: 600;
}
QPushButton:hover {
    background: #27272a;
}
QPushButton:pressed {
    background: #09090b;
}
QPushButton:disabled {
    background: #d4d4d8;
    color: #fafafa;
}
QPushButton#btnEnd {
    background: #eab308;
    color: #18181b;
}
QPushButton#btnEnd:hover {
    background: #ca8a04;
    color: #ffffff;
}
QPushButton#btnResume {
    background: #16a34a;
    color: #ffffff;
}
QPushButton#btnResume:hover {
    background: #15803d;
}
QToolBar {
    background: #ffffff;
    border: 1px solid #e4e4e7;
    border-radius: 12px;
    spacing: 6px;
    padding: 4px 8px;
    margin: 0 0 6px 0;
}
QToolButton, QToolBar QToolButton {
    background: transparent;
    border: none;
    border-radius: 8px;
    padding: 6px;
}
QToolButton:hover, QToolBar QToolButton:hover {
    background: #f4f4f5;
}
QSplitter::handle {
    background: #e4e4e7;
    border-radius: 2px;
}
QSplitter::handle:hover {
    background: #a1a1aa;
}
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 4px;
}
QScrollBar::handle:vertical {
    background: #d4d4d8;
    border-radius: 5px;
    min-height: 28px;
}
QScrollBar::handle:vertical:hover {
    background: #a1a1aa;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar:horizontal {
    background: transparent;
    height: 10px;
    margin: 4px;
}
QScrollBar::handle:horizontal {
    background: #d4d4d8;
    border-radius: 5px;
    min-width: 28px;
}
QLabel#scopeLabel {
    background: #ffffff;
    color: #52525b;
    border: 1px solid #e4e4e7;
    border-radius: 10px;
    padding: 8px 12px;
}
QStatusBar, QLabel#statusBar {
    background: #ffffff;
    color: #71717a;
    border-top: 1px solid #e4e4e7;
}
QMessageBox, QInputDialog {
    background: #ffffff;
}
QListWidget {
    padding: 4px;
}
QListWidget::item {
    border-radius: 8px;
    padding: 6px 8px;
}
QListWidget::item:hover, QListView::item:hover, QTreeView::item:hover {
    background: #fef9c3;
    color: #18181b;
}
QListWidget::item:selected, QListView::item:selected, QTreeView::item:selected {
    background: #facc15;
    color: #18181b;
}
"""


def apply_app_style(app_or_widget):
    """给 QApplication 或顶层窗口套用主题。选中色走调色板，避免富文本选区发灰。"""
    from PyQt6.QtGui import QColor, QPalette
    app_or_widget.setStyleSheet(APP_QSS)
    pal = app_or_widget.palette()
    pal.setColor(QPalette.ColorRole.Highlight, QColor("#facc15"))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#18181b"))
    app_or_widget.setPalette(pal)
