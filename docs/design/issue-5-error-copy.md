# Issue #5 — 前端 UX 错误文案刷新（Error Copy Refresh）

> Status: Draft (Step 2a, before `/design-check`).
> Owner: Method.
> Date: 2026-04-20.

## 1. Problem statement

真实用户在登录时看到 `alert("发送失败：rate_limit")` 这种字面英文错误码，体验明显是 bug。根因是后端返回 `{"error": "<machine_code>"}`、前端用 ``alert("xxx：" + body.error)`` 直接拼接渲染，把内部协议字段当文案展示给人看。同样的问题出现在登录验证、研究提交、评论提交、删除等多条路径。本 issue 把所有用户可见的错误文案集中归口到后端 `message` 字段，前端退化为「哑渲染」（`body.message || body.error || 通用文案`）。

## 2. Decisions

- **Q1 = A（已确认）**：后端在每个错误响应里同时返回 `error`（机器码，BC）和 `message`（中文文案）。后端是文案的单一事实源。
- **Q2 = B（已确认）**：全量扫描 `app/static/app.js` + `app/templates/*.html` 中所有 `alert(...)` 与 `body.error` / `data.error` 渲染点，统一收口；不仅限 TODO 列出的 3 处。
- **Q3 = B（已确认）**：后续若进入 dev loop，预算上限 10 iterations / 10 分钟。

## 3. Backend response shape

### 3.1 当前形态

历史上有两种错误体共存：

```jsonc
// 形态 A —— routers/auth.py、routers/research.py 大部分
{"error": "rate_limit"}

// 形态 B —— file_processor.LimitExceededError → routers/research.py 直接 bubble
{"code": "files_too_many", "message": "9 files exceeds the 8-per-request limit"}
```

形态 B 的 `message` 是英文调试串、不是面向用户的中文文案；并且 key 是 `code` 不是 `error`，与形态 A 不一致。

### 3.2 目标形态（after this issue）

所有 4xx/5xx JSON 错误体统一为：

```jsonc
{
  "error": "<machine_code>",       // BC：所有现有 body.error 读取者继续可用
  "message": "<面向用户的中文文案>", // 新增：前端首选渲染
  // 其他既有字段（如 request_id、retry_after）保持不变
}
```

**BC contract**：`body.error`（机器码）字段绝不被移除或改名。已有读取 `body.error` 的代码（如测试、第三方调用）继续工作。

**`file_processor.LimitExceededError` 收敛**：把 `detail = {"code": code, "message": <英文>}` 改为 `detail = {"error": code, "message": <中文>}`。`routers/research.py:121` 继续 bubble `exc.detail`，自动获得新 shape。`code` key 退场（无外部消费者；测试同步改）。

**HTTP status code 不变。** 不改语义、不改路由签名。

## 4. Error code → 中文 message 映射表

> 单一事实源：本表 + 实现中的 `app/services/error_copy.py`（新增模块）。新增错误码必须先在本表登记。

### 4.1 后端机器码映射

| Error code | HTTP | 出处（file:line） | 中文 message |
|---|---|---|---|
| `rate_limit` | 429 | auth.py:164 | 请求过于频繁，请稍后再试 |
| `mail_send_failed` | 503 | auth.py:170 | 验证码邮件发送失败，请稍后重试 |
| `bad_request` | 400 | auth.py:176 | 请求参数有误，请检查后重试 |
| `invalid_or_expired` | 400 | auth.py:197 | 验证码无效或已过期，请重新获取 |
| `unauthenticated` | 401 | auth.py:276 | 登录已过期，请刷新页面重新登录 |
| `bad_origin` | 403 | auth.py:289 | 请求来源校验失败，请刷新页面重试 |
| `empty_question` | 400 | research.py:101 | 请输入研究问题 |
| `question_too_long` | 400 | research.py:106 | 问题过长，请精简后再提交 |
| `invalid_mode` | 400 | research.py:111 | 研究模式不合法，请刷新页面重试 |
| `internal` | 500 | research.py:172, 540 | 服务器开小差了，请稍后重试 |
| `plan_missing` | 500 | research.py:364 | 方案文件缺失，请联系管理员 |
| `request_busy` | 409 | research.py:403 | 请求仍在处理中，请等它结束后再操作 |
| `request_not_finalized` | 409 | research.py:502 | 当前请求还在生成中，请等它结束再评论 |
| `anchor_text_invalid` | 400 | research.py:509 | 选中的原文不合法，请重新框选 |
| `body_invalid` | 400 | research.py:513 | 评论内容不符合要求（长度或格式），请修改后重试 |
| `anchor_context_too_long` | 400 | research.py:520 | 选中段落上下文过长，请缩短后重试 |
| `body_empty` | 400 | research.py:533 | 评论不能为空 |
| `ai_reply_not_deletable` | 403 | research.py:651 | AI 回复不能被删除 |
| `not_found` | 404 | research.py / history.py 多处 (HTTPException detail) | 记录不存在或已被删除 |
| `files_too_many` | 400 | file_processor.py:144 | 上传文件数超出单次 8 个的上限，请删减后重试 |
| `unsupported_type` | 400 | file_processor.py:154 | 文件类型不支持，请改为 md/txt/pdf/docx/pptx/xlsx/png/jpg/jpeg/webp/gif |
| `empty_file` | 400 | file_processor.py:160 | 上传的文件是空的（0 字节），请检查后重试 |
| `file_too_large` | 400 | file_processor.py:162 | 单个文件超过 50 MB 上限，请压缩或拆分后再上传 |
| `total_too_large` | 400 | file_processor.py:169 | 上传总大小超过限制，请删减后重试 |
| `mime_mismatch` | 400 | file_processor.py:191+ | 文件内容与扩展名不一致，请重新选择文件 |

