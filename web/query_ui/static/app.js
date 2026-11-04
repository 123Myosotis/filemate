/* P2-B query UI — request / render only; no business rules. */
(function () {
  "use strict";

  function meta(name) {
    var el = document.querySelector('meta[name="' + name + '"]');
    return el ? el.getAttribute("content") : "";
  }

  var TOKEN = meta("x-query-token");
  var TOKEN_HEADER = meta("x-token-header") || "X-Memory-Token";

  var state = {
    filePaths: [],
    askHistory: [],
    priorFive: null,
    priorFailLocus: null,
  };

  var FIVE_LABELS = {
    speculation: "推测",
    evidence: "依据",
    what_might_be_wrong: "可能哪里不对",
    next_step: "下一步",
    please_confirm_or_correct: "请确认或纠正",
  };

  function $(id) {
    return document.getElementById(id);
  }

  function showBanner(kind, text) {
    var b = $("banner");
    b.hidden = !text;
    b.className = "banner " + (kind || "info");
    b.textContent = text || "";
  }

  function authHeaders(extra) {
    var h = {};
    h[TOKEN_HEADER] = TOKEN;
    if (extra) {
      Object.keys(extra).forEach(function (k) {
        h[k] = extra[k];
      });
    }
    return h;
  }

  function setStages(stages) {
    var ul = $("stage-list");
    ul.innerHTML = "";
    if (!stages || !stages.length) {
      var li0 = document.createElement("li");
      li0.className = "muted";
      li0.textContent = "等待开始…";
      ul.appendChild(li0);
      return;
    }
    stages.forEach(function (s) {
      var li = document.createElement("li");
      li.className = String(s.status || "");
      var detail = s.detail ? " — " + s.detail : "";
      li.textContent = (s.name || "?") + ": " + (s.status || "") + detail;
      ul.appendChild(li);
    });
  }

  function renderFive(five) {
    var dl = $("five-part");
    dl.innerHTML = "";
    var keys = Object.keys(FIVE_LABELS);
    keys.forEach(function (k) {
      var dt = document.createElement("dt");
      dt.textContent = FIVE_LABELS[k];
      var dd = document.createElement("dd");
      dd.textContent = (five && five[k]) || "（空）";
      dl.appendChild(dt);
      dl.appendChild(dd);
    });
  }

  function renderTable(preview) {
    var thead = $("result-table").querySelector("thead");
    var tbody = $("result-table").querySelector("tbody");
    thead.innerHTML = "";
    tbody.innerHTML = "";
    var cols = (preview && preview.columns) || [];
    var rows = (preview && preview.rows) || [];
    var trh = document.createElement("tr");
    cols.forEach(function (c) {
      var th = document.createElement("th");
      th.textContent = c;
      trh.appendChild(th);
    });
    thead.appendChild(trh);
    rows.forEach(function (row) {
      var tr = document.createElement("tr");
      cols.forEach(function (c) {
        var td = document.createElement("td");
        var v = row ? row[c] : "";
        td.textContent = v == null ? "" : String(v);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    $("result-meta").textContent =
      "预览 " +
      ((preview && preview.preview_rows) || 0) +
      " / 共 " +
      ((preview && preview.total_rows) || 0) +
      " 行 · 列数 " +
      cols.length;
  }

  function applyResult(data) {
    setStages(data.stages || []);
    var sliceOn = !!(data.slice_processing || data.cross_slice_bind_required);
    $("slice-hint").hidden = !sliceOn;

    if (data.status === "needs_user") {
      $("needs-user-panel").hidden = false;
      $("result-panel").hidden = true;
      state.priorFive = data.five_part || null;
      state.priorFailLocus = data.fail_locus || null;
      if (Array.isArray(data.ask_history)) {
        state.askHistory = data.ask_history;
      }
      renderFive(data.five_part || {});
      showBanner("info", "需要你确认后继续");
      return;
    }

    $("needs-user-panel").hidden = true;
    if (data.status === "plan_ready" && data.result_preview) {
      $("result-panel").hidden = false;
      renderTable(data.result_preview);
      var plan = data.plan_summary || {};
      $("plan-summary").textContent = JSON.stringify(plan, null, 2);
      var exp = data.export || {};
      var btn = $("btn-export");
      if (exp.download_url) {
        btn.hidden = false;
        btn.href = exp.download_url;
        btn.onclick = function (ev) {
          ev.preventDefault();
          fetch(exp.download_url, { headers: authHeaders() })
            .then(function (r) {
              if (!r.ok) throw new Error("download_failed");
              return r.blob();
            })
            .then(function (blob) {
              var a = document.createElement("a");
              a.href = URL.createObjectURL(blob);
              a.download = exp.filename || "query_result.xlsx";
              a.click();
              URL.revokeObjectURL(a.href);
            })
            .catch(function () {
              showBanner("fail", "导出失败或已过期");
            });
        };
      } else {
        btn.hidden = true;
      }
      showBanner("ok", "查询完成");
      return;
    }

    $("result-panel").hidden = true;
    showBanner("fail", "失败：" + (data.error || data.fail_locus || data.status || "?"));
  }

  function runQuery(payload) {
    showBanner("info", "查询进行中…（同步等待，请勿关闭）");
    setStages([{ name: "client", status: "running", detail: "waiting_server" }]);
    $("btn-run").disabled = true;
    $("btn-reply").disabled = true;
    return fetch("/api/query/run", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
    })
      .then(function (r) {
        return r.json().then(function (j) {
          if (!r.ok) throw j;
          return j;
        });
      })
      .then(function (data) {
        applyResult(data);
      })
      .catch(function (err) {
        showBanner("fail", "请求失败：" + ((err && err.error) || (err && err.detail) || "network"));
        setStages([{ name: "client", status: "failed", detail: "request_error" }]);
      })
      .finally(function () {
        $("btn-run").disabled = false;
        $("btn-reply").disabled = false;
      });
  }

  $("btn-upload").addEventListener("click", function () {
    var input = $("file-input");
    if (!input.files || !input.files.length) {
      showBanner("fail", "请先选择文件");
      return;
    }
    var fd = new FormData();
    Array.prototype.forEach.call(input.files, function (f) {
      fd.append("files", f, f.name);
    });
    showBanner("info", "上传中…");
    fetch("/api/query/upload", {
      method: "POST",
      headers: authHeaders(),
      body: fd,
    })
      .then(function (r) {
        return r.json().then(function (j) {
          if (!r.ok) throw j;
          return j;
        });
      })
      .then(function (data) {
        state.filePaths = data.file_paths || [];
        $("upload-hint").textContent =
          "已上传 " + state.filePaths.length + " 个文件";
        showBanner("ok", "上传成功");
      })
      .catch(function (err) {
        showBanner("fail", "上传失败");
        console.error(err);
      });
  });

  $("btn-run").addEventListener("click", function () {
    state.askHistory = [];
    state.priorFive = null;
    state.priorFailLocus = null;
    $("needs-user-panel").hidden = true;
    runQuery({
      utterance: $("utterance").value || "",
      file_paths: state.filePaths.slice(),
      ask_history: [],
    });
  });

  $("btn-reply").addEventListener("click", function () {
    var reply = ($("user-reply").value || "").trim();
    if (!reply) {
      showBanner("fail", "请填写确认内容");
      return;
    }
    runQuery({
      utterance: $("utterance").value || "",
      file_paths: state.filePaths.slice(),
      ask_history: state.askHistory.slice(),
      user_reply: reply,
      prior_five_part: state.priorFive,
      prior_fail_locus: state.priorFailLocus,
    });
  });
})();
