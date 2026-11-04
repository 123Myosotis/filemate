# pipeline/stage2_classifier/doc_classifier.py
"""
Stage2 路由层：6 类文档判别（technical/novel/guide/project/contract/academic）。
纯规则特征评分，零成本、可解释：每类一组特征正则，按每千字命中密度打分，
得分最高者为文档类型；全零时兜底 technical（结构化文档的通用策略）。
判别结果由调用方按内容 hash 写入 infra.cache_db.doc_type_cache。
"""
import re

# 6 类文档的特征正则（命中一次计 1 分；权重写在正则后的元组里，缺省 1）
FEATURES = {
    "technical": [
        (r"```", 3.0),                      # 代码围栏是最强信号
        (r"\bdef\s+\w+", 2.0),
        (r"\bclass\s+\w+", 2.0),
        (r"\bimport\s+\w+", 1.5),
        (r"\bfunction\b", 1.0),
        (r"(接口|端点|API)", 1.0),
        (r"(配置|部署|环境变量)", 1.0),
        (r"(数据库|服务器|缓存)", 0.8),
    ],
    "novel": [
        (r"「[^」]{2,30}」", 2.0),           # 直角引号对话
        (r"“[^”]{2,30}”", 2.0),             # 弯引号对话
        (r"(从前|有一天|话说)", 1.5),
        (r"(说道|喊道|心想|低声说)", 1.5),
        (r"(只见|忽然|突然)", 0.8),
    ],
    "guide": [
        (r"步骤\s*\d+", 2.0),
        (r"第[一二三四五六七八九十\d]+步", 2.0),
        (r"(首先|其次|然后|接着|最后)", 0.8),
        (r"(点击|打开|选择|勾选|输入)", 1.0),
        (r"注意[:：]", 1.0),
        (r"(教程|指南|手册)", 1.5),
    ],
    "project": [
        (r"(任务|待办)", 1.0),
        (r"里程碑", 2.0),
        (r"(截止|deadline)", 1.5),
        (r"(交付|验收|排期|迭代)", 1.0),
        (r"(需求|范围|风险)", 0.8),
    ],
    "contract": [
        (r"第[一二三四五六七八九十\d]+条", 2.5),  # "第X条" 是合同条款的强信号
        (r"条款", 1.5),
        (r"(甲方|乙方|丙方)", 2.0),
        (r"违约", 1.5),
        (r"(协议|合同)", 1.0),
        (r"(签署|盖章|生效)", 1.0),
    ],
    "academic": [
        (r"摘要", 1.5),
        (r"参考文献", 2.5),
        (r"\[\d+\]", 1.5),                  # [1] 式文献引用
        (r"et al", 1.5),
        (r"(引言|绪论)", 1.5),
        (r"(实验|样本|对照)", 1.0),
        (r"(本文|本研究)", 1.0),
    ],
}

# 全零兜底类型；同时作为对外公开的合法类型清单
DOC_TYPES = list(FEATURES.keys())
DEFAULT_TYPE = "technical"

# 编译缓存：模块加载时编译一次，避免每次判别重复 compile
_COMPILED = {
    t: [(re.compile(p), w) for p, w in pats] for t, pats in FEATURES.items()
}


def classify(text: str) -> tuple:
    """
    判别文档类型。

    返回 (doc_type, scores)：
        doc_type  6 类之一
        scores    {"technical": 1.2, ...} 每千字得分，供 UI 透明展示
    """
    n = max(len(text), 1)
    scale = 1000.0 / n  # 归一化系数：按每千字密度计分，长短文档可比
    scores = {}
    for dtype, pats in _COMPILED.items():
        score = 0.0
        for pat, weight in pats:
            hits = len(pat.findall(text))
            if hits:
                score += hits * weight
        scores[dtype] = round(score * scale, 3)

    best = max(scores, key=scores.get)
    if scores[best] <= 0:  # 全零：没有任一特征命中 → 兜底
        return DEFAULT_TYPE, scores
    return best, scores
