"""2a 控制流最小落地：on_analysis + 疑问回复次数提示。不写桥/拼接。"""

import config


def _qid(q, fallback):
    raw = q.get("raw") if isinstance(q, dict) else q
    if isinstance(raw, dict) and raw.get("question_id") is not None:
        return str(raw["question_id"])
    # 用四要素+block_ids 指纹作稳定键（不生成疑问正文）
    if isinstance(raw, dict):
        key = (
            str(raw.get("hypothesis") or ""),
            str(raw.get("evidence") or ""),
            str(raw.get("if_holds") or ""),
            str(raw.get("ask_user") or ""),
            tuple(raw.get("block_ids") or []),
            str(raw.get("scope") or ""),
        )
        return "q:" + repr(key)
    return "q:" + str(fallback)


def _same_question(a_raw, b_raw):
    if not isinstance(a_raw, dict) or not isinstance(b_raw, dict):
        return False
    keys = ("hypothesis", "evidence", "if_holds", "ask_user", "scope")
    if any(str(a_raw.get(k) or "") != str(b_raw.get(k) or "") for k in keys):
        return False
    return list(a_raw.get("block_ids") or []) == list(b_raw.get("block_ids") or [])


def on_analysis(analysis, block_plan, bridge_readonly=None, state=None):
    """
    2a on_analysis：零条疑问则全部放行；可执行疑问写入暂停；畸形只留痕不暂停。
    不调用桥实现，只声明 released_to_bridge 清单。
    """
    state = dict(state or {})
    journal = list(state.get("journal") or [])
    reply_counts = dict(state.get("reply_counts") or {})
    mappings = list((analysis or {}).get("mappings") or [])
    questions = list((analysis or {}).get("questions") or [])

    known = {bl.get("block_id") for bl in (block_plan or {}).get("blocks") or []}
    pause = {"blocks": [], "mappings": [], "value_domains": []}
    open_questions = []
    prompts = []

    for i, q in enumerate(questions):
        raw = q.get("raw") if isinstance(q, dict) else q
        if not q.get("executable"):
            journal.append(
                {
                    "question_raw": raw,
                    "reply_raw": "",
                    "mapping_before": None,
                    "mapping_after": None,
                    "disposition": "unexecutable",
                    "reason": q.get("reason"),
                }
            )
            continue
        qid = _qid(q, i)
        open_questions.append({"question_id": qid, "raw": raw})
        journal.append(
            {
                "question_raw": raw,
                "reply_raw": "",
                "mapping_before": None,
                "mapping_after": None,
                "disposition": "open",
                "question_id": qid,
            }
        )
        scope = str(raw.get("scope") or "block")
        bids = [b for b in (raw.get("block_ids") or []) if b in known]
        if scope == "block" or (
            scope in ("mapping", "value_domain") and not raw.get("mapping_indexes")
        ):
            for b in bids:
                if b not in pause["blocks"]:
                    pause["blocks"].append(b)
        elif scope == "mapping":
            for b in bids:
                for idx in raw.get("mapping_indexes") or []:
                    pause["mappings"].append({"block_id": b, "mapping_index": idx})
        elif scope == "value_domain":
            for b in bids:
                for idx in raw.get("mapping_indexes") or []:
                    pause["value_domains"].append(
                        {
                            "block_id": b,
                            "mapping_index": idx,
                            "value_domain": raw.get("value_domain"),
                        }
                    )

    paused_block = set(pause["blocks"])
    paused_map = {(m["block_id"], m["mapping_index"]) for m in pause["mappings"]}
    released = []
    # mapping 归属：按 sources 首次 block_id 计本块下标
    per_block_seq = {}
    for gi, m in enumerate(mappings):
        for src in m.get("sources") or []:
            bid = src.get("block_id")
            if bid is None:
                continue
            local = per_block_seq.get(bid, 0)
            per_block_seq[bid] = local + 1
            if bid in paused_block:
                break
            if (bid, local) in paused_map:
                break
            released.append({"block_id": bid, "mapping_index": local, "global_index": gi})
            break

    waiting = bool(open_questions)
    if not questions:
        # 零条疑问：全部 mappings 进入 released_to_bridge
        released = []
        per_block_seq = {}
        for gi, m in enumerate(mappings):
            for src in m.get("sources") or []:
                bid = src.get("block_id")
                local = per_block_seq.get(bid, 0)
                per_block_seq[bid] = local + 1
                released.append(
                    {"block_id": bid, "mapping_index": local, "global_index": gi}
                )
                break
        waiting = False

    return {
        "open_questions": open_questions,
        "pause": pause,
        "released_to_bridge": released,
        "journal": journal,
        "waiting": waiting,
        "reply_counts": reply_counts,
        "ui_prompts": prompts,
        "bridge_readonly": bridge_readonly or {"passed": []},
    }


def on_user_reply_count(state, reply_text, new_analysis_questions, answered_ids=None):
    """
    用户回复后：累加同一疑问的回复次数；达阈值且仍返回同一疑问 → 只提示。
    不自动关闭、不放行。
    """
    state = dict(state or {})
    counts = dict(state.get("reply_counts") or {})
    threshold = int(config.get_param("question.repeat_prompt_threshold"))
    open_q = list(state.get("open_questions") or [])
    answered_ids = set(answered_ids) if answered_ids is not None else {
        q.get("question_id") for q in open_q
    }
    ui_prompts = []
    journal = list(state.get("journal") or [])

    new_raws = []
    for q in new_analysis_questions or []:
        raw = q.get("raw") if isinstance(q, dict) else q
        if q.get("executable", True):
            new_raws.append(raw)

    still = []
    for oq in open_q:
        qid = oq.get("question_id")
        if qid not in answered_ids:
            still.append(oq)
            continue
        counts[qid] = int(counts.get(qid) or 0) + 1
        matched = any(_same_question(oq.get("raw"), nr) for nr in new_raws)
        journal.append(
            {
                "question_raw": oq.get("raw"),
                "reply_raw": reply_text or "",
                "disposition": "still_open" if matched else "reanalyzed",
                "question_id": qid,
                "reply_count": counts[qid],
            }
        )
        if matched:
            still.append(oq)
            if counts[qid] >= threshold:
                ui_prompts.append(question_repeat_prompt())
        # 未 matched：该疑问关闭，不放进 still

    # 新分析里全新的可执行疑问也加入开放集
    existing_ids = {s.get("question_id") for s in still}
    for i, q in enumerate(new_analysis_questions or []):
        if not q.get("executable", True):
            continue
        qid = _qid(q, "new-%s" % i)
        if any(_same_question(q.get("raw"), s.get("raw")) for s in still):
            continue
        if qid in existing_ids:
            continue
        still.append({"question_id": qid, "raw": q.get("raw")})

    state["open_questions"] = still
    state["reply_counts"] = counts
    state["journal"] = journal
    state["ui_prompts"] = ui_prompts
    state["waiting"] = bool(still)
    return state


def question_repeat_prompt():
    return "AI 仍在询问此问题，可继续回答或取消任务"