### 4.2 前端兜底文案（与后端无关）

| Trigger | 中文文案 |
|---|---|
| 5xx 通用兜底（无 `message`、无 `error`） | 服务器开小差了，请稍后重试 |
| 网络异常 / fetch throw | 网络异常，请检查连接后重试 |
| 客户端文件类型/大小被前端预校验拒绝 | 复用 `rejectReason()` 已有中文（保持现状） |
| 复制到剪贴板失败 | 复制失败，请手动选择复制 |
| 删除请求失败、状态码非 204/404/409 | 删除失败 (`{status}`)，请稍后重试 |
| 评论提交失败、状态码非已枚举 | 提交失败 (`{status}`)，请稍后重试 |
| 任何 `body.message` 缺失但 HTTP 非 2xx 时的最终兜底 | 操作失败（`{status}`），请稍后重试 |

### 4.3 模板渲染的错误

| 模板 | 当前显示内容 | 中文化策略 |
|---|---|---|
| `templates/approval_error.html` | 链接无效或已过期 | 已是中文，保持不变（仅作为审计登记）|
| `templates/history_detail.html` 第 21 行 `{{ error_message }}` | 来源 = `research_requests.error_message` 字段 | **不在本 issue 范围内**——该字段由 `research_runner` / `claude_runner` 写入，是后台任务失败信息，需要单独的中文化（HARNESS 规则 1：error_message 不能为空）。本 issue 仅在前端 banner 兜底空值显示「研究失败，原因未知，请重试」（`{{ error_message or "研究失败，原因未知，请重试" }}`）。 |

## 5. Output files (产出文件)

> 本表驱动 Step 4（test-quality-check）的设计覆盖度 BLOCKING 检查。每行至少对应一个测试。

