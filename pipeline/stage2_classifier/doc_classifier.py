# pipeline/stage2_classifier/doc_classifier.py
"""
Stage2 路由层：规则文档判别 + other。
纯规则特征评分，零成本、可解释。
CLASSIFIER_VERSION 变更时内容分类索引/软标签缓存必须失效重算。

改 FEATURES / 阻尼 / 置信 / 强规则时必须同时：
  1) CLASSIFIER_VERSION += 1
  2) 更新 RULES_DIGEST = compute_rules_digest()
单测会卡住「只改规则忘升版本」。
"""
from __future__ import annotations

import hashlib
import json
import re

# 规则版本：改 FEATURES/阻尼/置信/强规则时务必 +1，驱动索引 stale 重算
CLASSIFIER_VERSION = 3

# 硬编码指纹：必须等于 compute_rules_digest()；改规则后与 VERSION 一起更新
RULES_DIGEST = "929ded97d5bfd42d"

# 规则可打分类（命中一次计权重；密度 = 原始分 × 1000/(字数+500)）
FEATURES = {
    "technical": [
        (r"```", 3.0),
        (r"\b(def|class|import|function|return)\b", 1.5),
        (r"(接口|端点|API|字段|表结构)", 1.2),
        (r"(实现|模块|调用|参数|请求|响应|日志|版本|机制|架构)", 0.8),
        (r"(配置|部署|环境变量|依赖|编译|运行|报错|异常)", 0.8),
        (r"(数据库|服务器|缓存|测试用例)", 0.8),
        (r"(规格|技术方案|设计说明)", 1.0),
    ],
    "report": [
        (r"(实验目的|实验步骤|实验内容|实验环境|实验原理)", 2.0),
        (r"(结果分析|实验总结|实验心得)", 2.0),
        (r"(实训报告|实验报告|课程设计|大作业|课程名称)", 2.0),
        (r"(指导教师|学号|姓名|学院|专业)", 1.0),
    ],
    "academic": [
        (r"摘要", 1.5),
        (r"关键词", 1.5),
        (r"参考文献", 2.5),
        (r"\[\d+\]", 1.5),
        (r"et al", 1.5),
        (r"(引言|绪论|研究方法|结论)", 1.2),
        (r"(本文|本研究)", 1.0),
    ],
    "contract": [
        (r"第[一二三四五六七八九十\d]+条", 2.5),
        (r"(条款|违约|解除|争议解决|仲裁|诉讼)", 1.5),
        (r"(甲方|乙方|丙方|发包方|承包方|委托方|受托方)", 2.0),
        (r"(价款|报酬|工期|结算|验收|责任)", 1.2),
        (r"(协议|合同)", 1.0),
        (r"(签署|盖章|生效)", 1.0),
    ],
    "guide": [
        (r"步骤\s*\d+", 2.0),
        (r"第[一二三四五六七八九十\d]+步", 2.0),
        (r"(教程|指南|手册|使用方法|安装说明)", 1.5),
        (r"(点击|打开|选择|勾选|输入|注意[:：]|提示[:：])", 0.8),
        (r"(首先|其次|然后|接着|最后)", 0.3),
    ],
    "project": [
        (r"(里程碑|截止|deadline)", 1.5),
        (r"(任务|待办|交付|验收|排期|迭代)", 1.0),
        (r"(需求|范围|风险|周报|进度|负责人|完成情况)", 0.8),
    ],
    "novel": [
        (r"(说道|喊道|低声说|心想|只见|忽然|突然)", 1.5),
        (r"(沉默|嘴角|眸子|颤抖|盯着|笑了笑)", 1.2),
        (r"(第一章|第[一二三四五六七八九十\d]+章|楔子|尾声)", 1.5),
        (r"(从前|有一天|话说)", 1.0),
    ],
    "script": [
        (r"(场景|旁白|第一幕|第[一二三四五六七八九十\d]+幕)", 2.0),
        (r"(角色|台词|分镜|剧情|主角|反派)", 1.5),
    ],
}

RULE_TYPES = list(FEATURES.keys())
DOC_TYPES = RULE_TYPES + ["other"]
DEFAULT_TYPE = "other"

TYPE_LABELS = {
    "technical": "技术文档",
    "report": "报告",
    "novel": "小说叙事",
    "guide": "指南教程",
    "project": "项目事务",
    "contract": "合同协议",
    "academic": "学术论文",
    "script": "剧本",
    "other": "其他",
}

# 低置信：top1 < MIN_TOP1 或 margin < MIN_MARGIN；短文另加严
MIN_TOP1 = 1.0
MIN_MARGIN = 0.8
SHORT_CHARS = 500
SHORT_MARGIN = 1.0

