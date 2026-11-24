# ui/chat_panel.py
"""早期独立对话自测（主窗口已内置对话）。保留入口，禁止模块导入时动文件/调模型。"""
from __future__ import annotations


def on_send_clicked(input_text: str, *, assistant=None) -> str:
    """可选自测回调：显式传入助手实例才处理。"""
    if assistant is None:
        raise RuntimeError("请传入 AIFileAssistant 实例；导入本模块不会创建助手。")
    return assistant.process_command(input_text)


if __name__ == "__main__":
    # 仅直接运行本文件时才触达桌面与模型
    from pathlib import Path

    from config import get_api_key
    from engine.executor import AIFileAssistant

    assistant = AIFileAssistant(
        api_key=get_api_key(),
        base_dir=Path.home() / "Desktop",
    )
    print(assistant.process_command("帮我把桌面上的PDF整理一下"))