| Path | Change | What test must verify |
|---|---|---|
| `app/services/error_copy.py` | **新建**：导出 `ERROR_COPY: dict[str, str]`（机器码 → 中文）+ `message_for(code: str) -> str`（不存在时返回 `"操作失败，请稍后重试"`）。单一事实源。 | 单元：表里每一项 `error → message` 都能被 `message_for()` 命中；未知码返回兜底；表的 key 集合 = 设计文档 §4.1 的全集（防止漂移）。 |
| `app/routers/auth.py` | **修改**：`request_code` / `verify_code` 的 4 个 `JSONResponse(content={"error": ...})` 处，`content` 改为 `{"error": code, "message": message_for(code)}`；`install_exception_handlers` 中 `_Unauthenticated` / `_BadOrigin` 同样补 `message`。 | 集成：4 个失败路径分别断言 `body["error"] == <code>` AND `body["message"] == <设计文档对应中文>`。 |
| `app/routers/research.py` | **修改**：所有 11 处 `JSONResponse(content={"error": ...})` 补 `message_for(code)`；HTTPException(detail="not_found") 不动（细见 §7 注 1）。 | 集成：每个错误码至少 1 个测试断言 `error+message` 同时正确。`request_busy` 当前已硬编码中文，迁移到 `message_for("request_busy")`，断言一致。 |
| `app/routers/history.py` | **修改**：`HTTPException(404, detail="not_found")` 由路由层转换（细见 §7 注 1）。 | 集成：404 路径断言 JSON body 同时含 `error="not_found"` + `message="记录不存在或已被删除"`。 |
| `app/services/file_processor.py` | **修改**：`LimitExceededError.__init__(code, message)` → `super().__init__(status_code=400, detail={"error": code, "message": <中文>})`。把现有英文 message 替换为本设计 §4.1 的中文文案；不再使用 `code` key。`message` 参数保留传入，但当 caller 传入空串时回落到 `message_for(code)`。 | 单元：6 个 `LimitExceededError` 路径分别断言 `exc.detail == {"error": <code>, "message": <中文>}`。集成：上传 9 文件触发 `files_too_many`，断言 router 返回 body 是新 shape 而非 `code` key。 |
| `app/main.py` | **修改（仅在需要时）**：若新增 FastAPI 全局 `RequestValidationError` handler，把默认 422 映射成 `{"error": "validation_error", "message": "提交内容格式有误，请检查后重试"}`。**本 issue 暂不强制**——观察现有路由是否有 422 漏出 alert 路径（grep 结果无）即可，作为 §8 out-of-scope 备忘。 | 不需要新测试（不变更）。 |
| `app/static/app.js` | **修改**：新增顶部辅助函数 `showError(body, status, fallback?)`（§6）；将所有 `alert("xxx：" + body.error)` / `alert("xxx (" + r.status + ")")` 改用该 helper；`document.querySelector(".error-banner")` 渲染路径同步使用 helper 计算文案。共计 17 处 `alert(`（文件位 35/38/39/60/63/173/246/263/402/450/452/454/458/644/645/673/675/677/679/682/705/709，含纯客户端校验 alert，详见 §7）。 | 浏览器侧无单测，但通过后端集成测试 + 人工 smoke 覆盖（飞书 Step 2b 评审记录）。**Step 4 test-quality-check 的覆盖判定**：对 app.js 这一行采取「review-by-grep」校验——测试套件中加入一条 `tests/test_static_assets.py::test_no_raw_error_alert`，用正则 `alert\([^)]*\+\s*body\.error` 扫描 `app/static/app.js`，断言匹配数为 0；同理扫描 `data\.error`、`resBody\.error`。 |
| `app/templates/history_detail.html` | **修改**：第 21 行 `{{ error_message }}` 改为 `{{ error_message or "研究失败，原因未知，请重试" }}`。 | 模板渲染测试：`error_message=None` 时 banner 显示兜底中文；`error_message="某具体原因"` 时显示原值。 |
| `tests/test_error_copy.py` | **新建**：覆盖 `app/services/error_copy.py` 的字典完整性 + 兜底逻辑 + 与设计文档同步性（关键码必须在表中）。 | 见上。 |
| `tests/test_static_assets.py` | **新建**：grep 式断言 `app.js` 中没有 `alert(... + body.error)` / `alert(... + data.error)` / `alert(... + resBody.error)` 的拼接。L4 反向扫描的 CI 化版本。 | 见上。 |
| `tests/routers/test_auth_error_copy.py` | **新建（或扩 test_auth_routes.py）**：4 条 auth 失败路径的 `error+message` 双字段断言。 | 见上。 |
| `tests/routers/test_research_error_copy.py` | **新建（或扩 test_research_routes.py）**：每个 research 错误码 1 条断言。 | 见上。 |
| `tests/services/test_file_processor_error_copy.py` | **新建（或扩 test_file_processor.py）**：6 条 `LimitExceededError` 的 detail shape 断言。 | 见上。 |

> **共 13 行**（产出文件 9 个 + 测试文件 4 个）。

## 6. Frontend rendering rule

### 6.1 中央 helper（新加在 `app/static/app.js` 顶部，紧随 `postJson` 之后）

```js
// 唯一的错误展示函数。所有错误路径必须经过它。
// 优先级：body.message > body.error 的查表（兜底而非展示原码）> 通用兜底。
function showError(body, status, fallback) {
  body = body || {};
  // 1. 后端给了中文 message，直接用。
  if (body.message) { alert(body.message); return; }
  // 2. 后端只给了机器码（罕见、向后兼容）：不展示原码，用通用兜底。
  //    禁止 alert("xxx：" + body.error) —— 这是本 issue 要消灭的反模式。
  // 3. 调用方提供的 fallback；否则按 status 给出通用兜底。
  if (fallback) { alert(fallback); return; }
  if (status >= 500) { alert("服务器开小差了，请稍后重试"); return; }
  alert("操作失败 (" + (status || "网络") + ")，请稍后重试");
}

function showNetworkError() { alert("网络异常，请检查连接后重试"); }
```

