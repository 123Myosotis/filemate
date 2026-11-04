"""按当前模型窗口决定「什么时候才该切」。

未到 context * fill_ratio 不切。输出 token 与提示模板从这份额度里扣掉，
避免输入本身还没到八成、加上回答却顶满窗口。

大小文件互相关联时：预算只决定「一次能让模型看见多少」；
真正的行仍在本地全量执行，不把几 MB / 十几 MB 的表整表灌进提示词。
"""
import re

import config

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def estimate_tokens(text: str) -> int:
    """CJK 约 1 字 1 token，其余约 4 字符 1 token。"""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    return cjk + (len(text) - cjk) // 4 + 1


def resolve_context_tokens(model: str = None) -> int:
    """窗口长度：环境式配置优先，其次模型对照表（最长前缀），最后兜底。"""
    name = (model if model is not None else config.get_param("api.model")) or ""
    name = str(name).strip()
    table = config.get_param("llm.context_by_model") or {}
    if isinstance(table, dict) and name in table:
        return int(table[name])
    if isinstance(table, dict) and name:
        best = ""
        for key in table:
            if name.startswith(str(key)) and len(str(key)) > len(best):
                best = str(key)
        if best:
            return int(table[best])
    return int(config.get_param("llm.context_tokens"))


def input_token_budget(reserved_output: int = None, overhead: int = None,
                       model: str = None) -> int:
    """单次请求允许的输入 token。合计占用不超过窗口 * fill_ratio。"""
    ctx = resolve_context_tokens(model)
    ratio = float(config.get_param("llm.context_fill_ratio"))
    out = int(config.get_param("api.max_tokens") if reserved_output is None
              else reserved_output)
    oh = int(config.get_param("llm.prompt_overhead_tokens") if overhead is None
             else overhead)
    budget = int(ctx * ratio) - out - oh
    floor = int(config.get_param("llm.min_input_tokens"))
    # 窗口极小时仍要留下输出位
    cap = max(1, ctx - out)
    return max(floor, min(budget, cap)) if budget > 0 else max(1, min(floor, cap))


def chars_for_tokens(tokens: int) -> int:
    per = float(config.get_param("llm.chars_per_token") or 1)
    return max(1, int(tokens * per))


def effective_input_chars(explicit_key: str = None, reserved_output: int = None,
                          model: str = None) -> int:
    """字符预算。仅当用户在 params 里显式写了 explicit_key 时才再取更小值。"""
    chars = chars_for_tokens(input_token_budget(reserved_output, model=model))
    if explicit_key and config.param_is_user_set(explicit_key):
        return max(1, min(chars, int(config.get_param(explicit_key))))
    return chars
