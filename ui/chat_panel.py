# ui/chat_panel.py
"""对话面板（早期独立测试版本）：当前主窗口已内置对话功能，本文件保留作简单自测用"""
from pathlib import Path  # 构造桌面路径用
from engine.executor import AIFileAssistant  # AI 文件助手，负责处理自然语言指令
from config import get_api_key  # 从本地 config.json 读取智谱 API Key

# 模块加载时即创建一个全局助手实例，默认操作范围为桌面目录
assistant = AIFileAssistant(
    api_key=get_api_key(),
    base_dir=Path.home() / "Desktop"
)
def on_send_clicked(input_text):
    """发送按钮回调：把用户输入交给助手处理"""
    response = assistant.process_command(input_text)
# 处理用户输入
# 以下为直接运行本文件时的自测代码：向助手发送一条示例指令并打印回复
response = assistant.process_command("帮我把桌面上的PDF整理一下")
print(response)
