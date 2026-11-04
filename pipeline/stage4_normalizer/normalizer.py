# pipeline/stage4_normalizer/normalizer.py
"""
Stage4 归一化层：把各块的抽取结果合并成统一 JSON（方案的"统一 JSON"契约）。
{
  "document_info": {type, filename, total_chunks, failed_chunks,
                    degraded_chunks, processed_at, content_hash},
  "structured_content": {
      "metadata": {...},
      "chapters": [{section_path, chunk_ids, status}],
      "global_entities": [{name, type, aliases, source_chunks}],
      "facts": [...], "tables": [...], "conflicts": [...]
  },
  "database_ready": bool
}
原则：
- 实体归一：同名（忽略大小写/首尾空格）同类型合并，别名保留原始写法
- 数值原值保留：任何字段都不做格式化/换算
- 冲突检测：同一 subject+predicate 出现不同 object → 记入 conflicts，事实仍保留
- 失败块不静默：failed/degraded 块计数进 document_info，内容不丢弃
"""
from datetime import datetime


def normalize(chunk_results: list, doc_type: str, filename: str,
              content_hash: str, extra_meta: dict = None) -> dict:
    """
    合并入口。
    chunk_results: ai_caller 输出的块级结果列表（可为空，如纯表格文档）
    返回统一 JSON dict。
    """
    chunk_results = chunk_results or []
    global_entities, facts, tables = _merge_entities(chunk_results), [], []
    conflicts = []

    # ---- 事实合并 + 冲突检测 ----
    fact_key_seen = {}  # (subject, predicate) → 首次出现的 object
    for r in chunk_results:
        for f in r.get("facts", []):
            facts.append({**f, "chunk_id": r["chunk_id"]})
            key = (f["subject"].strip(), f["predicate"].strip())
            first_obj = fact_key_seen.setdefault(key, f["object"].strip())
            if f["object"].strip() != first_obj:
                # 同一主语同一谓语出现不同宾语 → 冲突，两个值都保留供人判断
                conflict = next((c for c in conflicts
                                 if c["subject"] == key[0] and c["predicate"] == key[1]), None)
                if conflict:
                    if f["object"].strip() not in conflict["objects"]:
                        conflict["objects"].append(f["object"].strip())
                else:
                    conflicts.append({"subject": key[0], "predicate": key[1],
                                      "objects": [first_obj, f["object"].strip()]})

    # ---- 表格合并（块级表格直接拼接，附来源块） ----
    for r in chunk_results:
        for t in r.get("tables", []):
            tables.append({**t, "source_chunk": r["chunk_id"]})

    # ---- 章节分组（保持首次出现顺序） ----
    chapters, chapter_index = [], {}
    for r in chunk_results:
        sp = r["section_path"]
        if sp not in chapter_index:
            chapter_index[sp] = {"section_path": sp, "chunk_ids": [], "status": "ok"}
            chapters.append(chapter_index[sp])
        chapter_index[sp]["chunk_ids"].append(r["chunk_id"])
        if r["status"] == "failed" or chapter_index[sp]["status"] == "failed":
            chapter_index[sp]["status"] = "failed"  # 章节状态取最差块
        elif r["status"] == "degraded":
            chapter_index[sp]["status"] = "degraded"

    failed = [r for r in chunk_results if r["status"] == "failed"]
    degraded = [r for r in chunk_results if r["status"] == "degraded"]

    unified = {
        "document_info": {
            "type": doc_type,
            "filename": filename,
            "total_chunks": len(chunk_results),
            "failed_chunks": len(failed),
            "degraded_chunks": len(degraded),
            "processed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "content_hash": content_hash,
        },
        "structured_content": {
            "metadata": {
                "title": filename,
                "chunk_strategy": doc_type,
                **(extra_meta or {}),
            },
            "chapters": chapters,
            "global_entities": list(global_entities.values()),
            "facts": facts,
            "tables": tables,
            "conflicts": conflicts,
        },
        # 所有块都成功（或本来就是纯表格）才标记 ready
        "database_ready": len(failed) == 0,
    }
    return unified


def _merge_entities(chunk_results: list) -> dict:
    """
    实体归一：key = (name 小写去空格, type)。
    合并时 aliases 收集所有不同原始写法；记录来源块 chunk_id 列表。
    """
    merged = {}  # key → 实体 dict（插入序保序）
    for r in chunk_results:
        for e in r.get("entities", []):
            name = str(e.get("name", "")).strip()
            etype = str(e.get("type", "其他")).strip() or "其他"
            if not name:
                continue
            key = (name.lower(), etype)
            if key not in merged:
                merged[key] = {"name": name, "type": etype,
                               "aliases": [name], "source_chunks": []}
            ent = merged[key]
            if name not in ent["aliases"]:
                ent["aliases"].append(name)  # 大小写/写法差异进 aliases
            if r["chunk_id"] not in ent["source_chunks"]:
                ent["source_chunks"].append(r["chunk_id"])
    # 出现次数多的排前面，方便展示 Top 实体
    return dict(sorted(merged.items(),
                       key=lambda kv: -len(kv[1]["source_chunks"])))
