# -*- coding: utf-8 -*-
"""Word 编辑模块（规格：`正式1.1.docx`）。

唯一对外接口：`WordModule.handle(request) -> dict`
返回：`{status, reason, message, plan_ref, audit_ref, user_actions[]}`

v1 起步：先做接入层占位（路由能走到这里）；L0/L1 在后续步骤实现。
"""


class WordModule:
    """Word 业务线入口。执行层独立，不注册 `capabilities.py`、不走 `task_runner` DAG。"""

    @staticmethod
    def handle(request: dict) -> dict:
        return {
            "status": "failed",
            "reason": "not_implemented",
            "message": "Word 编辑模块已接入路由（5 选 1），L0/L1 尚未实现（v1 开发中）。",
            "plan_ref": None,
            "audit_ref": None,
            "user_actions": [],
        }
