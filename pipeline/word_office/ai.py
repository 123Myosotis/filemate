# -*- coding: utf-8 -*-
"""L1 AI 调用：只出「建议 / 纯文本替换」，**禁止结构操作**。"""
from __future__ import annotations

from ai.client import chat_json

_SYSTEM = """你是 Word 文档的轻量编辑助手。只输出一个 JSON 对象，不要任何解释。
你只能做「纯文本层面的修改」：替换 / 润色 / 校对 / 标记；禁止改变文档结构（不加标题、不加段落、不加表格、不改样式）。
输出格式：{"summary": "一句话说明你做了什么", "ops": [{"find": "原文片段", "replace": "替换后", "reason": "为什么"}]}
规则：
- find 必须是文档里原样出现的片段；不要发明。
- 如果用户只要摘要/建议，ops 返回空数组，summary 给结论。
- 最多 20 个 op。"""


def call_l1(user_input: str, portrait: str, api_key: str) -> dict | None:
    prompt = (
        "【用户指令】%s\n\n【文档画像】\n%s\n\n请输出 JSON。"
        % (user_input, portrait)
    )
    return chat_json(prompt, api_key, system=_SYSTEM, max_tokens=1200, temperature=0.2)
