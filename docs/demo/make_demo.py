# -*- coding: utf-8 -*-
"""复现 README 里的演示素材：改前 docx / 改后 docx / 证据 JSON。

用法（在项目根目录）：
    python docs/demo/make_demo.py

说明：直接调用项目自己的 `WordModule.handle`，不使用任何 mock。
输出写入 `docs/images/`；证据来自 `storage/evidence/word/`（本地，不上传）。
"""
from __future__ import annotations

import gzip
import json
import shutil
import sys
from pathlib import Path

from docx import Document
from docx.shared import Pt

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "docs" / "images"
OUT.mkdir(parents=True, exist_ok=True)

before = OUT / "demo_before.docx"
after = OUT / "demo_after.docx"

# ---------- 1) 造一份"改前"文档 ----------
d = Document()
d.add_heading("设备巡检记录", level=1)

p = d.add_paragraph()
r1 = p.add_run("甲方是A型机，")
r1.bold = True
p.add_run("乙方是B型机。")

p2 = d.add_paragraph("巡检时间：2024-03-12，负责人：张工。")
for run in p2.runs:
    run.font.size = Pt(10.5)

t = d.add_table(rows=2, cols=2)
t.style = "Table Grid"
t.rows[0].cells[0].text = "设备"
t.rows[0].cells[1].text = "状态"
t.rows[1].cells[0].text = "A型机"
t.rows[1].cells[1].text = "运行"
d.save(str(before))

# ---------- 2) 用项目自己的模块改 ----------
shutil.copy2(before, after)

from pipeline.word_office import WordModule  # noqa: E402

res = WordModule.handle({
    "user_input": "把 A型机 替换成 C型机",
    "selected_files": [str(after)],
})
print("status :", res.get("status"))
print("reason :", res.get("reason"))
print("message:", res.get("message"))
print("applied:", (res.get("patch_set") or {}).get("applied"))

# ---------- 3) 取最新一条证据（脱敏后落一份到 docs/images） ----------
evdir = ROOT / "storage" / "evidence" / "word"
files = sorted(evdir.glob("*.json.gz"), key=lambda p: p.stat().st_mtime)
if files:
    with gzip.open(files[-1], "rt", encoding="utf-8") as f:
        data = json.load(f)
    # 去掉本机绝对路径，便于作为示例提交
    if isinstance(data.get("file"), dict):
        data["file"]["path"] = "docs/images/demo_after.docx"
    (OUT / "evidence_sample.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("evidence:", files[-1].name)
else:
    print("WARN: 没有找到证据文件（不影响 before/after）")

# ---------- 4) 清理 .bak ----------
bak = after.with_suffix(after.suffix + ".bak")
if bak.exists():
    bak.unlink()

print("done:", before.name, "/", after.name)
