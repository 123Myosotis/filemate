(function () {
  function token() {
    var el = document.querySelector('meta[name="x-memory-token"]');
    return el ? el.getAttribute("content") : "";
  }

  function banner(msg, kind) {
    var el = document.getElementById("banner");
    if (!el) return;
    el.hidden = false;
    el.className = "banner " + (kind || "info");
    el.textContent = msg;
    clearTimeout(banner._t);
    banner._t = setTimeout(function () {
      if (kind !== "fail") el.hidden = true;
    }, kind === "fail" ? 10000 : 4500);
  }

  function statusZh(st) {
    if (st === "proposed") return "待确认（proposed）";
    if (st === "active") return "已生效（active）";
    if (st === "invalidated") return "已失效（invalidated）";
    if (st === "revoked") return "已撤销（revoked）";
    return st || "—";
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function pretty(v) {
    try {
      return JSON.stringify(v, null, 2);
    } catch (e) {
      return String(v);
    }
  }

  function formatApiError(method, path, status, data, netMsg) {
    if (netMsg) {
      return (
        "网络失败 " +
        method +
        " " +
        path +
        "：" +
        netMsg +
        "（若刚清空/刷新并发，请重试；持续失败请重启 Web 服务）"
      );
    }
    var detail = data && data.detail;
    var reason = "";
    if (typeof detail === "string") {
      reason = detail;
    } else if (detail && typeof detail === "object") {
      reason =
        detail.subreason ||
        detail.message ||
        detail.fail_locus ||
        JSON.stringify(detail).slice(0, 180);
    } else if (data && data.message) {
      reason = String(data.message);
    } else if (data && data.raw) {
      reason = String(data.raw).slice(0, 180);
    }
    return (
      "HTTP " +
      (status != null ? status : "?") +
      " " +
      method +
      " " +
      path +
      (reason ? "：" + reason : "")
    );
  }

  async function api(method, path, formObj, jsonBody) {
    var opts = {
      method: method,
      headers: { "X-Memory-Token": token() },
    };
    if (jsonBody != null) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(jsonBody);
    } else if (formObj) {
      var fd = new FormData();
      Object.keys(formObj).forEach(function (k) {
        var v = formObj[k];
        fd.append(k, v == null ? "" : String(v));
      });
      opts.body = fd;
    }
    var resp;
    try {
      resp = await fetch(path, opts);
    } catch (netErr) {
      var ne = new Error(
        formatApiError(
          method,
          path,
          null,
          null,
          netErr && netErr.message ? netErr.message : String(netErr)
        )
      );
      ne.network = true;
      ne.path = path;
      throw ne;
    }
    var data = null;
    var text = "";
    try {
      text = await resp.text();
      data = JSON.parse(text);
    } catch (e) {
      data = { raw: text };
    }
    if (!resp.ok) {
      var err = new Error(formatApiError(method, path, resp.status, data, null));
      err.status = resp.status;
      err.data = data;
      err.path = path;
      throw err;
    }
    return data;
  }

  function payloadFields(bp) {
    if (!bp || typeof bp !== "object") {
      return { term: "—", description: "—", scope: "—" };
    }
    return {
      term: bp.term || "—",
      description: bp.description || "—",
      scope: bp.scope || "—",
    };
  }

  function sourceLabel(st) {
    if (st === "TXT_MINING") return "来源：人工导入（TXT）";
    if (st === "DIALOGUE_REVIEW") return "来源：对话复盘";
    if (st === "MAIBOT_CONFLICT") return "来源：MaiBot 冲突裁决";
    if (st === "WIZARD") return "来源：新建知识";
    if (st === "MANUAL") return "来源：手动";
    return st ? "来源：" + st : "来源：—";
  }

  var _dialogueSessionId = null;

  function engineLabel(eng) {
    if (eng === "chat") return "聊天（MaiBot 模式）";
    if (eng === "vector") return "向量（本地模式）";
    return eng || "—";
  }

  function switchTab(name) {
    document.querySelectorAll(".tab").forEach(function (t) {
      var on = t.getAttribute("data-tab") === name;
      t.classList.toggle("active", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
    });
    document.querySelectorAll(".tab-panel").forEach(function (p) {
      var on = p.getAttribute("data-panel") === name;
      p.classList.toggle("active", on);
      p.hidden = !on;
    });
  }

  function proposalCardHtml(p) {
    var bp = (p.payload && p.payload.binding_payload) || {};
    var f = payloadFields(bp);
    var src =
      p.source_type ||
      (p.payload && p.payload.source_type) ||
      "WIZARD";
    return (
      '<article class="card" data-proposal-id="' +
      esc(p.proposal_id) +
      '"><div class="card-main">' +
      '<p class="card-title">术语：' +
      esc(f.term) +
      "</p>" +
      '<p class="muted"><span class="source-tag">' +
      esc(sourceLabel(src)) +
      "</span> · 状态：待确认 · " +
      esc(p.created_at || "") +
      "</p>" +
      '<p class="desc-line"><span class="label">描述</span> ' +
      esc(f.description) +
      "</p>" +
      '<p class="desc-line"><span class="label">场景</span> ' +
      esc(f.scope) +
      "</p>" +
      '</div><div class="card-actions">' +
      '<button type="button" class="btn primary" data-action="confirm" data-proposal-id="' +
      esc(p.proposal_id) +
      '">确认写入</button>' +
      '<button type="button" class="btn danger" data-action="discard" data-proposal-id="' +
      esc(p.proposal_id) +
      '">丢弃</button></div></article>'
    );
  }

  function conflictCardHtml(c) {
    return (
      '<article class="card" data-conflict-id="' +
      esc(c.conflict_id) +
      '"><div class="card-main">' +
      '<p class="card-title">词条：' +
      esc(c.content) +
      "</p>" +
      '<p class="muted"><span class="source-tag">' +
      esc(sourceLabel(c.source_type || "MAIBOT_CONFLICT")) +
      "</span></p>" +
      '<p class="desc-line"><span class="label">MaiBot</span> ' +
      esc(c.maibot_meaning) +
      "</p>" +
      '<p class="desc-line"><span class="label">FileMate</span> ' +
      esc(c.filemate_meaning) +
      "</p>" +
      '</div><div class="card-actions wrap">' +
      '<button type="button" class="btn" data-action="resolve" data-resolution="keep_maibot" data-conflict-id="' +
      esc(c.conflict_id) +
      '">保留 MaiBot</button>' +
      '<button type="button" class="btn primary" data-action="resolve" data-resolution="keep_filemate" data-conflict-id="' +
      esc(c.conflict_id) +
      '">保留 FileMate</button>' +
      '<button type="button" class="btn" data-action="resolve" data-resolution="coexist" data-conflict-id="' +
      esc(c.conflict_id) +
      '">共存</button>' +
      '<button type="button" class="btn danger" data-action="resolve" data-resolution="dismissed" data-conflict-id="' +
      esc(c.conflict_id) +
      '">忽略</button></div></article>'
    );
  }

  function renderProposals(list, hostId) {
    var host = document.getElementById(hostId || "proposals-list");
    if (!host) return;
    if (!list || !list.length) {
      host.innerHTML = '<p class="empty">暂无待确认提议</p>';
      return;
    }
    host.innerHTML = list.map(proposalCardHtml).join("");
  }

  function renderConflicts(list, hostId) {
    var host = document.getElementById(hostId || "conflicts-list");
    if (!host) return;
    if (!list || !list.length) {
      host.innerHTML = '<p class="empty">暂无开放冲突</p>';
      return;
    }
    host.innerHTML = list.map(conflictCardHtml).join("");
  }

  function renderBindings(list) {
    var host = document.getElementById("bindings-list");
    if (!host) return;
    if (!list || !list.length) {
      host.innerHTML = '<p class="empty">暂无知识</p>';
      return;
    }
    host.innerHTML = list
      .map(function (b) {
        var f = payloadFields(b.binding_payload);
        var revokeBtn =
          b.lifecycle_status === "active"
            ? '<button type="button" class="btn danger" data-action="revoke" data-binding-id="' +
              esc(b.binding_id) +
              '" title="撤销后不可恢复">撤销（revoke）</button>'
            : "";
        return (
          '<details class="card fold" data-binding-id="' +
          esc(b.binding_id) +
          '"><summary>' +
          '<span class="mono short">' +
          esc(f.term !== "—" ? f.term : b.utterance_span_fingerprint || "—") +
          "</span>" +
          '<span class="pill">' +
          esc(statusZh(b.lifecycle_status)) +
          '</span></summary><div class="fold-body">' +
          '<p><span class="label">术语</span> ' +
          esc(f.term) +
          '</p>' +
          '<p><span class="label">描述</span> ' +
          esc(f.description) +
          '</p>' +
          '<p><span class="label">场景</span> ' +
          esc(f.scope) +
          '</p>' +
          '<p><span class="label">绑定编号（binding_id）</span> <code class="mono">' +
          esc(b.binding_id) +
          '</code></p>' +
          revokeBtn +
          '</div></details>'
        );
      })
      .join("");
  }

  function renderTraces(list) {
    var host = document.getElementById("traces-list");
    if (!host) return;
    if (!list || !list.length) {
      host.innerHTML = '<p class="empty">暂无操作记录</p>';
      return;
    }
    host.innerHTML = list
      .map(function (t) {
        return (
          '<article class="card"><div class="card-main">' +
          '<p class="card-title">' +
          esc(t.kind) +
          " · " +
          esc(t.created_at || "") +
          "</p>" +
          '<pre class="mini">' +
          esc(pretty(t.body)) +
          '</pre></div><div class="card-actions">' +
          '<button type="button" class="btn" data-action="replay" data-trace-id="' +
          esc(t.trace_id) +
          '">按记录回放</button></div></article>'
        );
      })
      .join("");
  }

  function renderMiningStatus(data) {
    var host = document.getElementById("mining-status");
    if (!host) return;
    var job = data && data.latest_job;
    var lines = [
      "<p><span class=\"label\">引擎</span> " +
        esc(engineLabel(data && data.self_learning_engine)) +
        "</p>",
      "<p><span class=\"label\">本地黑话条数</span> " +
        esc(data && data.filemate_jargon_count != null ? data.filemate_jargon_count : 0) +
        "</p>",
      "<p><span class=\"label\">待确认提议</span> " +
        esc(data && data.pending_proposal_count != null ? data.pending_proposal_count : 0) +
        "</p>",
      "<p><span class=\"label\">开放冲突</span> " +
        esc(data && data.open_conflict_count != null ? data.open_conflict_count : 0) +
        "</p>",
    ];
    if (!job) {
      lines.push('<p class="empty">尚无挖掘任务</p>');
    } else {
      lines.push(
        "<p><span class=\"label\">最近任务</span> " +
          esc(job.source_name) +
          " · " +
          esc(job.status) +
          "</p>"
      );
      lines.push(
        "<p class=\"muted\">词条 " +
          esc(job.term_count) +
          " · 推断 " +
          esc(job.inferred_count) +
          " · 静默 " +
          esc(job.silent_skip_count) +
          " · 冲突 " +
          esc(job.conflict_count) +
          " · 待确认 " +
          esc(job.pending_count) +
          "</p>"
      );
    }
    host.innerHTML = lines.join("");
  }

  function renderDashboard(data) {
    var engEl = document.getElementById("dash-engine-label");
    var metaEl = document.getElementById("dash-engine-meta");
    if (engEl) {
      engEl.textContent =
        "当前引擎：" + engineLabel(data && data.self_learning_engine);
    }
    if (metaEl) {
      metaEl.textContent =
        "等价判定：" +
        ((data && data.meaning_equivalence_mode) === "local"
          ? "本地字符串"
          : "AI 判定") +
        " · 对话复盘：" +
        (data && data.dialogue_review_enabled === false ? "关" : "开") +
        " · 外带策略：" +
        ((data && data.egress_policy) === "local_only"
          ? "本地模式"
          : "允许外部") +
        (data && data.retrieval_hint ? " · " + data.retrieval_hint : "");
    }
    var stats = (data && data.job_stats) || {};
    var statsHost = document.getElementById("dash-job-stats");
    if (statsHost) {
      statsHost.innerHTML =
        "<p><span class=\"label\">最近任务数</span> " +
        esc(stats.total != null ? stats.total : 0) +
        " · 成功 " +
        esc(stats.success != null ? stats.success : 0) +
        " · 失败 " +
        esc(stats.failed != null ? stats.failed : 0) +
        " · 运行中 " +
        esc(stats.running != null ? stats.running : 0) +
        "</p>";
    }
    var jobsHost = document.getElementById("dash-jobs-list");
    if (jobsHost) {
      var jobs = (data && data.jobs) || [];
      if (!jobs.length) {
        jobsHost.innerHTML = '<p class="empty">尚无 TXT 学习任务</p>';
      } else {
        jobsHost.innerHTML = jobs
          .map(function (j) {
            return (
              '<article class="card tight"><div class="card-main">' +
              '<p class="card-title">' +
              esc(j.source_name) +
              " · " +
              esc(j.status) +
              "</p>" +
              '<p class="muted">推断 ' +
              esc(j.inferred_count) +
              " · 静默 " +
              esc(j.silent_skip_count) +
              " · 冲突 " +
              esc(j.conflict_count) +
              " · 待确认 " +
              esc(j.pending_count) +
              " · " +
              esc(j.updated_at || j.created_at || "") +
              "</p></div></article>"
            );
          })
          .join("");
      }
    }
    renderInbox(data);
    renderEgress(data && data.egress_audits);
  }

  function renderInbox(data) {
    var host = document.getElementById("dash-inbox-list");
    if (!host) return;
    var parts = [];
    var inbox = (data && data.inbox) || [];
    if (inbox.length) {
      inbox.forEach(function (item) {
        if (item.kind === "conflict" || item.conflict_id) {
          parts.push(conflictCardHtml(item));
        } else {
          parts.push(proposalCardHtml(item));
        }
      });
    } else {
      var props = (data && data.pending_proposals) || [];
      var confs = (data && data.conflicts) || [];
      props.forEach(function (p) {
        parts.push(proposalCardHtml(p));
      });
      confs.forEach(function (c) {
        parts.push(conflictCardHtml(c));
      });
    }
    host.innerHTML = parts.length
      ? parts.join("")
      : '<p class="empty">暂无待确认或冲突</p>';
  }

  function renderEgress(list) {
    var host = document.getElementById("dash-egress-list");
    if (!host) return;
    if (!list || !list.length) {
      host.innerHTML = '<p class="empty">尚无外带记录</p>';
      return;
    }
    host.innerHTML = list
      .map(function (a) {
        return (
          '<article class="card tight"><div class="card-main">' +
          '<p class="card-title">' +
          esc(a.status) +
          " · " +
          esc(a.channel || "") +
          " · " +
          esc(a.model_name || "") +
          "</p>" +
          '<p class="muted">' +
          esc(a.timestamp || a.created_at || "") +
          " · " +
          esc(a.target_url || "") +
          " · " +
          esc(a.payload_chars != null ? a.payload_chars : 0) +
          " chars</p>" +
          '<pre class="mini">' +
          esc(a.payload_preview || "") +
          "</pre></div></article>"
        );
      })
      .join("");
  }

  async function refreshMaibotDirWarn(pathOverride) {
    var warn = document.getElementById("maibot-dir-warn");
    var input = document.getElementById("set-maibot-dir");
    if (!warn) return;
    var path =
      pathOverride != null
        ? pathOverride
        : input
          ? (input.value || "").trim()
          : "";
    if (!path) {
      warn.hidden = true;
      return;
    }
    try {
      var data = await api(
        "GET",
        "/api/settings/maibot_data_dir/validate?path=" +
          encodeURIComponent(path)
      );
      if (data && data.valid) {
        warn.hidden = true;
      } else {
        warn.hidden = false;
        warn.textContent = (data && data.message) || "未找到数据库文件";
      }
    } catch (e) {
      warn.hidden = false;
      warn.textContent = "未找到数据库文件";
    }
  }

  function fillSettingsForm(data) {
    if (!data) return;
    var set = function (id, val) {
      var el = document.getElementById(id);
      if (el) el.value = val != null ? val : "";
    };
    set("set-maibot-dir", data.maibot_data_dir);
    refreshMaibotDirWarn(data.maibot_data_dir || "");
    set("set-embed-path", data.embedded_model_path);
    set(
      "set-thresholds",
      (data.jargon_inference_thresholds || []).join(",")
    );
    set(
      "set-vw",
      data.fusion && data.fusion.vector_weight != null
        ? data.fusion.vector_weight
        : ""
    );
    set(
      "set-bw",
      data.fusion && data.fusion.bm25_weight != null
        ? data.fusion.bm25_weight
        : ""
    );
    var model = data.model || {};
    set("set-api-base", model.api_base_url);
    set("set-api-model", model.api_model);
    set("set-api-key", "");
    var keyHint = document.getElementById("set-api-key-hint");
    if (keyHint) {
      keyHint.textContent = model.api_key_configured
        ? "已配置 Key（" + (model.api_key_masked || "***") + "），留空保存不改动"
        : "尚未配置 API Key";
    }
    var eng = data.self_learning_engine || "chat";
    document.querySelectorAll('input[name="self_learning_engine"]').forEach(
      function (r) {
        r.checked = r.value === eng;
      }
    );
    var mode = data.meaning_equivalence_mode || "ai";
    document.querySelectorAll('input[name="meaning_equivalence_mode"]').forEach(
      function (r) {
        r.checked = r.value === mode;
      }
    );
    var eg = data.egress_policy || "allow_external";
    document.querySelectorAll('input[name="egress_policy"]').forEach(function (r) {
      r.checked = r.value === eg;
    });
    var dlg = document.getElementById("set-dialogue-on");
    if (dlg) dlg.checked = data.dialogue_review_enabled !== false;
    var provider = data.dense_provider || "off";
    document.querySelectorAll('input[name="dense_provider"]').forEach(function (r) {
      r.checked = r.value === provider;
    });
    applyDenseProviderPanels(provider, data.ui || {});
    var ext = data.external_embedding || {};
    set("set-ext-emb-base", ext.base_url || "");
    set("set-ext-emb-model", ext.model || "");
    set("set-ext-emb-key", "");
    set(
      "set-ext-emb-weight",
      ext.weight != null ? ext.weight : "0"
    );
    var extKeyHint = document.getElementById("set-ext-emb-key-hint");
    if (extKeyHint) {
      var masked = data.api_key_masked || ext.api_key_masked || "";
      extKeyHint.textContent = ext.api_key_configured
        ? "已配置 Key（末 8 位可见：" +
          masked +
          "，来源 " +
          (ext.api_key_source || "config") +
          "），留空保存不改动"
        : "尚未配置外部 API Key";
    }
    var extStatus = document.getElementById("set-ext-emb-status");
    if (extStatus) {
      extStatus.textContent = ext.status_hint || "";
    }
    fillMigrationBanner(data);
    var hint = document.getElementById("settings-hint");
    if (hint) {
      hint.textContent =
        "引擎 " +
        engineLabel(eng) +
        " · 向量来源 " +
        provider +
        " · 兜底阈值 " +
        (data.meaning_fallback_threshold != null
          ? data.meaning_fallback_threshold
          : "—") +
        (ext.status_hint ? " · " + ext.status_hint : "");
    }
  }

  function applyDenseProviderPanels(provider, ui) {
    var offHint = document.getElementById("dense-off-hint");
    var localPanel = document.getElementById("dense-local-panel");
    var extPanel = document.getElementById("dense-external-panel");
    var showOff = ui.show_off_hint != null ? !!ui.show_off_hint : provider === "off";
    var showLocal =
      ui.show_local_path != null ? !!ui.show_local_path : provider === "local";
    var showExt =
      ui.show_external_fields != null
        ? !!ui.show_external_fields
        : provider === "external";
    if (offHint) offHint.hidden = !showOff;
    if (localPanel) localPanel.hidden = !showLocal;
    if (extPanel) extPanel.hidden = !showExt;
  }

  function fillMigrationBanner(data) {
    var bannerEl = document.getElementById("migration-banner");
    var actions = document.getElementById("migration-banner-actions");
    var show = !!data.show_migration_banner;
    if (bannerEl) {
      bannerEl.hidden = !show;
      bannerEl.textContent = show
        ? data.migration_banner_text ||
          "检测到旧配置冲突，已默认本地。请确认。"
        : "";
    }
    if (actions) actions.hidden = !show;
  }

  async function acknowledgeMigration(choice) {
    try {
      var data = await api("POST", "/api/settings", null, {
        migration_conflict_acknowledged: true,
        migration_conflict_choice: choice,
        dense_provider: choice,
      });
      fillSettingsForm((data && data.settings) || data || {});
      banner("已确认向量来源：" + choice, "ok");
    } catch (e) {
      banner("确认失败：" + (e && e.message ? e.message : String(e)), "fail");
    }
  }

  async function refreshSettings() {
    try {
      var data = await api("GET", "/api/settings");
      fillSettingsForm(data);
    } catch (e) {
      /* optional */
    }
  }

  async function refreshAll() {
    var filter = document.getElementById("binding-filter");
    var st = filter ? filter.value : "active";
    var q = st && st !== "all" ? "?status=" + encodeURIComponent(st) : "";
    // Sequential fetches: avoid hammering the single SQLite connection.
    var failures = [];
    async function step(label, fn) {
      try {
        return await fn();
      } catch (e) {
        failures.push(label + " → " + (e && e.message ? e.message : String(e)));
        return null;
      }
    }
    var bindings = await step("bindings", function () {
      return api("GET", "/api/bindings" + q);
    });
    var proposals = await step("proposals", function () {
      return api("GET", "/api/proposals?status=proposed");
    });
    var traces = await step("traces", function () {
      return api("GET", "/api/traces");
    });
    var mining = await step("mining", function () {
      return api("GET", "/api/mining/status");
    });
    var conflicts = await step("conflicts", function () {
      return api("GET", "/api/conflicts");
    });
    var dash = await step("dashboard", function () {
      return api("GET", "/api/dashboard");
    });
    if (bindings) renderBindings(bindings.bindings || []);
    if (proposals) renderProposals(proposals.proposals || [], "proposals-list");
    if (traces) renderTraces(traces.traces || []);
    if (mining) renderMiningStatus(mining || {});
    if (conflicts) renderConflicts(conflicts.conflicts || [], "conflicts-list");
    if (dash) renderDashboard(dash || {});
    await step("settings", function () {
      return refreshSettings();
    });
    if (failures.length && failures.length >= 6) {
      banner("加载失败：" + failures[0], "fail");
    } else if (failures.length) {
      banner("部分刷新失败（" + failures.length + "）：" + failures[0], "fail");
    }
  }

  async function onAction(btn) {
    var action = btn.getAttribute("data-action");
    try {
      if (action === "confirm") {
        var cid = btn.getAttribute("data-proposal-id");
        if (!cid) throw new Error("缺少 proposal_id");
        await api("POST", "/api/confirm_memory_edit", {
          proposal_id: cid,
        });
        banner("写入成功，已加入生效列表", "ok");
      } else if (action === "discard") {
        var did = btn.getAttribute("data-proposal-id");
        if (!did) throw new Error("缺少 proposal_id");
        await api("POST", "/api/discard_proposal", {
          proposal_id: did,
        });
        banner("已丢弃该提议（未写入）", "info");
      } else if (action === "revoke") {
        if (!window.confirm("撤销后不可恢复，确认撤销？")) return;
        await api("POST", "/api/revoke_memory_edit", {
          binding_id: btn.getAttribute("data-binding-id"),
        });
        banner("已撤销", "ok");
      } else if (action === "replay") {
        var data = await api("POST", "/api/replay_memory_edit", {
          trace_id: btn.getAttribute("data-trace-id"),
        });
        var box = document.getElementById("replay-result");
        if (box) {
          box.hidden = false;
          box.textContent = pretty(data);
        }
        banner("回放成功", "ok");
      } else if (action === "resolve") {
        await api("POST", "/api/conflicts/resolve", {
          conflict_id: btn.getAttribute("data-conflict-id"),
          resolution: btn.getAttribute("data-resolution"),
          note: "",
        });
        banner("冲突已裁决", "ok");
      } else {
        return;
      }
      await refreshAll();
    } catch (e) {
      var msg = e && e.message ? e.message : String(e);
      if (
        msg.indexOf("加载失败：") !== 0 &&
        msg.indexOf("HTTP ") !== 0 &&
        msg.indexOf("网络失败") !== 0 &&
        msg.indexOf("新建失败：") !== 0
      ) {
        msg = "操作失败：" + msg;
      }
      banner(msg, "fail");
    }
  }

  function collectSettingsPatch() {
    var form = document.getElementById("settings-unified-form");
    if (!form) return {};
    var engEl = form.querySelector(
      'input[name="self_learning_engine"]:checked'
    );
    var modeEl = form.querySelector(
      'input[name="meaning_equivalence_mode"]:checked'
    );
    var thrRaw = (document.getElementById("set-thresholds").value || "").trim();
    var thresholds = thrRaw
      ? thrRaw.split(/[,;]+/).map(function (x) {
          return parseInt(x.trim(), 10);
        }).filter(function (n) {
          return !isNaN(n);
        })
      : undefined;
    var patch = {
      maibot_data_dir: (document.getElementById("set-maibot-dir").value || "").trim(),
      self_learning_engine: engEl ? engEl.value : "chat",
      meaning_equivalence_mode: modeEl ? modeEl.value : "ai",
      embedded_model_path: (
        document.getElementById("set-embed-path").value || ""
      ).trim(),
      dialogue_review_enabled: !!document.getElementById("set-dialogue-on")
        .checked,
      egress_policy: (function () {
        var el = document.querySelector('input[name="egress_policy"]:checked');
        return el ? el.value : "allow_external";
      })(),
      api_base_url: (document.getElementById("set-api-base").value || "").trim(),
      api_model: (document.getElementById("set-api-model").value || "").trim(),
      fusion: {
        vector_weight: parseFloat(
          document.getElementById("set-vw").value || "0.7"
        ),
        bm25_weight: parseFloat(
          document.getElementById("set-bw").value || "0.3"
        ),
      },
    };
    if (thresholds && thresholds.length) {
      patch.jargon_inference_thresholds = thresholds;
    }
    var keyVal = document.getElementById("set-api-key").value;
    if (keyVal != null && String(keyVal).length > 0) {
      patch.api_key = String(keyVal).trim();
    }
    var denseEl = document.querySelector('input[name="dense_provider"]:checked');
    patch.dense_provider = denseEl ? denseEl.value : "off";
    var extEmb = {
      base_url: (document.getElementById("set-ext-emb-base").value || "").trim(),
      model: (document.getElementById("set-ext-emb-model").value || "").trim(),
      weight: parseFloat(
        document.getElementById("set-ext-emb-weight").value || "0"
      ),
    };
    var extKeyEl = document.getElementById("set-ext-emb-key");
    var extKeyVal = extKeyEl ? String(extKeyEl.value || "").trim() : "";
    // Empty / masked → omit api_key (server keeps existing). Explicit clear sentinel.
    if (extKeyVal === "__CLEAR__" || extKeyVal.toLowerCase() === "clear") {
      extEmb.clear = true;
    } else if (extKeyVal && extKeyVal.indexOf("*") < 0) {
      extEmb.api_key = extKeyVal;
    }
    patch.external_embedding = extEmb;
    return patch;
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".tab").forEach(function (tab) {
      tab.addEventListener("click", function () {
        switchTab(tab.getAttribute("data-tab"));
      });
    });

    var form = document.getElementById("propose-form");
    if (form) {
      form.addEventListener("submit", async function (e) {
        e.preventDefault();
        var term = (document.getElementById("wizard-term").value || "").trim();
        var description = (
          document.getElementById("wizard-description").value || ""
        ).trim();
        var scopeText = (
          document.getElementById("wizard-scope").value || ""
        ).trim();
        if (!term || !description) {
          banner("加载失败：请填写业务术语与术语描述", "fail");
          return;
        }
        try {
          await api("POST", "/api/propose_wizard", {
            term: term,
            description: description,
            scope_text: scopeText,
          });
          document.getElementById("wizard-term").value = "";
          document.getElementById("wizard-description").value = "";
          document.getElementById("wizard-scope").value = "";
          banner("提议已生成，请去待确认列表处理", "ok");
          await refreshAll();
        } catch (err) {
          banner(
            "新建失败：" + (err && err.message ? err.message : String(err)),
            "fail"
          );
        }
      });
    }

    document.querySelectorAll('input[name="dense_provider"]').forEach(function (r) {
      r.addEventListener("change", function () {
        applyDenseProviderPanels(r.value, {});
      });
    });
    var btnClearKey = document.getElementById("btn-clear-ext-key");
    if (btnClearKey) {
      btnClearKey.addEventListener("click", function () {
        var el = document.getElementById("set-ext-emb-key");
        if (el) el.value = "clear";
        banner("将在保存时清除外部 API Key", "ok");
      });
    }
    var btnMigLocal = document.getElementById("btn-mig-local");
    if (btnMigLocal) {
      btnMigLocal.addEventListener("click", function () {
        acknowledgeMigration("local");
      });
    }
    var btnMigExt = document.getElementById("btn-mig-external");
    if (btnMigExt) {
      btnMigExt.addEventListener("click", function () {
        acknowledgeMigration("external");
      });
    }
    var btnMigOff = document.getElementById("btn-mig-off");
    if (btnMigOff) {
      btnMigOff.addEventListener("click", function () {
        acknowledgeMigration("off");
      });
    }
    var settingsForm = document.getElementById("settings-unified-form");
    if (settingsForm) {
      settingsForm.addEventListener("submit", async function (e) {
        e.preventDefault();
        try {
          var patch = collectSettingsPatch();
          await api("POST", "/api/settings", null, patch);
          banner("设置已保存（已写入本地配置文件；外部 Key 不回显明文）", "ok");
          await refreshAll();
        } catch (err) {
          banner(
            "加载失败：" + (err && err.message ? err.message : String(err)),
            "fail"
          );
        }
      });
    }

    var btnDlgStart = document.getElementById("btn-dialogue-start");
    if (btnDlgStart) {
      btnDlgStart.addEventListener("click", async function () {
        try {
          var r = await api("POST", "/api/dialogue/sessions", {
            title: "web-session",
          });
          _dialogueSessionId = r.session_id;
          var hint = document.getElementById("dialogue-session-hint");
          if (hint) hint.textContent = "会话：" + _dialogueSessionId;
          banner("会话已开始，可追加对话轮次", "ok");
        } catch (err) {
          banner(
            "加载失败：" + (err && err.message ? err.message : String(err)),
            "fail"
          );
        }
      });
    }
    var btnDlgTurn = document.getElementById("btn-dialogue-turn");
    if (btnDlgTurn) {
      btnDlgTurn.addEventListener("click", async function () {
        if (!_dialogueSessionId) {
          banner("加载失败：请先开始会话", "fail");
          return;
        }
        var role = (document.getElementById("dialogue-role").value || "user").trim();
        var content = (
          document.getElementById("dialogue-content").value || ""
        ).trim();
        if (!content) {
          banner("加载失败：请填写对话内容", "fail");
          return;
        }
        try {
          await api("POST", "/api/dialogue/turns", {
            session_id: _dialogueSessionId,
            role: role,
            content: content,
          });
          document.getElementById("dialogue-content").value = "";
          banner("已追加一轮对话", "ok");
        } catch (err) {
          banner(
            "加载失败：" + (err && err.message ? err.message : String(err)),
            "fail"
          );
        }
      });
    }
    var btnDlgEnd = document.getElementById("btn-dialogue-end");
    if (btnDlgEnd) {
      btnDlgEnd.addEventListener("click", async function () {
        if (!_dialogueSessionId) {
          banner("加载失败：请先开始会话", "fail");
          return;
        }
        try {
          var r = await api("POST", "/api/dialogue/end", {
            session_id: _dialogueSessionId,
            async_review: "0",
          });
          var box = document.getElementById("dialogue-result");
          if (box) {
            box.hidden = false;
            box.textContent = pretty(r);
          }
          banner(
            "复盘完成：待确认 " +
              (r.pending_count || 0) +
              " / 冲突 " +
              (r.conflict_count || 0) +
              " / 静默 " +
              (r.silent_skip_count || 0),
            "ok"
          );
          _dialogueSessionId = null;
          var hint = document.getElementById("dialogue-session-hint");
          if (hint) hint.textContent = "会话已结束";
          await refreshAll();
        } catch (err) {
          banner(
            "加载失败：" + (err && err.message ? err.message : String(err)),
            "fail"
          );
        }
      });
    }

    var browseBtn = document.getElementById("btn-browse-maibot");
    if (browseBtn) {
      browseBtn.addEventListener("click", async function () {
        try {
          banner("正在打开系统文件夹选择框…", "info");
          var r = await api("GET", "/api/pick_directory");
          if (r && r.cancelled) {
            banner("已取消选择", "info");
            return;
          }
          if (r && r.status === "error") {
            banner(
              "加载失败：" + (r.error || "无法打开文件夹选择框"),
              "fail"
            );
            return;
          }
          var path = (r && (r.path || r.maibot_data_dir)) || "";
          var input = document.getElementById("set-maibot-dir");
          if (input) input.value = path;
          await refreshMaibotDirWarn(path);
          banner(
            path ? "已选择并保存 MaiBot 目录（只读对接）" : "未选择目录",
            path ? "ok" : "info"
          );
          await refreshSettings();
        } catch (err) {
          banner(
            "加载失败：" + (err && err.message ? err.message : String(err)),
            "fail"
          );
        }
      });
    }

    var maibotInput = document.getElementById("set-maibot-dir");
    if (maibotInput) {
      maibotInput.addEventListener("change", function () {
        refreshMaibotDirWarn();
      });
      maibotInput.addEventListener("blur", function () {
        refreshMaibotDirWarn();
      });
    }

    var txtForm = document.getElementById("txt-form");
    if (txtForm) {
      txtForm.addEventListener("submit", async function (e) {
        e.preventDefault();
        var fileInput = document.getElementById("txt-file");
        if (!fileInput || !fileInput.files || !fileInput.files[0]) {
          banner("加载失败：请选择 txt 文件", "fail");
          return;
        }
        var fd = new FormData();
        fd.append("file", fileInput.files[0]);
        try {
          var resp = await fetch("/api/mining/import_txt", {
            method: "POST",
            headers: { "X-Memory-Token": token() },
            body: fd,
          });
          var text = await resp.text();
          var data = null;
          try {
            data = JSON.parse(text);
          } catch (pe) {
            data = { raw: text };
          }
          if (!resp.ok) {
            var detail = data && data.detail;
            throw new Error(
              typeof detail === "string"
                ? detail
                : (detail && (detail.subreason || detail.message)) ||
                  "HTTP " + resp.status
            );
          }
          var box = document.getElementById("txt-result");
          if (box) {
            box.hidden = false;
            box.textContent = pretty(data);
          }
          banner(
            "学习完成：静默 " +
              (data.silent_skip_count || 0) +
              " / 冲突 " +
              (data.conflict_count || 0) +
              " / 待确认 " +
              (data.pending_count || 0),
            "ok"
          );
          await refreshAll();
        } catch (err) {
          banner(
            "加载失败：" + (err && err.message ? err.message : String(err)),
            "fail"
          );
        }
      });
    }

    document.body.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-action]");
      if (!btn) return;
      onAction(btn);
    });

    var reload = document.getElementById("btn-reload-lists");
    if (reload) {
      reload.addEventListener("click", function () {
        refreshAll();
      });
    }

    var filter = document.getElementById("binding-filter");
    if (filter) {
      filter.addEventListener("change", function () {
        refreshAll();
      });
    }

    var purgeBtn = document.getElementById("btn-purge-local");
    if (purgeBtn) {
      purgeBtn.addEventListener("click", async function () {
        var ok = window.confirm(
          "将清空 FileMate 本地记忆库（待确认/黑话/对话/审计/绑定），不动 MaiBot，设置保留。确认？"
        );
        if (!ok) return;
        var ok2 = window.confirm("再次确认：不可撤销。继续清空？");
        if (!ok2) return;
        try {
          var data = await api("POST", "/api/admin/purge_local_memory", {
            confirm: "PURGE",
          });
          var hint = document.getElementById("purge-hint");
          if (hint) {
            hint.textContent =
              "已清空。待确认剩余 " +
              (data.pending_proposals_remaining != null
                ? data.pending_proposals_remaining
                : "—");
          }
          banner("本机测试数据已清空（MaiBot 未动）", "ok");
          await refreshAll();
        } catch (err) {
          banner(
            "清空失败：" + (err && err.message ? err.message : String(err)),
            "fail"
          );
        }
      });
    }

    refreshAll().catch(function (e) {
      banner("加载失败：" + (e && e.message ? e.message : e), "fail");
    });
  });
})();