_FILENAME_BOOSTS = (
    (re.compile(r"(合同|协议|承包|劳务)"), "contract", 3.0),
    (re.compile(r"(实验报告|大作业|课程设计)"), "report", 3.0),
    (re.compile(r"(剧本|分镜)"), "script", 3.0),
    (re.compile(r"(规格|设计|方案|说明)"), "technical", 1.5),
    (re.compile(r"(流程|指南|教程|手册)"), "guide", 1.5),
)

_COMPILED = {
    t: [(re.compile(p), w) for p, w in pats] for t, pats in FEATURES.items()
}

# 报告结构强规则：前 500 字标题类 + ≥2 结构词 → 强制 report
_REPORT_HEAD = re.compile(r"(实训报告|实验报告)")
_REPORT_STRUCT_PATS = [
    re.compile(p)
    for p in (
        r"实验目的", r"实验步骤", r"实验环境", r"结果分析",
        r"实验总结", r"指导教师", r"学号",
    )
]

_LABEL_TO_TYPE = {v: k for k, v in TYPE_LABELS.items()}


def label_of(doc_type: str) -> str:
    return TYPE_LABELS.get(doc_type, doc_type or "其他")


def type_of_label(label: str) -> str:
    return _LABEL_TO_TYPE.get(str(label or ""), "")


def force_report_by_structure(text: str) -> bool:
    """正文前 500 字：命中报告标题且结构词 ≥2 → True。"""
    head = (text or "")[:500]
    if not _REPORT_HEAD.search(head):
        return False
    hits = sum(1 for p in _REPORT_STRUCT_PATS if p.search(head))
    return hits >= 2


def _apply_filename_boost(scores: dict[str, float], filename: str) -> dict[str, float]:
    name = str(filename or "")
    if not name:
        return scores
    out = dict(scores)
    for pat, dtype, boost in _FILENAME_BOOSTS:
        if pat.search(name) and dtype in out:
            out[dtype] = round(float(out[dtype]) + float(boost), 3)
    return out


def _score_text(text: str, filename: str = "") -> dict[str, float]:
    n = len(text or "")
    scale = 1000.0 / (n + 500)  # 阻尼：短文不虚高
    scores = {}
    for dtype, pats in _COMPILED.items():
        score = 0.0
        for pat, weight in pats:
            hits = len(pat.findall(text or ""))
            if hits:
                score += hits * weight
        scores[dtype] = round(score * scale, 3)
    return _apply_filename_boost(scores, filename)


def classify(text: str, filename: str = "") -> tuple:
    """
    判别文档类型。

    返回 (doc_type, scores)：
        doc_type  DOC_TYPES 之一（全零/低置信 → other）
        scores    规则类密度分（不含 other）
    """
    decision = classify_decision(text, filename=filename)
    return decision["doc_type"], decision["scores"]


def classify_decision(text: str, filename: str = "") -> dict:
    """
    带回置信信息的判别。

    low_confidence：top1 < MIN_TOP1，或 top1−top2 < MIN_MARGIN；
    正文 < SHORT_CHARS 时还要求 margin ≥ SHORT_MARGIN。
    低置信时 doc_type = other。
    """
    n = len(text or "")
    scores = _score_text(text, filename=filename)
    ranked = sorted(scores.items(), key=lambda x: (-x[1], x[0]))
    top1_k, top1_s = ranked[0]
    top2_k, top2_s = ranked[1] if len(ranked) > 1 else ("", 0.0)
    margin = round(float(top1_s) - float(top2_s), 3)
    low = bool(top1_s < MIN_TOP1 or margin < MIN_MARGIN)
    if n < SHORT_CHARS and margin < SHORT_MARGIN:
        low = True
    doc_type = DEFAULT_TYPE if low else top1_k
    forced = False
    if force_report_by_structure(text):
        # 强制 report，但得分保留真实密度，不伪造险胜
        doc_type = "report"
        low = False
        forced = True
    return {
        "doc_type": doc_type,
        "scores": scores,
        "top1": top1_k,
        "top1_score": top1_s,
        "top2": top2_k,
        "top2_score": top2_s,
        "margin": margin,
        "low_confidence": low,
        "classifier_version": CLASSIFIER_VERSION,
        "char_count": n,
        "forced_report": forced,
    }


def compute_rules_digest() -> str:
    """规则面指纹（不含 CLASSIFIER_VERSION 本身）。改 FEATURES 等会变。"""
    payload = {
        "features": FEATURES,
        "min_top1": MIN_TOP1,
        "min_margin": MIN_MARGIN,
        "short_chars": SHORT_CHARS,
        "short_margin": SHORT_MARGIN,
        "filename_boosts": [
            (p.pattern, dtype, boost) for p, dtype, boost in _FILENAME_BOOSTS
        ],
        "report_head": _REPORT_HEAD.pattern,
        "report_struct": [p.pattern for p in _REPORT_STRUCT_PATS],
        "type_labels": TYPE_LABELS,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

