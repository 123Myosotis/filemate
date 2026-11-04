# engine/scanner.py
"""桌面扫描工具：定位真实桌面路径，并提取桌面上 PDF 文件的文本摘要"""
from pathlib import Path  # 跨平台路径对象
import platform  # 操作系统判断（保留原有导入）


def get_desktop() -> Path:
    """获取真实桌面路径（处理 OneDrive 等重定向）"""
    try:
        import os, winreg  # 延迟导入：winreg 仅 Windows 可用，避免其他系统 import 时报错
        # 打开注册表「用户外壳文件夹」键，里面记录桌面等系统文件夹的真实位置
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        )
        value, _ = winreg.QueryValueEx(key, "Desktop")  # 读出 Desktop 的值（可能含 %USERPROFILE% 等环境变量）
        winreg.CloseKey(key)
        return Path(os.path.expandvars(value))  # 展开环境变量后转成 Path 返回
    except Exception:
        # 注册表读取失败（如非 Windows 环境）时走降级方案
        onedrive = Path.home() / "OneDrive" / "Desktop"  # 优先尝试 OneDrive 重定向后的桌面
        if onedrive.exists():
            return onedrive
        return Path.home() / "Desktop"  # 最终兜底：常规桌面路径


def get_desktop_pdfs_content() -> str:
    """扫描桌面PDF并提取前500字内容"""
    desktop = get_desktop()  # 先定位真实桌面路径
    pdf_files = list(desktop.glob("*.pdf"))  # 找出桌面上一层级的所有 .pdf 文件

    if not pdf_files:
        return "桌面上没有找到PDF文件"  # 没有 PDF 时直接返回提示文案

    result = []  # 每个 PDF 的一段摘要文本都会追加到这里
    for pdf in pdf_files:
        try:
            # 尝试用 PyPDF2 提取文本
            import PyPDF2  # 延迟导入：只有真正处理 PDF 时才需要该库
            with open(pdf, "rb") as f:  # 以二进制只读方式打开 PDF
                reader = PyPDF2.PdfReader(f)
                text = ""
                for i, page in enumerate(reader.pages):
                    if i >= 3:  # 只取前3页
                        break  # 超过 3 页就停止，避免大文件耗时过长
                    text += page.extract_text() or ""  # 某些页可能提取不到文本，用 or "" 防 None

                summary = text[:500] if text else "(无法提取文本)"  # 每个文件只保留前 500 字作为摘要
                result.append(f"【{pdf.name}】\n{summary}\n")
        except ImportError:
            result.append(f"【{pdf.name}】(需要安装 PyPDF2 才能提取内容)")  # 未安装 PyPDF2 时给出提示
        except Exception as e:
            result.append(f"【{pdf.name}】(读取失败: {str(e)})")  # 单个文件失败不影响其他文件

    return "\n".join(result)  # 把所有文件的摘要拼成一整段文本返回（供 AI 提示词使用）