# engine/dispatcher.py
"""
AI 全权代理调度器（对话链路唯一入口）：
用户指令 → AI 解析为 mode(执行/咨询/闲聊) + actions[]（文件动作/内容动作，可多个）
        → 本地只做"白名单消毒 + 真实文件编译 + 安全执行"，不做任何意图决策。

  execute：意图解析（8 维）→ 任务拆解（DAG）→ task_runner 调度执行
           （依赖串行/独立并行/失败不阻塞/复核兜底），直接干不出方案
  consult：只基于真实目录生成预览，一个文件都不动
  chat：普通对话

安全：所有文件夹名/文件名由 AI 给出后必须过白名单（与真实目录取交集、单层目录名、
拒绝路径穿越）；AI 决策无法落地时带具体原因反馈给 AI 自我修正（轮数 router.correction_rounds）。
无 API Key 或 AI 连续不可用时明确提示，不再偷偷走任何本地规则。
"""
import re
import time
from datetime import datetime
from pathlib import Path

import config
from agent_core import intent_router
from agent_core.intent_router import sanitize_folder
from ai.client import ask_llm


class IntentDispatcher:
    """对话链路：AI 全权决策，本地安全执行"""

    def __init__(self, ai_assistant):
        self.ai = ai_assistant  # 兼容旧构造；实际 Key 统一从 config 读
        self._progress = None  # 可选进度回调，由 UI 线程注入

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def dispatch(self, user_input: str, target_dir, progress=None,
                 control=None, channel_id=None, resume=False,
                 file_paths=None, ask_history=None,
                 seed_binding_payload=None, confirmed: bool = False) -> dict:
        self._progress = progress
        self._control = control
        target_dir = Path(target_dir)
        if not target_dir.exists() or not target_dir.is_dir():
            return {"handler": "系统",
                    "message": "⚠️ 请先在最右边的文件管理器中进入一个具体目录，我才能执行操作。",
                    "operations": []}

        # 待确认的二次洗：用户回复"洗/不洗" → 直接处理那个文件，不走意图路由
        from agent_core import capabilities as _cap
        pending = _cap.read_pending_wash()
        if pending and self._is_wash_reply(user_input):
            return self._handle_pending_wash(user_input, pending)

        if not config.get_api_key():
            return {"handler": "AI代理",
                    "message": "🔑 当前是 AI 全权代理模式，需要智谱 API Key。\n"
                               "请到「设置 → 设置智谱 API Key」配置后再下达指令。",
                    "operations": []}

        selected = [str(p) for p in (file_paths or []) if str(p).strip()]
        if progress:
            progress("正在解析意图…")
        intent = intent_router.route(
            user_input, target_dir, selected_files=selected or None)
        if intent is None:
            return {"handler": "AI代理",
                    "message": "⚠️ AI 决策服务暂时不可用（网络失败或返回无法解析），请稍后重试或换个说法。",
                    "operations": []}

        tool = str(intent.get("tool") or "")
        # P1 跨表查询 Tool：仅信任 LLM 分类的 tool 字段（本地不做原文正则猜）
        if tool == "table_query":
            if progress:
                progress("正在为你进行跨表查询，请稍候…")
            return self._run_table_query_tool(
                user_input,
                target_dir,
                selected_paths=selected,
                ask_history=ask_history,
                progress=progress,
                seed_binding_payload=seed_binding_payload,
            )

        # Word 编辑业务线：唯一入口 WordModule.handle()（规格：接入层共享、执行层独立）
        if tool == "word_office":
            if progress:
                progress("正在处理 Word 文档…")
            return self._run_word_office(user_input, target_dir, selected,
                                         confirmed=confirmed)

        # 整理/统计/导出表格类指令：禁止提前落成闲聊，先锁目录再强制 content 动作
        wants_table = self._wants_table_synthesize(
            user_input, {"output_format": "", "constraints": []})
        if intent["mode"] == "chat" and not wants_table:
            return {"handler": "AI",
                    "message": ask_llm(user_input, config.get_api_key()),
                    "operations": []}
        if intent["mode"] == "chat" and wants_table:
            intent = self._ensure_content_action(intent, force=True)

        # 工作目录：先认用户原文里的真实路径，再认子目录名，避免扫整棵项目根
        path_hit = intent_router.infer_work_dir_from_path(user_input, target_dir)
        if path_hit is not None and path_hit.is_dir():
            work_dir = path_hit
            if progress:
                progress(f"工作目录按指令路径锁定到「{work_dir}」…")
        else:
            if not str(intent.get("subdir") or "").strip():
                inferred = intent_router.infer_subdir_from_user(target_dir, user_input)
                if inferred:
                    intent["subdir"] = inferred
                    if progress:
                        progress(f"工作目录按指令锁定到「{inferred}」…")
            work_dir = intent_router.resolve_work_dir(target_dir, intent["subdir"])
        if work_dir is None:
            # subdir 非法或不存在 → 反馈让 AI 修正一次
            intent = self._correct(user_input, target_dir,
                                   f"子文件夹「{intent['subdir']}」不存在或不合法，"
                                   f"请改用空字符串或真实存在的子文件夹")
            if intent is None:
                return self._unavailable()
            work_dir = intent_router.resolve_work_dir(target_dir, intent["subdir"])
            if work_dir is None:
                return {"handler": "AI代理",
                        "message": f"⚠️ 文件夹「{intent['subdir']}」不存在，请在右侧文件管理器进入正确目录后重试。",
                        "operations": []}

        locked_work_dir = work_dir  # 后续纠错不得再放大回项目根

        # 表格合成/导出类指令：强制 content 动作，避免被咨询/闲聊/空动作短路
        if wants_table:
            intent = self._ensure_content_action(intent, force=True)
            if intent.get("mode") in ("chat", "consult"):
                intent["mode"] = "execute"

        plan, feedback = self._compile(intent, work_dir)
        if feedback:
            locked_sub = str(intent.get("subdir") or "").strip()
            intent = self._correct(user_input, target_dir, feedback, old=intent)
            if intent is None:
                return self._unavailable()
            # 纠错轮常把 subdir 清空，不能因此退回整棵根目录
            if locked_sub and not str(intent.get("subdir") or "").strip():
                intent["subdir"] = locked_sub
            if self._wants_table_synthesize(user_input, {"output_format": "", "constraints": []}):
                intent = self._ensure_content_action(intent)
            work_dir = locked_work_dir or intent_router.resolve_work_dir(
                target_dir, intent["subdir"]) or work_dir
            plan, feedback = self._compile(intent, work_dir)
            if feedback and self._wants_table_synthesize(
                    user_input, {"output_format": "", "constraints": []}):
                # 最终兜底：强制整目录内容整理
                intent = self._ensure_content_action(intent, force=True)
                plan, feedback = self._compile(intent, work_dir)
            if feedback:
                if progress:
                    progress(f"编译未通过：{feedback}")
                return {"handler": "AI代理",
                        "message": f"⚠️ 这条指令我没能对上当前目录的文件：{feedback}",
                        "operations": []}

        if intent["mode"] == "consult" and not wants_table:
            return {"handler": "AI方案", "message": self._render_consult(intent, plan), "operations": []}
        if intent["mode"] == "consult" and wants_table:
            intent = self._ensure_content_action(intent, force=True)
            intent["mode"] = "execute"
            plan, feedback = self._compile(intent, work_dir)
            if feedback:
                if progress:
                    progress(f"咨询改执行仍失败：{feedback}")
                return {"handler": "AI代理",
                        "message": f"⚠️ 这条指令我没能对上当前目录的文件：{feedback}",
                        "operations": []}
        return self._execute(intent, plan, user_input, work_dir,
                             control=control, channel_id=channel_id, resume=resume)

    def _collect_query_file_paths(
        self, target_dir: Path, selected_paths: list | None
    ) -> list[str]:
        """Prefer selected xlsx/csv; else xlsx/csv under work dir (non-recursive top + one level)."""
        from pipeline.stage1_parser.file_parser import TABLE_EXTS

        out = []
        for raw in selected_paths or []:
            p = Path(str(raw))
            if p.is_file() and p.suffix.lower() in TABLE_EXTS:
                out.append(str(p.resolve()))
        if out:
            return list(dict.fromkeys(out))
        skip = set(config.get_param("router.skip_dirs") or [])
        found = []
        try:
            for p in sorted(target_dir.rglob("*")):
                if not p.is_file() or p.name.startswith(".") or p.name.startswith("~$"):
                    continue
                if any(part in skip for part in p.relative_to(target_dir).parts):
                    continue
                if p.suffix.lower() in TABLE_EXTS:
                    found.append(str(p.resolve()))
                if len(found) >= 12:
                    break
        except OSError:
            pass
        return found

    def _run_word_office(self, user_input: str, target_dir: Path, selected: list,
                         confirmed: bool = False) -> dict:
        """Word 编辑业务线：唯一入口 WordModule.handle()。执行层独立，不注册 capabilities。"""
        from pipeline.word_office import WordModule

        request = {
            "user_input": user_input,
            "target_dir": str(target_dir),
            "selected_files": list(selected or []),
            "confirmed": bool(confirmed),
        }
        result = WordModule.handle(request) or {}
        return {
            "handler": "WordOffice",
            "message": str(result.get("message") or ""),
            "operations": list(result.get("user_actions") or []),
            "status": str(result.get("status") or ""),
            "reason": str(result.get("reason") or ""),
            "patch_set": result.get("patch_set") or {},
        }

    def _run_table_query_tool(
        self,
        user_input: str,
        target_dir: Path,
        *,
        selected_paths: list | None = None,
        ask_history=None,
        progress=None,
        seed_binding_payload=None,
    ) -> dict:
        """Invoke shared P1 run_query Tool; return chat-friendly result dict."""
        from pipeline.block_cross_table.query_engine.run_query_tool import run_query

        paths = self._collect_query_file_paths(target_dir, selected_paths)
        if not paths:
            return {
                "handler": "AI代理",
                "message": "⚠️ 跨表查询需要 Excel/CSV。请在右侧选中相关表格，或进入包含表格的目录后再试。",
                "operations": [],
                "tool": "table_query",
            }
        if progress:
            progress("跨表查询：已锁定 %s 张表，调用 QP/QE…" % len(paths))
        try:
            result = run_query(
                utterance=user_input,
                file_paths=paths,
                ask_history=list(ask_history or []) or None,
                seed_binding_payload=seed_binding_payload,
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "handler": "AI代理",
                "message": "⚠️ 跨表查询失败：%s: %s" % (type(exc).__name__, exc),
                "operations": [],
                "tool": "table_query",
                "status": "failed",
            }

        status = str(result.get("status") or "")
        if status == "needs_user":
            five = result.get("five_part") or {}
            lines = [
                "❓ 跨表查询需要你确认（直接在输入框回复即可继续）：",
                "",
                "推测：%s" % (five.get("speculation") or ""),
                "依据：%s" % (five.get("evidence") or ""),
                "可能问题：%s" % (five.get("what_might_be_wrong") or ""),
                "下一步：%s" % (five.get("next_step") or ""),
                "请确认或纠正：%s" % (five.get("please_confirm_or_correct") or ""),
            ]
            return {
                "handler": "AI代理",
                "message": "\n".join(lines),
                "operations": [],
                "tool": "table_query",
                "status": "needs_user",
                "fail_locus": result.get("fail_locus"),
                "five_part": five,
                "ask_history": result.get("ask_history_echo") or list(ask_history or []),
                "file_paths": paths,
                "plan_summary": result.get("plan_summary"),
                "stages": result.get("stages"),
            }

        if status != "plan_ready":
            return {
                "handler": "AI代理",
                "message": "⚠️ 跨表查询未完成（%s）：%s"
                % (result.get("fail_locus") or status, result.get("error") or status),
                "operations": [],
                "tool": "table_query",
                "status": status,
                "fail_locus": result.get("fail_locus"),
                "stages": result.get("stages"),
                "plan_summary": result.get("plan_summary"),
                "file_paths": paths,
                "assoc_retry_log": result.get("assoc_retry_log"),
                "error": result.get("error"),
                "plan": result.get("plan"),
                "result_preview": result.get("result_preview"),
            }

        preview = result.get("result_preview") or {}
        cols = preview.get("columns") or []
        rows = preview.get("rows") or []
        total = int(preview.get("total_rows") or 0)
        export = result.get("export") or {}
        export_path = export.get("path") or ""
        lines = [
            "✅ 跨表查询完成：共 %s 行 × %s 列" % (total, len(cols)),
            "列：%s" % ("、".join(str(c) for c in cols[:20]) if cols else "（空）"),
        ]
        if rows:
            lines.append("前 %s 行预览：" % min(10, len(rows)))
            for i, row in enumerate(rows[:10], 1):
                if not isinstance(row, dict):
                    continue
                cells = " | ".join(
                    "%s=%s" % (c, row.get(c)) for c in cols[:6]
                )
                lines.append("  %s. %s" % (i, cells))
        if export_path:
            lines.append("结果表：%s" % export_path)
        elif export.get("download_url"):
            lines.append("下载：%s" % export.get("download_url"))
        return {
            "handler": "AI代理",
            "message": "\n".join(lines),
            "operations": [],
            "tool": "table_query",
            "status": "plan_ready",
            "result_preview": preview,
            "export": export,
            "plan": result.get("plan"),
            "plan_summary": result.get("plan_summary"),
            "stages": result.get("stages"),
            "file_paths": paths,
            "qr_verdict": result.get("qr_verdict"),
            "local_override": result.get("local_override"),
            "qr_checks": result.get("qr_checks"),
            "qr_fixture_verdict": result.get("qr_fixture_verdict"),
            "assoc_retry_log": result.get("assoc_retry_log"),
            "ask_history": result.get("ask_history_echo") or list(ask_history or []),
        }

    @staticmethod
    def _ensure_content_action(intent: dict, force: bool = False) -> dict:
        """保证 intent 含 content 动作（all=true），供表格合成走通。"""
        intent = dict(intent or {})
        actions = list(intent.get("actions") or [])
        has_content = any(isinstance(a, dict) and a.get("domain") == "content" for a in actions)
        if force:
            # 要的是新表，不能把括号里的类别词当成文件名去移动源文件
            actions = [a for a in actions if not (isinstance(a, dict) and a.get("domain") == "file")]
        if force or not has_content:
            actions = [a for a in actions if not (isinstance(a, dict) and a.get("domain") == "content")]
            actions.append({"domain": "content", "op": "report", "all": True, "target_files": []})
            intent["actions"] = actions
            if intent.get("mode") == "chat":
                intent["mode"] = "execute"
        return intent

    def _correct(self, user_input, target_dir, feedback, old=None):
        """带错误反馈让 AI 重新决策；修正次数由 router.correction_rounds 控制"""
        rounds = int(config.get_param("router.correction_rounds"))
        intent = old
        for _ in range(max(rounds, 0)):
            intent = intent_router.route(user_input, target_dir, feedback=feedback)
            if intent is None:
                return None
            _, new_feedback = self._compile(
                intent, intent_router.resolve_work_dir(target_dir, intent["subdir"]) or Path(target_dir))
            if not new_feedback:
                return intent
            feedback = new_feedback
        return intent  # 至少返回最后一次结果，调用方会再次校验

    def _unavailable(self):
        return {"handler": "AI代理",
                "message": "⚠️ AI 决策服务暂时不可用（网络失败或返回无法解析），请稍后重试或换个说法。",
                "operations": []}

    # ---- 待确认的二次洗（跨轮）----
    _WASH_YES = {"洗", "清洗", "洗吧", "要洗", "可以洗", "洗一下", "好的", "好", "要"}
    _WASH_NO = {"不洗", "不用", "不清洗", "不要", "算了", "不用了", "不"}
    _RECOVER = ("模型恢复", "让模型恢复", "恢复表头")

    def _is_wash_reply(self, text) -> bool:
        t = str(text or "").strip()
        if t in self._WASH_YES or t in self._WASH_NO:
            return True
        if any(w in t for w in self._RECOVER):
            return True
        if "表头" in t and len(t) <= 200:
            return True
        return False

    def _handle_pending_wash(self, user_input, pending) -> dict:
        import re

        from agent_core import capabilities
        from pipeline.stage1_parser import file_parser

        table = str(pending.get("table") or "")
        file_path = str(pending.get("file") or "")
        if not file_path or not table:
            capabilities.clear_pending_wash()
            return {"handler": "系统", "message": "⚠️ 待确认记录不完整，已清除。", "operations": []}
        text = str(user_input or "").strip()
        try:
            parsed = file_parser.parse_file(file_path)
        except Exception as exc:  # noqa: BLE001
            capabilities.clear_pending_wash()
            return {"handler": "系统", "message": "⚠️ 读取失败：%s" % exc, "operations": []}

        # 1) 不用 → 保留原表
        if text in self._WASH_NO:
            capabilities.clear_pending_wash()
            return {"handler": "系统", "message": "✅ 已保留原表。", "operations": []}

        # 2) 让模型恢复（只有用户明确要求才调 LLM）
        if any(w in text for w in self._RECOVER):
            api_key = config.get_api_key()
            if not api_key:
                return {"handler": "系统",
                        "message": "🔑 让模型恢复需要 API Key，请先在设置里配置。",
                        "operations": []}
            rec = capabilities.table_recover.recover(
                parsed["table"], api_key, user_hint=parsed.get("filename") or "")
            if rec.get("status") == "recovered":
                parsed["table"] = rec["table"]
                ctx = {"data": {"skip_wash": True}, "operations": [],
                       "api_key": api_key, "user_input": user_input}
                capabilities._finish_table(ctx, parsed)
                capabilities.clear_pending_wash()
                return {"handler": "系统", "message": "✅ 模型已恢复表头。", "operations": []}
            capabilities.clear_pending_wash()
            return {"handler": "系统", "message": "⚠️ 模型没能恢复表头，已保留原表。", "operations": []}

        # 3) 表头是 X、Y、Z → 直接赋名（用 normalize 结果，首行也当数据，不丢行）
        if "表头" in text:
            raw = text.split("表头", 1)[-1].lstrip("是：: ")
            names = [n.strip() for n in re.split(r"[、,，;；/]", raw) if n.strip()]
            base = capabilities.table_normalizer.normalize(parsed["table"])
            old_cols = list(base.get("columns") or [])
            if not names or len(names) != len(old_cols):
                return {"handler": "系统",
                        "message": "⚠️ 表头列数（%d）和表列数（%d）不一致，请重试。"
                        % (len(names), len(old_cols)),
                        "operations": []}
            parsed["table"] = capabilities._assign_columns(base, names)
            ctx = {"data": {"skip_wash": True}, "operations": [],
                   "api_key": config.get_api_key(), "user_input": user_input}
            capabilities._finish_table(ctx, parsed)
            capabilities.clear_pending_wash()
            return {"handler": "系统", "message": "✅ 已按你给的表头更新。", "operations": []}

        # 4) 洗/要 → 走几何洗（沿用旧逻辑，洗不动仍会保留原表）
        if text not in self._WASH_YES:
            capabilities.clear_pending_wash()
            return {"handler": "系统", "message": "✅ 已保留原表。", "operations": []}
        try:
            ctx = {"data": {"wash_confirm": {table: True}}, "operations": [],
                   "api_key": config.get_api_key(), "user_input": user_input}
            capabilities._finish_table(ctx, parsed)
        except Exception as exc:  # noqa: BLE001
            capabilities.clear_pending_wash()
            return {"handler": "系统", "message": "⚠️ 二次洗失败：%s" % exc, "operations": []}
        if ctx["data"].get("wash_rejected"):
            return {"handler": "系统",
                    "message": "⚠️ 二次洗没能改善这张表（成空/行数骤减/仍认不出），已保留原表。"
                               "你可以补充表头（如「表头是设备、负责人」）或回复「让模型恢复」。",
                    "operations": []}
        return {"handler": "系统", "message": "✅ 已按你的确认完成二次洗。", "operations": []}

    # ------------------------------------------------------------------
    # 编译：把 AI 的动作翻译为"文件级、可安全执行"的计划
    # ------------------------------------------------------------------

    def _compile(self, intent: dict, work_dir: Path):
        """
        返回 (plan, feedback)：
        plan = {"moves": [(src_path, dst_path)...], "content_targets": [文件名...]}
        feedback 非空表示决策无法落地及原因（供 AI 自我修正）。
        """
        if not intent["actions"]:
            return None, "没有解析出任何动作，请明确要移动哪些文件或整理哪些文件的内容"

        # 文件动作只看顶层文件（不移动已在子文件夹里的文件）；
        # 内容动作递归查找所有文件（包括子文件夹内的），但跳过项目垃圾目录
        files = [p for p in work_dir.iterdir() if p.is_file()]
        actual = {p.name: p for p in files}
        skip = set(config.get_param("router.skip_dirs") or [])
        all_files = [p for p in work_dir.rglob("*")
                     if p.is_file() and not p.name.startswith(".")
                     and not any(part in skip for part in p.relative_to(work_dir).parts)]
        moving = set()       # 已被前面的文件动作占用的文件名（不再参与后续动作）
        moves = []
        content_targets = None  # None=未出现内容动作；[]=显式空；非空=目标文件
        errors = []

        for action in intent["actions"]:
            if action["domain"] == "file":
                produced, err = self._compile_file(action, files, actual, moving)
                if err:
                    errors.append(err)
                for src_name, folder in produced:
                    moves.append((actual[src_name], work_dir / folder / src_name))
                    moving.add(src_name)
            else:
                targets, err = self._compile_content(action, all_files, work_dir)
                if err:
                    errors.append(err)
                elif targets is not None:
                    content_targets = targets if content_targets is None \
                        else sorted(set(content_targets) | set(targets))

        if not moves and content_targets is None:
            return None, "；".join(errors) or (
                "没在当前目录找到匹配的文件。"
                "请在右上角文件面板定位到目标文件夹后再试，"
                "或直接告诉我文件的完整路径。")
        if not moves and not content_targets:
            return None, ("当前目录没有可整理内容的文件（支持 TXT/MD/Word/PDF/CSV/XLSX）。"
                          "请在右上角面板切换到目标文件夹后再试。")
        return {"moves": moves, "content_targets": content_targets}, ""

    def _compile_file(self, action: dict, files: list, actual: dict, moving: set):
        """
        把单个文件动作编译为 [(文件名, 目标文件夹名)...]。
        决策完全来自 AI（直接点名或给筛选参数），这里只做机械匹配与消毒。
        """
        op, p = action["op"], action.get("params", {})
        out = []

        if op == "move":
            for item in p.get("moves") or []:
                if not isinstance(item, dict):
                    continue
                name, folder = str(item.get("file") or ""), sanitize_folder(item.get("folder"))
                if name in actual and name not in moving and folder:
                    out.append((name, folder))
            if not out:
                wanted = [str(i.get("file")) for i in (p.get("moves") or [])
                          if isinstance(i, dict) and i.get("file")]
                return [], ("文件移动清单中的文件不在当前目录：" + "、".join(wanted)
                            + "；请严格按清单中的真实文件名照抄")
            return out, ""

        if op == "organize_by_ext":
            ext_map = {}
            raw = p.get("ext_map") if isinstance(p.get("ext_map"), dict) else {}
            for ext, folder in raw.items():
                ext = str(ext).lower()
                if not ext.startswith("."):
                    ext = "." + ext
                folder = sanitize_folder(folder)
                if re.fullmatch(r"\.[a-z0-9]{1,8}", ext) and folder:
                    ext_map[ext] = folder
            if not ext_map:
                return [], "扩展名分类为空或格式不对（应为 {\".pdf\":\"文档\"} 形式）"
            for f in files:
                if f.name not in moving and f.suffix.lower() in ext_map:
                    out.append((f.name, ext_map[f.suffix.lower()]))
            return out, "" if out else "当前目录没有这些扩展名的文件"

        if op == "move_keyword":
            keyword = str(p.get("keyword") or "").strip()
            folder = sanitize_folder(p.get("folder"))
            if not keyword or not folder:
                return [], "缺少关键词 keyword 或目标文件夹 folder"
            for f in files:
                if f.name not in moving and keyword in f.name:
                    out.append((f.name, folder))
            return out, "" if out else f"没有文件名包含「{keyword}」的文件"

        if op == "move_recent":
            folder = sanitize_folder(p.get("folder"))
            try:
                days = int(p.get("days"))
            except (TypeError, ValueError):
                return [], "时间窗口 days 必须是整数天数"
            if not folder or days <= 0:
                return [], "时间窗口或目标文件夹无效"
            cutoff = time.time() - days * 86400
            for f in files:
                if f.name not in moving and f.stat().st_mtime >= cutoff:
                    out.append((f.name, folder))
            return out, "" if out else f"最近 {days} 天内没有修改过的文件"

        if op == "move_date":
            for f in files:
                if f.name in moving:
                    continue
                ym = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y/%m")
                out.append((f.name, ym))  # 年/月 两级目录由本地按文件真实 mtime 生成
            return out, "" if out else "当前目录没有可按日期归档的文件"

        return [], f"未实现的文件动作：{op}"

    def _compile_content(self, action: dict, files: list, work_dir: Path):
        """编译内容动作：返回 (目标相对路径列表, 错误)；all=true 时覆盖全部内容类文件。
        files 为递归扫描出的所有文件路径；匹配时同时支持文件名和相对路径。"""
        from pipeline.stage1_parser import file_parser
        supported = file_parser.TEXT_EXTS | file_parser.TABLE_EXTS
        content_files = [f for f in files if f.suffix.lower() in supported]

        def rel(f: Path) -> str:
            try:
                return str(f.relative_to(work_dir))
            except ValueError:
                return f.name

        if action.get("all"):
            targets = [rel(f) for f in content_files]
            return (targets if targets else []), "" if targets else "当前目录没有可整理内容的文件"

        wanted = action.get("target_files") or []
        if isinstance(wanted, str):
            wanted = [wanted]
        name_set = {f.name for f in content_files}
        rel_set = {rel(f) for f in content_files}
        names = [n for n in wanted if isinstance(n, str) and (n in rel_set or n in name_set)]
        if not names:
            return None, ("内容动作点名的文件不存在或类型不支持："
                          + "、".join(str(n) for n in wanted)
                          + "（须为 TXT/MD/Word/PDF/CSV/XLSX，且文件名与清单一致）")
        return names, ""

    # ------------------------------------------------------------------
    # 咨询模式：只渲染预览
    # ------------------------------------------------------------------

    def _render_consult(self, intent: dict, plan: dict) -> str:
        lines = ["📋 方案预览（AI 咨询模式，未执行任何操作）", ""]
        if intent["plan"].strip():
            lines += [intent["plan"].strip(), ""]
        if plan["moves"]:
            lines.append(f"预计移动 {len(plan['moves'])} 个文件：")
            lines.extend(f"· {src.name} → {dst.parent.name.replace(chr(92), '/')}/"
                         for src, dst in plan["moves"][:20])
            lines.append("")
        if plan["content_targets"] is not None:
            lines.append(f"预计整理 {len(plan['content_targets'])} 个文件的内容：")
            lines.extend(f"· {n}" for n in plan["content_targets"][:20])
            lines.append("")
        lines.append("确认后直接说「执行」，或改成执行语气（如：把这些文档整理了并生成报告）。")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 执行模式：意图解析（8 维）→ 任务拆解（DAG）→ 调度执行（并行/失败不阻塞/复核兜底）
    # ------------------------------------------------------------------

    def _execute(self, intent: dict, plan: dict, user_input: str, work_dir: Path,
                 control=None, channel_id=None, resume=False) -> dict:
        from agent_core import task_planner, task_runner
        from pipeline.stage1_parser.file_parser import TEXT_EXTS, TABLE_EXTS

        # 递归列出所有文件（含子文件夹），用相对路径标识，与 cap_scan 保持一致
        # 跳过项目垃圾目录（.git/.venv 等），避免无关文件干扰 AI 规划
        skip = set(config.get_param("router.skip_dirs") or [])
        file_names = sorted(str(p.relative_to(work_dir))
                            for p in work_dir.rglob("*")
                            if p.is_file() and not p.name.startswith(".")
                            and not p.name.startswith("~$")
                            and not any(part in skip for part in p.relative_to(work_dir).parts))

        # 内容整理时，只把可解析的文档文件给规划层，避免代码/图片等无关文件干扰 AI
        if plan.get("content_targets") is not None:
            doc_exts = TEXT_EXTS | TABLE_EXTS
            file_names = [n for n in file_names
                          if Path(n).suffix.lower() in doc_exts]
        want_synth_early = self._wants_table_synthesize(
            user_input, {"output_format": "", "constraints": []})
        # 表格合成：跳过任务规划 AI（常失败且耗时长），本地直出 DAG
        if want_synth_early:
            if self._progress:
                self._progress("表格合成：跳过任务规划，直达扫描/解析/合成…")
            planned = {
                "intent": task_planner.normalize_intent({
                    "operation_type": "content_organize",
                    "execution_mode": "execute",
                    "file_scope": "全部表格文件",
                    "target_object": (user_input or "")[:120],
                    "filter_conditions": [],
                    "sort_requirement": {},
                    "output_format": "",
                    "constraints": [],
                }),
                "tasks": [],
            }
        else:
            table_hints = self._table_hints(work_dir)
            planned = task_planner.parse_and_plan(
                user_input, file_names,
                moves_summary=task_planner.moves_to_summary(plan["moves"]),
                table_hints=table_hints)
            if planned is None or not (planned.get("tasks") or []):
                detail = task_planner.LAST_ERROR or "AI 未返回可执行任务"
                return {"handler": "AI代理",
                        "message": f"⚠️ AI 任务规划失败：{detail}\n请稍后重试或换个说法。",
                        "operations": []}

        tasks = planned["tasks"]
        intent8 = planned["intent"]
        # 语义解析输出格式：用户说 word 就 word，说 excel 就 excel，绝不互相覆盖
        intent8["output_format"] = self._resolve_output_format(user_input, intent8)
        want_synth = self._wants_table_synthesize(user_input, intent8) or want_synth_early
        need_file = intent8["output_format"] in ("word", "excel", "xlsx", "csv")

        if want_synth:
            # 强制干净串行 DAG，避免 AI 把 synthesize 与 parse 并行或只 parse 单个文件
            keep = [t for t in tasks if t["capability"] in ("file_organize",)]
            tasks = keep + [
                {"task_id": "scan_auto", "task_name": "扫描目录",
                 "capability": "scan", "dependencies": [],
                 "parallel_possible": False, "input": {},
                 "expected_output": "文件清单"},
                {"task_id": "parse_auto", "task_name": "解析全部表格",
                 "capability": "parse", "dependencies": ["scan_auto"],
                 "parallel_possible": False, "input": {},  # 空 input=解析 scan 全部
                 "expected_output": "全部表格数据"},
                {"task_id": "synth_auto", "task_name": "动态合成表格",
                 "capability": "table_synthesize",
                 "dependencies": ["parse_auto"],
                 "parallel_possible": False, "input": {},
                 "expected_output": "自定义列合成表"},
            ]
            if need_file:
                tasks.append(
                    {"task_id": "export_auto", "task_name": "按格式导出",
                     "capability": "export", "dependencies": ["synth_auto"],
                     "parallel_possible": False, "input": {},
                     "expected_output": f"{intent8['output_format']} 文件"})
        else:
            if (intent8.get("operation_type") == "content_organize"
                    and not any(t["capability"] == "analyze" for t in tasks)
                    and not any(t["capability"] == "table_synthesize" for t in tasks)):
                parse_ids = [t["task_id"] for t in tasks
                             if t["capability"] in ("parse", "classify")]
                tasks.append({"task_id": "analyze_auto", "task_name": "全文分析",
                              "capability": "analyze",
                              "dependencies": parse_ids,
                              "parallel_possible": False, "input": {},
                              "expected_output": "全文摘要/要点/结论/建议"})

            # 只有用户明确要文件时才自动补 export
            if (need_file and intent8.get("operation_type") == "content_organize"
                    and not any(t["capability"] == "export" for t in tasks)):
                content_task_ids = [t["task_id"] for t in tasks
                                    if t["capability"] not in ("review", "file_organize")]
                tasks.append({"task_id": "export_auto", "task_name": "按格式导出",
                              "capability": "export",
                              "dependencies": content_task_ids,
                              "parallel_possible": False, "input": {},
                              "expected_output": f"{intent8['output_format']} 文件"})

        # 兜底3：AI 没排 review 时本地强制追加（依赖全部任务，软依赖不阻塞）
        tasks = [t for t in tasks if t["capability"] != "review"]  # 重建依赖更干净
        # 规范化依赖：去掉已删除任务的边
        valid = {t["task_id"] for t in tasks}
        for t in tasks:
            t["dependencies"] = [d for d in t.get("dependencies") or [] if d in valid]
        tasks.append({"task_id": "review_final", "task_name": "统一复核",
                      "capability": "review",
                      "dependencies": [t["task_id"] for t in tasks],
                      "parallel_possible": False, "input": {},
                      "expected_output": "复核结论 JSON"})

        def _report(msg):
            if self._progress:
                self._progress(str(msg))

        from agent_core.session_state import (
            TaskControl, load_checkpoint, load_tables_blob, clear_checkpoint)
        ctrl = control if control is not None else TaskControl()
        if getattr(ctrl, "status", "") == "idle":
            ctrl.reset()
        ctx = {"user_input": user_input, "work_dir": work_dir, "target_dir": work_dir,
               "api_key": config.get_api_key(), "intent": intent8,
               "moves": plan["moves"], "operations": [], "data": {}, "report": _report,
               "control": ctrl, "channel_id": channel_id}
        if resume:
            ck = load_checkpoint(work_dir, channel_id)
            blob = load_tables_blob(work_dir, channel_id)
            if ck and blob:
                _report(f"从检查点续跑：沿用已解析的 {len(blob)} 张表…")
                ctx["data"]["resume_hint"] = ck
                ctx["data"]["tables"] = blob
                ctx["data"]["files"] = list(ck.get("files") or [])
                ctx["data"]["resume_from_tables"] = True
            elif ck or blob:
                _report("检查点不完整，按当前目录重新扫描…")
            else:
                _report("检查点不可用，按当前目录重新开始…")
        _report("开始执行任务…")
        result = task_runner.run_tasks(tasks, ctx)
        if result.get("cancelled"):
            return {"handler": "AI代理",
                    "message": "⏹ 任务已由用户结束。可点「重新」从检查点继续，或重新发送指令。",
                    "operations": ctx["operations"], "cancelled": True,
                    "work_dir": str(work_dir)}
        if result.get("ok"):
            clear_checkpoint(work_dir, channel_id)
        return {"handler": "AI代理",
                "message": self._assemble(ctx, result, intent8),
                "operations": ctx["operations"],
                "work_dir": str(work_dir),
                "instruction_conflicts": list((ctx.get("data") or {}).get("instruction_conflicts") or []),
                "report_paths": list((ctx.get("data") or {}).get("report_paths") or [])}

    @staticmethod
    def _resolve_output_format(user_input: str, intent8: dict) -> str:
        """按用户语义解析输出格式；已明确的 intent 优先，绝不把 word 改成 excel。"""
        fmt = str(intent8.get("output_format") or "").lower().strip()
        if fmt == "xlsx":
            fmt = "excel"
        if fmt in ("word", "excel", "csv", "text", "ppt"):
            return fmt
        text = user_input or ""
        # 显式格式词（互不抢占：先匹配到的算）
        rules = [
            (r"(导出|生成|输出|保存).{0,6}(excel|xlsx)|\bexcel\b|\bxlsx\b", "excel"),
            (r"(导出|生成|输出|保存).{0,6}csv|\bcsv\b", "csv"),
            (r"(导出|生成|输出|保存).{0,6}(word|docx)|word\s*报告|\bdocx\b", "word"),
            (r"(导出|生成|输出|保存).{0,6}(ppt|pptx)|\bppt\b", "ppt"),
        ]
        for pat, val in rules:
            if re.search(pat, text, re.I):
                return val
        # 要「新表/列出行/表格文件」但没说格式 → excel
        if IntentDispatcher._wants_table_synthesize(text, intent8):
            if re.search(r"报告|word|文档", text, re.I) and not re.search(
                    r"excel|xlsx|csv|表格文件", text, re.I):
                return "word"
            return "excel"
        # 要报告但没说格式 → word
        if re.search(r"生成.{0,6}报告|整理成文档|输出报告", text):
            return "word"
        return "text"

    @staticmethod
    def _wants_table_synthesize(user_input: str, intent8: dict) -> bool:
        """是否需要动态合成新表（与最终落盘格式无关）。"""
        fmt = str(intent8.get("output_format") or "").lower()
        if fmt in ("excel", "xlsx", "csv"):
            return True
        text = user_input or ""
        signals = config.get_param_list("intent.table_export_signals")
        for s in signals:
            try:
                if re.search(s, text, re.I):
                    return True
            except re.error:
                if s.lower() in text.lower():
                    return True
        for c in intent8.get("constraints") or []:
            if re.search(r"excel|csv|表格|合成|列出行", str(c), re.I):
                return True
        return False

    @staticmethod
    def _table_hints(work_dir: Path) -> str:
        """读取工作目录内表格文件的首行表头（含 xlsx 各 sheet），防止 AI 臆造列名。"""
        hints = []
        skip = set(config.get_param("router.skip_dirs") or [])
        try:
            for p in sorted(work_dir.rglob("*")):
                if not p.is_file() or p.name.startswith("."):
                    continue
                if any(part in skip for part in p.relative_to(work_dir).parts):
                    continue
                suf = p.suffix.lower()
                rel = str(p.relative_to(work_dir))
                if suf in (".csv", ".tsv"):
                    try:
                        with open(p, "r", encoding="utf-8-sig", errors="replace") as f:
                            header = f.readline().strip()
                    except OSError:
                        continue
                    if header:
                        sep = "\t" if suf == ".tsv" else ","
                        hints.append(f"· {rel}：{'、'.join(header.split(sep))}")
                elif suf == ".xls":
                    try:
                        import xlrd
                        book = xlrd.open_workbook(str(p))
                        for idx in range(book.nsheets):
                            sh = book.sheet_by_index(idx)
                            if sh.nrows <= 0:
                                continue
                            cols = [str(sh.cell_value(0, c) or "") for c in range(sh.ncols)]
                            label = rel if book.nsheets == 1 else f"{rel}::{sh.name}"
                            hints.append(f"· {label}：{'、'.join(c for c in cols if c)}")
                    except Exception:
                        continue
                elif suf == ".xlsx":
                    try:
                        from openpyxl import load_workbook
                        wb = load_workbook(p, read_only=True, data_only=True)
                        for sheet in wb.sheetnames:
                            ws = wb[sheet]
                            row = next(ws.iter_rows(values_only=True), None)
                            if not row:
                                continue
                            cols = [str(c) if c is not None else "" for c in row]
                            label = rel if len(wb.sheetnames) == 1 else f"{rel}::{sheet}"
                            hints.append(f"· {label}：{'、'.join(c for c in cols if c)}")
                        wb.close()
                    except Exception:
                        continue
        except OSError:
            pass
        # 未显式收紧时，长度顶到当前模型窗口的填充比，而不是固定小段
        from ai.context_budget import effective_input_chars
        max_chars = effective_input_chars(
            explicit_key="planner.table_hints_chars",
            reserved_output=int(config.get_param("planner.max_tokens")),
        )
        text = "\n".join(hints)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n…（表头提示已到本批上下文预算）"
        return text

    def _assemble(self, ctx: dict, result: dict, intent8: dict) -> str:
        """把 DAG 执行结果装配成给用户看的最终消息（执行模式只给结果，不给方案）。
        按内容分段，段间空行，文字润色面向用户。"""
        from pipeline.stage3_processor import table_engine
        data = ctx["data"]
        blocks = []   # 每个 block 是一组相关行，块间用空行分隔

        # —— 查询结果 ——
        if intent8.get("operation_type") == "query":
            if data.get("answer_text"):
                blocks.append([data["answer_text"]])
            else:
                blocks.append(["🔎 知识库中没有检索到相关数据（先整理过文件才能查询）。"])

        # —— 文件整理去向 ——
        ops = ctx["operations"]
        moved = len(ops)
        if moved:
            b = [f"📦 文件整理（{moved} 个，撤销请到日志右键「撤回」）"]
            for op in ops[:30]:
                src, dst = Path(op["src"]).name, Path(op["dst"])
                b.append(f"  · {src}  →  {dst.parent.name}/")
            if moved > 30:
                b.append(f"  · ……（还有 {moved - 30} 个，详见日志）")
            blocks.append(b)

        # —— 内容整理摘要 ——
        unified_store = data.get("unified") or {}
        if unified_store:
            b = ["📝 内容整理"]
            for name, u in unified_store.items():
                info = u["document_info"]
                sc = u["structured_content"]
                status = "✅" if info["failed_chunks"] == 0 else "⚠️"
                b.append(f"  {status} {name}")
                b.append(f"     类型：{info['type']}　|　实体 {len(sc['global_entities'])}"
                         f"　|　事实 {len(sc['facts'])}　|　表格 {len(sc['tables'])}")
            blocks.append(b)

        # —— 全文分析摘要 ——
        analysis_store = data.get("analysis") or {}
        if analysis_store:
            b = ["🔍 全文分析"]
            for name, a in analysis_store.items():
                status = "✅" if a.get("status") == "ok" else "⚠️"
                b.append(f"  {status} {name}")
                if a.get("summary"):
                    b.append(f"     摘要：{a['summary'][:120]}{'…' if len(a['summary']) > 120 else ''}")
                if a.get("key_points"):
                    b.append(f"     要点 {len(a['key_points'])} 条 · 结论 {len(a['conclusions'])} 条"
                             f" · 建议 {len(a['recommendations'])} 条")
            blocks.append(b)

        # —— 表格查询结果 ——
        table = data.get("table_result")
        if table and table.get("rows"):
            head = f"🔎 数据结果（{len(table['rows'])} 行"
            if table.get("sorted_by"):
                head += f"，按 {table['sorted_by']}"
            blocks.append([head + "）：", table_engine.to_display(table, limit=10)])

        # —— 生成报告 ——
        reports = data.get("report_paths") or []
        if reports:
            b = ["📄 生成报告"]
            for p in reports:
                b.append(f"  · {p}")
            blocks.append(b)

        # —— 未完成任务 ——
        failed = list(result.get("failed") or [])
        if failed:
            b = [f"⚠️ {len(failed)} 个任务未完成"]
            for tid in failed[:8]:
                err = (result["results"].get(tid, {}).get("error") or "未知原因")[:60]
                b.append(f"  · {tid}：{err}")
            if len(failed) > 8:
                b.append(f"  · ……（还有 {len(failed) - 8} 个，详见日志）")
            blocks.append(b)

        # —— 复核发现的问题 ——
        review = result.get("review") or {}
        conflicts = list(data.get("instruction_conflicts") or [])
        sheet_notes = list(data.get("sheet_notes") or [])
        if sheet_notes:
            b = ["📌 表内说明（已从对齐键拆出，供核对）"]
            for note in sheet_notes[:8]:
                b.append(f"  · {note}")
            blocks.append(b)
        if conflicts:
            b = [f"⚠️ 检查完表格后发现 {len(conflicts)} 处冲突。表已经生成，请确认是否保留。"]
            for c in conflicts:
                if c.get("kind") == "data":
                    b.append(
                        f"  · 列「{c.get('column')}」同一对象有两个来源："
                        + "、".join(c.get("values") or [])
                        + f"，先写入的是「{c.get('applied')}」。"
                    )
                elif c.get("kind") == "keep_drop":
                    b.append("  · 同一对象既要保留又要去掉：" + "、".join(c.get("values") or []))
                elif c.get("resolved"):
                    b.append(
                        f"  · 列「{c.get('column')}」多句取值不一致（{'、'.join(c.get('values') or [])}），"
                        f"带先后词的一句是「{c.get('applied')}」。"
                    )
                else:
                    b.append(
                        f"  · 列「{c.get('column')}」多句取值不一致（{'、'.join(c.get('values') or [])}），"
                        "没有先后词，没有改写成其中某一句。"
                    )
            blocks.append(b)

        if review and not review.get("passed"):
            issues = review.get("issues") or []
            rounds_txt = f"（已重跑 {result['rounds']} 轮）" if result.get("rounds") else ""
            b = [f"⚠️ 复核发现 {len(issues)} 项问题{rounds_txt}"]
            for i in issues[:6]:
                sev = {"high": "严重", "medium": "中等", "low": "轻微"}.get(
                    i.get("severity", "low"), "轻微")
                b.append(f"  · [{i.get('dimension', '')}·{sev}] {i.get('description', '')}")
            blocks.append(b)

        # —— 表格结构识别兜底（五段式求证，仅"有数据但洗成空"时出现） ——
        needs_user = list(data.get("table_needs_user") or [])
        if needs_user:
            b = ["❓ 有表格识别不出表头，需要你确认（直接在输入框回复即可继续）："]
            for item in needs_user:
                five = item.get("five_part") or {}
                b.append(f"【{item.get('table', '')}】")
                b.append(f"  推测：{five.get('speculation', '')}")
                b.append(f"  依据：{five.get('evidence', '')}")
                b.append(f"  可能问题：{five.get('what_might_be_wrong', '')}")
                b.append(f"  下一步：{five.get('next_step', '')}")
                b.append(f"  请确认或纠正：{five.get('please_confirm_or_correct', '')}")
            b.append("  💡 直接补充表头信息再发一次即可重试（例如：整理这个表，表头是设备、负责人、状态）。")
            blocks.append(b)

        # —— 补充说明 ——
        notes = []
        for note in data.get("degrade_notes") or []:
            notes.append(f"  · {note}")
        for note in data.get("filter_notes") or []:
            notes.append(f"  · {note}")
        if not ctx.get("api_key"):
            notes.append("  · 未配置 API Key，内容理解部分走降级模式。")
        if notes:
            blocks.append(["💡 补充说明"] + notes)

        if not blocks:
            return "📂 当前目录没有需要改动的文件。如需整理，请在右上角定位到目标文件夹后再试。"

        # 块间用空行分隔
        return "\n\n".join("\n".join(b) for b in blocks)
