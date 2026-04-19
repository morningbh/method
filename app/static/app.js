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
      try {
        const r = await postJson("/api/auth/request_code", { email });
        const body = await r.json().catch(() => ({}));
        if (!r.ok) { alert("发送失败：" + (body.error || r.status)); return; }
        if (body.status === "sent") show("code-sent");
        else if (body.status === "pending") show("pending");
        else if (body.status === "rejected") alert("该邮箱已被拒绝");
      } catch (e) { alert("网络错误，请稍后再试"); }
    });
    if (codeForm) {
      codeForm.addEventListener("submit", async (ev) => {
        ev.preventDefault();
        const email = document.getElementById("email").value.trim();
        const code = document.getElementById("code").value.trim();
        try {
          const r = await postJson("/api/auth/verify_code", { email, code });
          if (r.ok) { window.location.assign("/"); return; }
          const body = await r.json().catch(() => ({}));
          alert("验证失败：" + (body.error || r.status));
        } catch (e) { alert("网络错误，请稍后再试"); }
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
  function renderChips(files, container, onRemove) {
    container.innerHTML = "";
    files.forEach((f, idx) => {
      const li = document.createElement("li");
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

    if (pickBtn && fileInput) {
      pickBtn.addEventListener("click", () => fileInput.click());
    }
    if (fileInput) {
      fileInput.addEventListener("change", (e) => {
        files = files.concat(Array.from(e.target.files));
        rerenderChips();
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
        files = files.concat(Array.from(e.dataTransfer.files));
        rerenderChips();
      });
    }

    const textarea = document.querySelector("[data-autofocus]");
    if (textarea && window.innerWidth >= 768) textarea.focus();

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (errorBanner) { errorBanner.hidden = true; errorBanner.textContent = ""; }
      const q = document.getElementById("question").value.trim();
      if (!q) { alert("请输入研究问题"); return; }
      const fd = new FormData();
      fd.append("question", q);
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

  function init() {
    initLogin();
    initLogout();
    initIndex();
    initDetail();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
