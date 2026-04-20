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
        if (!r.ok) { alert("发送失败：" + (body.error || r.status)); return; }
        if (body.status === "sent") show("code-sent");
        else if (body.status === "pending") show("pending");
        else if (body.status === "rejected") alert("该邮箱已被拒绝");
      } catch (e) { alert("网络错误，请稍后再试"); }
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
          alert("验证失败：" + (body.error || r.status));
          if (btn) btn.disabled = false;
        } catch (e) {
          alert("网络错误，请稍后再试");
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
        const msg = body.message || body.error || ("提交失败 (" + r.status + ")");
        if (errorBanner) { errorBanner.textContent = msg; errorBanner.hidden = false; }
        else alert(msg);
      } catch (err) {
        if (errorBanner) { errorBanner.textContent = "网络错误"; errorBanner.hidden = false; }
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
        if (r.status === 409) {
          alert("请求仍在处理中，请等它结束后再删除");
        } else if (r.status === 404) {
          alert("记录不存在或已被删除");
        } else {
          alert("删除失败 (" + r.status + ")");
        }
        btn.disabled = false;
      } catch (e) {
        alert("网络错误，请稍后再试");
        btn.disabled = false;
      }
    });
  }

  function init() {
    initLogin();
    initLogout();
    initIndex();
    initDetail();
    initHistoryDelete();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