> **设计意图**：即使后端某条路径漏写 `message`，前端也不会回退去拼 `body.error`——直接走兜底。这把「漏 message」从「用户看到英文码」降级为「用户看到通用文案」，符合 HARNESS 规则 1（错误不能静默；但用户体验侧不能比静默更糟）。

### 6.2 既有 banner 渲染点（index 页面 §242-263）

`app.js` 已经存在的模式 `body.message || body.error || ...` 是错误的（仍会拼 `error`）。改为：

```js
const msg = body.message || messageForCode(body.error) || "提交失败 (" + r.status + ")";
```

**或者**（更简单）：让 banner 也复用 `showError`，但写到 `errorBanner.textContent` 而不是 `alert`。建议做一个变体 `renderErrorTo(banner, body, status, fallback)`。

### 6.3 是否需要前端字典 `messageForCode`？

**结论：不需要**。后端 §4.1 的映射表是单一事实源，前端不应该 fork。所以前端的 `body.error` 分支只用于「后端没给 message 的兜底」——直接走通用文案即可。如未来真出现 BC 旧客户端也要兼容，再补字典。

## 7. Migration / sweep strategy（17 处 alert 全名单）

> 路径全部相对 `/home/ubuntu/method-dev/app/static/app.js`。

| Line | 当前代码 | 处理 |
|---|---|---|
| 35 | `alert("发送失败：" + (body.error || r.status))` | 替换为 `showError(body, r.status)` |
| 38 | `alert("该邮箱已被拒绝")` | 保持（纯文案）|
| 39 | `alert("网络错误，请稍后再试")` | 替换为 `showNetworkError()` |
| 60 | `alert("验证失败：" + (body.error || r.status))` | 替换为 `showError(body, r.status)` |
| 63 | `alert("网络错误，请稍后再试")` | 替换为 `showNetworkError()` |
| 173 | `alert(rejects.join("\n"))` | 保持（纯客户端预校验，已是中文）|
| 246 | `alert("请输入研究问题")` | 保持（纯客户端校验）|
| 263 | `alert(msg)` | 上游 `msg` 计算改用 `body.message || "提交失败 (..." `（不再拼 `body.error`）|
| 402 | `alert("复制失败，请手动选择")` | 保持（纯客户端）|
| 450 | `alert("请求仍在处理中…")` | 改为读取 `body.message`，因为后端已返回 `request_busy` 文案；保留 fallback。建议：`showError(body, 409, "请求仍在处理中，请等它结束后再删除")` |
| 452 | `alert("记录不存在或已被删除")` | 同上：`showError(body, 404, "记录不存在或已被删除")` |
| 454 | `alert("删除失败 (" + r.status + ")")` | `showError(body, r.status)` |
| 458 | `alert("网络错误，请稍后再试")` | `showNetworkError()` |
| 644 | `alert("评论不能为空")` | 保持（纯客户端）|
| 645 | `alert("请先选中方案里的一段文字")` | 保持（纯客户端）|
| 673 | `alert("提交失败：" + (resBody.error || r.status))` | `showError(resBody, r.status)` |
| 675 | `alert("当前请求还在生成中…")` | `showError(resBody, 409, "当前请求还在生成中，请等它结束再评论")` |
| 677 | `alert("登录已过期，请刷新页面")` | `showError(resBody, 401, "登录已过期，请刷新页面")` |
| 679 | `alert("提交失败 (" + r.status + ")")` | `showError(resBody, r.status)` |
| 682 | `alert("网络错误，请稍后再试")` | `showNetworkError()` |
| 705 | `alert("删除失败 (" + r.status + ")")` | `showError({}, r.status)` |
| 709 | `alert("网络错误")` | `showNetworkError()` |

**注 1（`HTTPException(detail="not_found")`）**：FastAPI 默认把这个序列化为 `{"detail": "not_found"}`——既不是 `error` 也不是 `message`。本 issue 在 `app/main.py` 增加一个 `HTTPException` exception handler：当 `exc.detail` 是 str 时包装为 `{"error": exc.detail, "message": message_for(exc.detail)}`；当是 dict（如 file_processor 已迁移后的 shape）时 bubble 不变。这条改动确保 history.py / research.py 的 12 处 `HTTPException(404)` 不需要逐个改。

