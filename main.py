# main.py
"""FileMate 程序入口：创建 Qt 应用对象并显示主窗口"""
import os
import sys
import traceback

# 双击运行时工作目录可能不是项目根，统一切到本文件所在目录
_ROOT = os.path.dirname(os.path.abspath(__file__))
if os.getcwd() != _ROOT:
    os.chdir(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _fail(msg: str) -> None:
    """依赖缺失或启动失败时给出可见提示（避免双击后黑窗一闪而过）。"""
    text = msg.strip() + "\n\n完整报错：\n" + traceback.format_exc()
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(None, "FileMate 启动失败", text)
    except Exception:
        # PyQt 本身都没装时，写错误日志并尽量弹出系统对话框
        err_path = os.path.join(_ROOT, "启动失败.log")
        with open(err_path, "w", encoding="utf-8") as f:
            f.write(text)
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0,
                "缺少依赖，请先在项目目录执行：\n"
                "py -3 -m pip install -r 库列表.txt\n\n"
                f"详情已写入：{err_path}",
                "FileMate 启动失败",
                0x10,
            )
        except Exception:
            print(text, file=sys.stderr)
            input("按回车键退出…")
    sys.exit(1)


try:
    from PyQt6.QtWidgets import QApplication
    from ui.main_window import MainWindow
    from ui.style import apply_app_style
except Exception:
    _fail(
        "无法导入运行依赖。\n\n"
        "请在项目根目录打开终端，执行：\n"
        "  py -3 -m pip install -r 库列表.txt\n"
        "或：\n"
        "  python -m pip install -r 库列表.txt\n\n"
        "说明：双击 .py 默认走系统的 py 启动器（当前多为 Python 3.13），\n"
        "依赖必须装到该解释器上；另外 .py 若被关联到 PyCharm，请改用「启动FileMate.bat」。"
    )


if __name__ == "__main__":
    try:
        from pipeline.synth_contract.boot import (
            ContractLibraryError,
            ensure_contract_library,
        )
        ensure_contract_library()
    except ContractLibraryError as e:
        _fail("契约库不可用，无法启动。\n\n" + str(e))
    except Exception:
        _fail("契约库校验时出错。")
    try:
        app = QApplication(sys.argv)
        apply_app_style(app)
        window = MainWindow()
        window.show()
        sys.exit(app.exec())
    except Exception:
        _fail("主窗口启动时出错。")
