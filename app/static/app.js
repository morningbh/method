/* Method frontend script — design §4.2.
   Pure ES2020, no modules. Per-page behaviour is feature-detected via
   DOM presence checks. */

(function () {
  "use strict";

  // ---------- Login page state machine ----------
  function postJson(url, obj) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(obj),
    });
  }

  // ---------- Centralised error renderer (Issue #5) ----------
  // Design §6.1: the ONLY path that turns a response body into a user-visible
  // string. Priority: body.message > caller fallback > generic by status.
  // Never renders body.error (the machine code) — that's the anti-pattern
  // Issue #5 eliminates.
  function showError(body, status, fallback) {
    body = body || {};
    if (body.message) { alert(body.message); return; }
    if (fallback) { alert(fallback); return; }
    if (status >= 500) { alert("服务器开小差了，请稍后重试"); return; }
    alert("操作失败 (" + (status || "网络") + ")，请稍后重试");
  }
  function showNetworkError() { alert("网络异常，请检查连接后重试"); }
  function initLogin() {
    const emailForm = document.getElementById("login-form");
    if (!emailForm) return;
    const codeForm = document.getElementById("code-form");
    const show = (state) => {
      document.querySelectorAll("[data-state]").forEach((el) => {
        el.hidden = el.getAttribute("data-state") !== state;
      });
    };
    emailForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const email = document.getElementById("email").value.trim();
      if (!email) return;
      const btn = emailForm.querySelector('button[type="submit"]');
      if (btn && btn.disabled) return;  // guard against double-submit race
      if (btn) btn.disabled = true;
      try {
        const r = await postJson("/api/auth/request_code", { email });
        const body = await r.json().catch(() => ({}));
        if (!r.ok) { showError(body, r.status); return; }
        if (body.status === "sent") show("code-sent");
        else if (body.status === "pending") show("pending");
        else if (body.status === "rejected") alert("该邮箱已被拒绝");
      } catch (e) { showNetworkError(); }
      finally { if (btn) btn.disabled = false; }
    });
    if (codeForm) {
      codeForm.addEventListener("submit", async (ev) => {
        ev.preventDefault();
        const email = document.getElementById("email").value.trim();
        const code = document.getElementById("code").value.trim();
        const btn = codeForm.querySelector('button[type="submit"]');
        // Hard guard against double-submit races: a successful verify
        // consumes the login code, so a concurrent second request always
        // fails with "invalid_or_expired" even though the first one
        // already set the session cookie — the user sees "验证失败" but
        // is actually logged in. Disable the button for the lifetime of
        // the request, and on success keep it disabled until navigation.
        if (btn && btn.disabled) return;
        if (btn) btn.disabled = true;
        try {
          const r = await postJson("/api/auth/verify_code", { email, code });
          if (r.ok) { window.location.assign("/"); return; }
          const body = await r.json().catch(() => ({}));
          showError(body, r.status);
          if (btn) btn.disabled = false;
        } catch (e) {
          showNetworkError();
          if (btn) btn.disabled = false;
        }
      });
    }
  }

  // ---------- Topbar logout ----------
  function initLogout() {
    const btn = document.getElementById("logout-btn");
    if (!btn) return;
    btn.addEventListener("click", async () => {
      try {
        await fetch("/api/auth/logout", { method: "POST" });
      } catch (e) { /* no-op */ }
      window.location.assign("/login");
    });
  }

  // ---------- Index workspace ----------
  const MAX_FILE_BYTES = 50 * 1024 * 1024;
  const IMAGE_SOFT_MAX_BYTES = 10 * 1024 * 1024;
  const ALLOWED_EXTS = [
    ".md", ".txt", ".pdf", ".docx", ".pptx", ".xlsx",
    ".png", ".jpg", ".jpeg", ".webp", ".gif",
  ];

  function extOf(name) {
    const i = name.lastIndexOf(".");
    return i < 0 ? "" : name.slice(i).toLowerCase();
  }
  function isImageFile(f) {
    return (f.type && f.type.startsWith("image/")) ||
      [".png", ".jpg", ".jpeg", ".webp", ".gif"].includes(extOf(f.name || ""));
  }
  function fmtMB(n) { return (n / 1024 / 1024).toFixed(1) + " MB"; }

  // Returns null if acceptable, else a user-facing rejection string.
  function rejectReason(f) {
    const ext = extOf(f.name || "");
    if (ext && !ALLOWED_EXTS.includes(ext)) {
      return `不支持的文件类型：${f.name}（仅 ${ALLOWED_EXTS.join(" / ")}）`;
    }
    if (f.size > MAX_FILE_BYTES) {
      return `${f.name} 有 ${fmtMB(f.size)}，超过单文件 50 MB 上限`;
    }
    return null;
  }
  function softWarn(f) {
    if (isImageFile(f) && f.size > IMAGE_SOFT_MAX_BYTES) {
      return `图片 ${f.name} 有 ${fmtMB(f.size)}（建议 ≤ 10 MB），继续上传？`;
    }
    return null;
  }

  function renderChips(files, container, onRemove) {
    container.innerHTML = "";
    files.forEach((f, idx) => {
      const li = document.createElement("li");
      if (isImageFile(f)) li.classList.add("chip-image");
      // Optional thumbnail for images.
      if (isImageFile(f) && typeof URL !== "undefined" && URL.createObjectURL) {
        const img = document.createElement("img");
        img.className = "chip-thumb";
        img.src = URL.createObjectURL(f);
        img.alt = "";
        img.onload = () => { try { URL.revokeObjectURL(img.src); } catch (_) {} };
        li.appendChild(img);
      }
      const name = document.createElement("span");
      name.textContent = f.name;
      const x = document.createElement("button");
      x.type = "button";
      x.textContent = "\u00d7";
      x.addEventListener("click", () => onRemove(idx));
      li.appendChild(name);
      li.appendChild(x);
      container.appendChild(li);
    });
  }

  function initIndex() {
    const form = document.getElementById("research-form");
    if (!form) return;
    const fileInput = document.getElementById("file-input");
    const dropZone = document.getElementById("drop-zone");
    const chipContainer = document.getElementById("chips");
    const pickBtn = document.getElementById("pick-files");
    const errorBanner = document.getElementById("submit-error");
    let files = [];

    function rerenderChips() {
      renderChips(files, chipContainer, (idx) => {
        files.splice(idx, 1);
        rerenderChips();
      });
    }

    // Add with client-side size / extension gate + soft-limit prompt. Returns
    // number of files accepted.
    function addFiles(candidates) {
      const rejects = [];
      const accepted = [];
      for (const f of candidates) {
        const reason = rejectReason(f);
        if (reason) { rejects.push(reason); continue; }
        const warn = softWarn(f);
        if (warn && !window.confirm(warn)) continue;
        accepted.push(f);
      }
      if (rejects.length) alert(rejects.join("\n"));
      if (accepted.length) {
        files = files.concat(accepted);
        rerenderChips();
      }
      return accepted.length;
    }

    if (pickBtn && fileInput) {
      pickBtn.addEventListener("click", () => fileInput.click());
    }
    if (fileInput) {
      fileInput.addEventListener("change", (e) => {
        addFiles(Array.from(e.target.files));
        fileInput.value = ""; // allow re-picking same file
      });
    }

    if (dropZone && window.innerWidth >= 768) {
      dropZone.addEventListener("dragover", (e) => {
        e.preventDefault();
        dropZone.classList.add("over");
      });
      dropZone.addEventListener("dragleave", () => {
        dropZone.classList.remove("over");
      });
      dropZone.addEventListener("drop", (e) => {
        e.preventDefault();
        dropZone.classList.remove("over");
        addFiles(Array.from(e.dataTransfer.files));
      });
    }

    // Clipboard paste: capture image blobs and treat them as uploads. Plain
    // text paste is left untouched (no preventDefault in that branch).
    function pasteHandler(e) {
      const cd = e.clipboardData;
      if (!cd || !cd.items) return;
      const imgs = [];
      for (const it of cd.items) {
        if (it.kind === "file" && it.type && it.type.startsWith("image/")) {
          const blob = it.getAsFile();
          if (!blob) continue;
          const ext = (it.type.split("/")[1] || "png").split("+")[0];
          const name = blob.name && blob.name !== "image.png"
            ? blob.name
            : `screenshot-${new Date().toISOString().replace(/[:.]/g, "-")}.${ext}`;
          // File is a proper File constructor, widely supported in modern browsers.
          try {
            imgs.push(new File([blob], name, { type: it.type }));
          } catch (_) {
            // Fallback: use the blob directly; addFiles handles it via .size/.name.
            try { Object.defineProperty(blob, "name", { value: name }); } catch (_) {}
            imgs.push(blob);
          }
        }
      }
      if (imgs.length) {
        e.preventDefault();
        addFiles(imgs);
      }
    }
    // Listen at document level so paste works whether focus is in the textarea
    // or anywhere on the page.
    document.addEventListener("paste", pasteHandler);

    const textarea = document.querySelector("[data-autofocus]");
    if (textarea && window.innerWidth >= 768) textarea.focus();

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (errorBanner) { errorBanner.hidden = true; errorBanner.textContent = ""; }
      const q = document.getElementById("question").value.trim();
      if (!q) { alert("请输入研究问题"); return; }
      const fd = new FormData();
      fd.append("question", q);
      const modeEl = form.querySelector('input[name="mode"]:checked');
      fd.append("mode", modeEl ? modeEl.value : "general");
      for (const f of files) fd.append("files", f);
      const submitBtn = form.querySelector('button[type="submit"]');
      if (submitBtn) submitBtn.disabled = true;
      try {
        const r = await fetch("/api/research", { method: "POST", body: fd });
        const body = await r.json().catch(() => ({}));
        if (r.ok && body.request_id) {
          window.location.assign("/history/" + body.request_id);
          return;
        }
        const msg = body.message || ("提交失败 (" + r.status + ")，请稍后重试");
        if (errorBanner) { errorBanner.textContent = msg; errorBanner.hidden = false; }
        else showError(body, r.status);
      } catch (err) {
        if (errorBanner) { errorBanner.textContent = "网络异常，请检查连接后重试"; errorBanner.hidden = false; }
      } finally {
        if (submitBtn) submitBtn.disabled = false;
      }
    });
  }

  // ---------- History detail SSE client ----------
  function setStatus(status, message) {
    const el = document.querySelector(".status-indicator");
    if (!el) return;
    el.className = "status-indicator status-" + status;
    if (status === "done") el.textContent = "\u25cf 已完成";
    else if (status === "failed") {
      el.textContent = "\u2717 失败";
      if (message) {
        let banner = document.querySelector(".error-banner");
        if (!banner) {
          banner = document.createElement("div");
          banner.className = "error-banner";
          const row = document.querySelector(".status-row");
          if (row && row.parentNode) row.parentNode.insertBefore(banner, row.nextSibling);
        }
        banner.textContent = message;
      }
    }
    if (status === "done") {
      const copy = document.getElementById("copy-btn");
      if (copy) copy.disabled = false;
      const dl = document.getElementById("download-btn");
      if (dl) { dl.removeAttribute("aria-disabled"); dl.removeAttribute("tabindex"); }
    }
  }

  function renderMarkdown(md) {
    const body = document.getElementById("markdown-body");
    if (!body) return;
    if (md) {
      // Store the raw source on the element so comment anchoring can find
      // the absolute character offset of a selection back in the markdown.
      body.setAttribute("data-markdown-source", md);
    }
    if (md && window.marked && typeof window.marked.parse === "function") {
      body.innerHTML = window.marked.parse(md, {
        gfm: true, breaks: false, headerIds: false, mangle: false,
      });
    } else if (md) {
      const pre = document.createElement("pre");
      pre.textContent = md;
      body.innerHTML = "";
      body.appendChild(pre);
    }
  }

  function fetchAndRender(requestId) {
    fetch("/api/research/" + requestId)
      .then((r) => r.json())
      .then((data) => {
        if (data.markdown) renderMarkdown(data.markdown);
      })
      .catch(() => { /* no-op */ });
  }

  function pollForResult(requestId, attempts) {
    attempts = attempts || 0;
    if (attempts > 100) { setStatus("failed", "polling gave up"); return; }
    fetch("/api/research/" + requestId)
      .then((r) => r.json())
      .then((data) => {
        if (data.status === "done") {
          fetchAndRender(requestId);
          setStatus("done");
        } else if (data.status === "failed") {
          setStatus("failed", data.error_message || "unknown error");
        } else {
          setTimeout(() => pollForResult(requestId, attempts + 1), 3000);
        }
      })
      .catch(() => setTimeout(() => pollForResult(requestId, attempts + 1), 3000));
  }

  function connectSSE(requestId) {
    const es = new EventSource("/api/research/" + requestId + "/stream");
    let closed = false;
    let buffer = "";
    es.addEventListener("delta", (ev) => {
      try {
        const data = JSON.parse(ev.data);
        buffer += data.text || "";
        renderMarkdown(buffer);
      } catch (e) { /* no-op */ }
    });
    es.addEventListener("done", (ev) => {
      closed = true;
      es.close();
      fetchAndRender(requestId);
      setStatus("done");
    });
    es.addEventListener("error", (ev) => {
      if (ev && ev.data) {
        closed = true;
        es.close();
        try {
          const data = JSON.parse(ev.data);
          setStatus("failed", data.message || "unknown error");
        } catch (e) {
          setStatus("failed", "unknown error");
        }
      }
    });
    es.onerror = () => {
      if (!closed && es.readyState === EventSource.CLOSED) {
        es.close();
        setTimeout(() => pollForResult(requestId), 2000);
      }
    };
  }

  function initDetail() {
    const detail = document.querySelector(".detail");
    if (!detail) return;
    const requestId = detail.getAttribute("data-request-id");
    const initial = detail.getAttribute("data-initial-status");
    if (initial === "pending" || initial === "running") {
      connectSSE(requestId);
    } else if (initial === "done") {
      fetchAndRender(requestId);
    }
    const copyBtn = document.getElementById("copy-btn");
    if (copyBtn) {
      copyBtn.addEventListener("click", () => {
        const body = document.getElementById("markdown-body");
        if (!body) return;
        const text = body.innerText;
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(
            () => { copyBtn.textContent = "已复制"; setTimeout(() => { copyBtn.textContent = "复制"; }, 1500); },
            () => { alert("复制失败，请手动选择"); }
          );
        } else {
          const range = document.createRange();
          range.selectNodeContents(body);
          const sel = window.getSelection();
          sel.removeAllRanges();
          sel.addRange(range);
        }
      });
    }
  }

  // ---------- History list: delete button ----------
  function initHistoryDelete() {
    const list = document.querySelector(".history-list .cards");
    if (!list) return;
    list.addEventListener("click", async (ev) => {
      const btn = ev.target.closest(".card-delete");
      if (!btn) return;
      ev.preventDefault();
      ev.stopPropagation();
      const rid = btn.getAttribute("data-request-id");
      if (!rid) return;
      if (!window.confirm("确定要删除这条研究记录吗？此操作不可撤销。")) return;
      btn.disabled = true;
      try {
        const r = await fetch("/api/research/" + encodeURIComponent(rid), {
          method: "DELETE",
        });
        if (r.status === 204) {
          const row = btn.closest(".card-row");
          if (row) {
            row.classList.add("removing");
            setTimeout(() => {
              row.remove();
              const remaining = list.querySelectorAll(".card-row").length;
              if (remaining === 0) {
                const empty = document.createElement("p");
                empty.className = "empty";
                empty.textContent = "还没有研究记录";
                list.replaceWith(empty);
              }
            }, 200);
          }
          return;
        }
        const body = await r.json().catch(() => ({}));
        if (r.status === 409) {
          showError(body, 409, "请求仍在处理中，请等它结束后再操作");
        } else if (r.status === 404) {
          showError(body, 404, "记录不存在或已被删除");
        } else {
          showError(body, r.status, "删除失败 (" + r.status + ")，请稍后重试");
        }
        btn.disabled = false;
      } catch (e) {
        showNetworkError();
        btn.disabled = false;
      }
    });
  }

  // ---------- History detail: selection-anchored comments ----------
  function initComments() {
    const detail = document.querySelector(".detail");
    if (!detail) return;
    const requestId = detail.getAttribute("data-request-id");
    const listEl = document.getElementById("comment-list");
    const composeDialog = document.getElementById("comment-compose");
    const tool = document.getElementById("selection-tool");
    if (!listEl || !composeDialog || !tool) return;

    const ANCHOR_CONTEXT_LEN = 50;

    function fmtAbsTime(iso) {
      if (!iso) return "";
      try {
        const d = new Date(iso);
        return d.toLocaleString();
      } catch (_) { return iso; }
    }

    function escapeHTML(s) {
      return (s || "").replace(/[&<>"']/g, function (c) {
        return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c];
      });
    }

    function renderCommentCard(item) {
      const li = document.createElement("li");
      li.className = "comment-card";
      li.setAttribute("data-cid", item.id);
      const ai = item.ai_reply;
      li.innerHTML =
        '<blockquote class="excerpt">' + escapeHTML(item.anchor_text) + "</blockquote>" +
        '<div class="user-comment">' +
          '<span class="author">你</span>' +
          '<span class="time">' + escapeHTML(fmtAbsTime(item.created_at)) + "</span>" +
          '<p class="body">' + escapeHTML(item.body) + "</p>" +
          '<button type="button" class="delete-btn" aria-label="删除" title="删除">×</button>' +
        "</div>" +
        '<div class="ai-reply" data-status="' + escapeHTML((ai && ai.ai_status) || "pending") + '" data-ai-cid="' + escapeHTML((ai && ai.id) || "") + '">' +
          '<span class="author">AI 评论员</span>' +
          '<p class="body">' + escapeHTML((ai && ai.body) || (ai && ai.ai_status === "failed" ? ("[失败] " + (ai.ai_error || "")) : "思考中...")) + "</p>" +
        "</div>";
      return li;
    }

    function appendCommentCard(item) {
      listEl.appendChild(renderCommentCard(item));
      if (item.ai_reply && (item.ai_reply.ai_status === "pending" || item.ai_reply.ai_status === "streaming")) {
        subscribeAIReply(item.ai_reply.id);
      }
    }

    function loadComments() {
      return fetch("/api/research/" + encodeURIComponent(requestId) + "/comments")
        .then((r) => r.ok ? r.json() : { comments: [] })
        .then((data) => {
          listEl.innerHTML = "";
          (data.comments || []).forEach(appendCommentCard);
        })
        .catch(() => { /* no-op */ });
    }

    function subscribeAIReply(aiCid) {
      if (!aiCid) return;
      const url = "/api/research/" + encodeURIComponent(requestId) +
                  "/comments/stream?comment_id=" + encodeURIComponent(aiCid);
      const es = new EventSource(url);
      const card = listEl.querySelector('.ai-reply[data-ai-cid="' + aiCid + '"]');
      let buffer = (card && card.querySelector(".body").textContent) || "";
      // Reset to empty for streaming accumulation (placeholder was "思考中...").
      if (card) { card.setAttribute("data-status", "streaming"); card.querySelector(".body").textContent = ""; buffer = ""; }
      es.addEventListener("ai_delta", (ev) => {
        try {
          const d = JSON.parse(ev.data);
          buffer += d.text || "";
          if (card) card.querySelector(".body").textContent = buffer;
        } catch (_) { /* no-op */ }
      });
      es.addEventListener("ai_done", (ev) => {
        es.close();
        try {
          const d = JSON.parse(ev.data);
          if (card) {
            card.setAttribute("data-status", d.ai_status || "done");
            if (d.ai_status === "failed") {
              card.querySelector(".body").textContent = "[失败] " + (d.ai_error || "unknown");
            } else if (d.body) {
              card.querySelector(".body").textContent = d.body;
            }
          }
        } catch (_) { /* no-op */ }
      });
      es.onerror = () => { es.close(); };
    }

    // ---------- Selection detection ----------
    const sources = []; // {el, source, kind}
    function registerSource(el, kind) {
      if (!el) return;
      // Populate data-markdown-source with the current text if not set; JS
      // will also update when renderMarkdown sets the plan body.
      if (!el.getAttribute("data-markdown-source")) {
        el.setAttribute("data-markdown-source", el.textContent || "");
      }
      sources.push({ el: el, kind: kind });
    }
    registerSource(document.getElementById("markdown-body"), "plan");
    document.querySelectorAll(".error-banner[data-markdown-source]").forEach((el) => registerSource(el, "error"));

    let currentSelection = null; // { text, before, after, sourceEl }

    function captureSelection() {
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed) { hideTool(); return; }
      const text = sel.toString();
      if (!text || !text.trim()) { hideTool(); return; }
      const range = sel.getRangeAt(0);
      // Find which registered source contains the selection.
      let sourceRec = null;
      for (const s of sources) {
        if (s.el && s.el.contains(range.commonAncestorContainer)) { sourceRec = s; break; }
      }
      if (!sourceRec) { hideTool(); return; }
      // Compute before/after from the DOM text content as a reasonable proxy
      // for the markdown source. This is a best-effort anchor; v1 does not
      // require exact offset reversal.
      const sourceText = sourceRec.el.getAttribute("data-markdown-source") || sourceRec.el.textContent || "";
      const idx = sourceText.indexOf(text);
      let before = "", after = "";
      if (idx >= 0) {
        before = sourceText.slice(Math.max(0, idx - ANCHOR_CONTEXT_LEN), idx);
        after = sourceText.slice(idx + text.length, idx + text.length + ANCHOR_CONTEXT_LEN);
      }
      currentSelection = { text: text, before: before, after: after, sourceEl: sourceRec.el };
      showTool(range);
    }
    function showTool(range) {
      const rect = range.getBoundingClientRect();
      tool.hidden = false;
      tool.classList.remove("hidden");
      tool.style.top = (window.scrollY + rect.top - tool.offsetHeight - 8) + "px";
      tool.style.left = (window.scrollX + rect.left) + "px";
    }
    function hideTool() {
      tool.hidden = true;
      tool.classList.add("hidden");
    }
    document.addEventListener("mouseup", () => setTimeout(captureSelection, 0));
    document.addEventListener("selectionchange", () => {
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed) hideTool();
    });

    // ---------- Compose dialog ----------
    const addBtn = document.getElementById("add-comment-btn");
    const bodyInput = composeDialog.querySelector(".comment-body-input");
    const excerptEl = composeDialog.querySelector(".selected-excerpt");
    const cancelBtn = composeDialog.querySelector(".cancel");
    const submitBtn = composeDialog.querySelector(".submit");
    const form = composeDialog.querySelector("form");

    function openCompose() {
      if (!currentSelection) return;
      excerptEl.textContent = currentSelection.text;
      bodyInput.value = "";
      if (typeof composeDialog.showModal === "function") composeDialog.showModal();
      else composeDialog.setAttribute("open", "");
      setTimeout(() => bodyInput.focus(), 50);
      hideTool();
    }
    function closeCompose() {
      if (typeof composeDialog.close === "function") composeDialog.close();
      else composeDialog.removeAttribute("open");
    }
    if (addBtn) addBtn.addEventListener("click", openCompose);
    if (cancelBtn) cancelBtn.addEventListener("click", (e) => { e.preventDefault(); closeCompose(); });
    if (form) form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = (bodyInput.value || "").trim();
      if (!body) { alert("评论不能为空"); return; }
      if (!currentSelection) { alert("请先选中方案里的一段文字"); return; }
      submitBtn.disabled = true;
      try {
        const r = await fetch("/api/research/" + encodeURIComponent(requestId) + "/comments", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            anchor_before: currentSelection.before || "",
            anchor_text: currentSelection.text,
            anchor_after: currentSelection.after || "",
            body: body,
          }),
        });
        const resBody = await r.json().catch(() => ({}));
        if (r.status === 201) {
          const userObj = resBody.comment || resBody.user_comment;
          const ai = resBody.ai_placeholder;
          appendCommentCard({
            id: userObj.id,
            author: "user",
            anchor_text: userObj.anchor_text,
            body: userObj.body,
            created_at: userObj.created_at,
            ai_reply: ai,
          });
          closeCompose();
          currentSelection = null;
        } else if (r.status === 400) {
          showError(resBody, r.status);
        } else if (r.status === 409) {
          showError(resBody, 409, "当前请求还在生成中，请等它结束再评论");
        } else if (r.status === 401) {
          showError(resBody, 401, "登录已过期，请刷新页面重新登录");
        } else {
          showError(resBody, r.status, "提交失败 (" + r.status + ")，请稍后重试");
        }
      } catch (_) {
        showNetworkError();
      } finally {
        submitBtn.disabled = false;
      }
    });

    // ---------- Delete ----------
    listEl.addEventListener("click", async (ev) => {
      const btn = ev.target.closest(".delete-btn");
      if (!btn) return;
      const card = btn.closest(".comment-card");
      const cid = card && card.getAttribute("data-cid");
      if (!cid) return;
      if (!window.confirm("删除这条评论？AI 回复也会一起删掉，不可恢复。")) return;
      btn.disabled = true;
      try {
        const r = await fetch("/api/research/" + encodeURIComponent(requestId) +
                              "/comments/" + encodeURIComponent(cid),
                              { method: "DELETE" });
        if (r.status === 204) {
          card.classList.add("removing");
          setTimeout(() => card.remove(), 200);
        } else {
          const body = await r.json().catch(() => ({}));
          showError(body, r.status, "删除失败 (" + r.status + ")，请稍后重试");
          btn.disabled = false;
        }
      } catch (_) {
        showNetworkError();
        btn.disabled = false;
      }
    });

    // Initial load.
    loadComments();
  }

  function init() {
    initLogin();
    initLogout();
    initIndex();
    initDetail();
    initHistoryDelete();
    initComments();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