**注 2**：评论 anchor_text 的纯客户端预校验（`alert("评论不能为空")`、`alert("请先选中…")`）保持原样——它们不读取 `body.error`，且文案已是中文，不属于本次 sweep 的目标反模式。

**注 3（反向扫描的 CI 化）**：`tests/test_static_assets.py` 持续 grep `alert\([^)]*\+\s*\w*\.error` 兜底未来回归。

## 8. Out of scope

- ❌ 把 `alert()` 全部换成 inline toast / banner 组件（Q2 的 C 选项已被否决；本次只动文案，不动 UI 形态）。
- ❌ 修改任何 HTTP status code 或错误码语义（仅补 `message`）。
- ❌ 重写 `research_runner` / `claude_runner` 写入 `error_message` 字段的细节（HARNESS 规则 1 已存在，本 issue 仅补前端兜底文案）。
- ❌ 邮件模板（`templates/emails/`）的中文化（已是中文）。
- ❌ 422 默认 validation 错误的统一中文化（grep 结果显示前端无 422 alert 路径——延后处理；登记到 `docs/TODO.md`）。
- ❌ 国际化框架（i18n / locale）——目前产品仅中文，不引入。

## 9. Risks

1. **BC 风险**：现有测试断言 `body == {"error": ...}` 严格相等而非包含关系，会因新增 `message` 字段失败。**对策**：先全仓 grep `assert.*== \{"error"` / `assertEqual.*\{"error"`，把这些断言改成 `assert body["error"] == ...` 风格；列入 Step 6 backup 清单。
2. **`file_processor` 切换 shape 风险**：`detail = {"code": ...}` → `detail = {"error": ...}` 是 BC-breaking 但只在仓内消费。**对策**：grep `\["code"\]` / `\.get\("code"\)` 全仓确认调用点（routers/research.py:121 是唯一一处 bubble，前端无消费），修改后跑全测试。
3. **「漏写 message」回归**：未来新增错误响应忘了带 `message`，前端走通用兜底但不会暴露原码——比静默更轻，但也是潜在 UX 退化。**对策**：在 `error_copy.ERROR_COPY` 字典里维护 codes 全集，加一个测试 `test_all_routes_use_known_codes`：grep 路由文件里所有 `"error": "<code>"` 字面量，断言 `<code>` 在字典里。

## 10. Category 14 self-check

> 对照 `~/.claude/skills/design-check/SKILL.md` Category 14 检查清单逐项确认。

- [x] **每个错误码都有中文 copy**：§4.1 表里 24 个后端机器码全部已配中文 message；§4.2 兜底场景 7 条全部配中文。
- [x] **copy 居所明确**：后端 = `app/services/error_copy.py::ERROR_COPY`（单一事实源），通过 router 的 `JSONResponse({"error": code, "message": message_for(code)})` 投递。前端 = 不维护字典，只在 `body.message` 缺失时走通用兜底（§6.3 决策）。
- [x] **明确禁止反模式 `alert("xxx：" + body.error)`**：§6.1 在 helper 注释、§7 sweep 表逐行替换、§5 新增 `tests/test_static_assets.py` grep 断言三层防护。
- [x] **覆盖表单校验 / 上传限制 / 文件类型拒绝 / 未登录跳转 / 网络兜底**：表单校验（empty_question / question_too_long / anchor_text_invalid / body_invalid / body_empty）已含；上传限制 6 条 `LimitExceededError` 已含；未登录走 `unauthenticated`；网络兜底 §4.2 + §6.1 `showNetworkError()`；邮件模板已中文化、out-of-scope 备忘。
- [x] **空状态文案**：`history.html` 已有「还没有研究记录」；`templates/history_detail.html` 失败 banner 兜底已加（§5）。其余空状态已是中文（comments-hint、empty 列表）。
- [x] **加载中文案**：`status-indicator` 已有「生成中 / 已完成 / 失败」中文，本 issue 不动。
- [x] **反向扫描**：grep `'{"error":'` 已扫描 `app/routers/` + `app/services/`（§4.1 列出所有 24 处）；grep `alert\(` 已扫描 `app/static/app.js`（§7 列出 22 行 17 处需迁移）。每个匹配点要么进入 §5 的产出文件改造列表，要么在 §7 注 2 显式说明保留。

**自检结论：Category 14 应可 PASS**（仍以 `/design-check` 实际执行结果为准）。
