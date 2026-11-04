"""与 ai.context_budget.estimate_tokens 等价的可增量计数。

estimate_tokens(text) = cjk + (len-cjk)//4 + 1（空串为 0）。
对同一分隔规则拼起来的全文，只累计长度与 CJK 个数，结果与整段调用相等。
"""

import re

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


class TokenAccum:
    def __init__(self):
        self.n = 0
        self.cjk = 0
        self.nbytes = 0

    def copy(self):
        other = TokenAccum()
        other.n = self.n
        other.cjk = self.cjk
        other.nbytes = self.nbytes
        return other

    def feed(self, text: str):
        if not text:
            return
        self.n += len(text)
        self.cjk += len(_CJK_RE.findall(text))
        self.nbytes += len(text.encode("utf-8"))

    def tokens(self) -> int:
        if self.n == 0:
            return 0
        return self.cjk + (self.n - self.cjk) // 4 + 1


def serialize_rows(rows, cell_sep: str, row_sep: str) -> str:
    return row_sep.join(cell_sep.join(row) for row in rows)


def measure_rows(rows, cell_sep: str, row_sep: str) -> TokenAccum:
    acc = TokenAccum()
    for i, row in enumerate(rows):
        if i:
            acc.feed(row_sep)
        acc.feed(cell_sep.join(row))
    return acc
