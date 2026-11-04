# ai/__init__.py
# 从 ai 包导入对话函数（兼容旧名 ask_deepseek）
from ai.client import ask_llm, ask_deepseek

# engine/__init__.py
# 对外导出规则引擎、AI 文件助手，以及桌面 PDF 扫描函数，方便直接 from engine import ...
from .executor import FileRuleEngine, AIFileAssistant
from .scanner import get_desktop_pdfs_content