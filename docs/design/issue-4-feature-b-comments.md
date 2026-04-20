# Issue #4 — 功能 B：选文评论 + AI 回复（v2）

Date: 2026-04-20
Feishu 审核版（canonical review artifact）：https://www.feishu.cn/docx/O37Ndd5BRofarhx40X5cDhPBnpg

> 范围：在研究方案详情页支持用户对方案选段评论，AI 自动回复；v2 是 2026-04-20 飞书评审后吸收 5 条反馈的版本，用户已批准。

---

## 1. 已定产品决策

| 项 | 选定 | 含义 |
|---|---|---|
| B-Q1 展示 | C | 两阶段：MVP-1 方案底部评论区；后续 MVP 叠加原文高亮 + 旁注 |
| B-Q2 AI 触发 | X | 每条用户评论自动触发 AI 回复 |
| B-Q3 AI 流式 | A | 流式（SSE 打字机） |
| B-Q4 删除 | A | 用户删自己评论时级联删 AI 回复；AI 回复不能单独删 |
| B-Q5 节流 | D | 先不设；上线观察 1-2 周再补 |
| B-Q6 AI 上下文 | A | 全套：原始问题 + 上传文件绝对路径列表 + 方案 markdown + 选中文本 + 用户评论 |
| B-Q7 failed 方案评论 | A | done + failed 都开放评论；失败方案的 AI 回复充当 bug report + 自我诊断入口 |
| B-Q8 模型 | 系统默认（Opus） | 不降级到 Haiku；评论是 skill 进化的燃料，质量优先；保留 `CLAUDE_COMMENT_MODEL` env 逃生门 |
| B-Q9 注销时数据 | 软删、30 天可恢复 | 用户注销时所有方案 + 评论软删；admin 可在 30 天内恢复；30 天后硬删（联动功能 A8） |

---

## 2. 数据模型

新增一张表 `comments`。SQLite 首次启动 `CREATE TABLE IF NOT EXISTS`（init_db 已有这机制），不影响现有数据。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | TEXT (ULID) | PK | 26 字符 Crockford base32 |
| request_id | TEXT | NOT NULL, FK → research_requests.id | 评论所属方案 |
| user_id | INT | NOT NULL, FK → users.id | 评论作者（AI 评论时仍填方案作者，author 字段区分） |
| parent_id | TEXT | nullable, FK → comments.id | AI 回复指向用户评论；用户评论为 NULL |
| author | TEXT | NOT NULL, CHECK IN ('user', 'ai') | |
| anchor_text | TEXT | NOT NULL, 1..2000 字符 | 用户选中的原文 |
| anchor_before | TEXT | NOT NULL, 最多 50 字符 | 锚定前缀（quote-based 锚定） |
| anchor_after | TEXT | NOT NULL, 最多 50 字符 | 锚定后缀 |
| body | TEXT | NOT NULL, 1..2000 字符 | 评论正文（纯文本；不走 markdown 渲染，防 XSS） |
| ai_status | TEXT | CHECK IN ('pending', 'streaming', 'done', 'failed'); 仅 author='ai' 非 NULL | |
| ai_error | TEXT | nullable | AI 失败原因（HARNESS §1：失败不可静默） |
| cost_usd | REAL | nullable | AI 回复本次 API 成本（`total_cost_usd`）；便于观察总支出 |
| created_at | DATETIME | NOT NULL | naive UTC（沿用项目 `_utcnow()` 约定） |
| deleted_at | DATETIME | nullable | 软删标记；naive UTC；所有列表查询过滤 `deleted_at IS NULL` |

**索引**：
- `idx_comments_request_created` on `(request_id, created_at)` — 按方案列表用
- `idx_comments_parent` on `(parent_id)` — 取某条用户评论的 AI 回复用

**GET 查询形态（避免 N+1）**：列出某方案评论必须用**单次 SELECT 全量拉所有用户评论 + AI 回复行**（`WHERE request_id = :rid AND deleted_at IS NULL`，一条 SQL），然后在 Python 内存里按 `parent_id` 拼装嵌套结构；禁止每条用户评论单独查一次 AI 回复。

**级联语义**（产品层面，非 SQL CASCADE）：DELETE 用户评论 → 该评论 + 它的 AI 回复都 `deleted_at = now()`；物理行保留。

**注销账号连锁**：归功能 A8 负责；本功能对 A8 的约束是"查询必须过滤 `deleted_at IS NULL`"。

---

## 3. 文本锚定机制

### 锚定三元组

`(anchor_before, anchor_text, anchor_after)` —— 用户选择的原文 + 其前后各 50 字符上下文。匹配时通过上下文唯一化，避免同文本多处出现的歧义。

### 前端选区 → 三元组

1. 监听 `.markdown-body` 的 `mouseup` / `selectionchange` 事件
2. 取 `window.getSelection().toString()` 作为 `anchor_text`；若为空、或跨越 `.markdown-body` 外的节点，忽略
3. 通过 Selection 的 startContainer/offset 反推：在方案 markdown 源文本（通过 `data-markdown-source` 注入到 DOM）里定位该文本的绝对偏移
4. 取偏移前 50 字符 / 后 50 字符作为 before/after
5. 在选区附近渲染浮动"💬 评论"按钮

### 特殊分支：failed 方案（B-Q7=A）

- failed 方案没有 `plan_markdown`，只有 `error_message`
- 前端展示 failed 状态时，把 `error_message` 也塞进一个 `.error-banner[data-markdown-source]` 区域，允许用户对错误信息选段评论
- 这种评论的 `anchor_text` 可能来自 error_message 而不是 plan_markdown；后端存的时候不区分来源，统一流程；prompt 模板里把"发生了什么"清楚地给 AI 看

### 后端持久化

只存三元组。MVP-1 不做偏移校验；MVP-3（行内高亮）时再用三元组在 DOM 里 TreeWalker 匹配、`<mark data-comment-id>` 包裹。

---

## 4. API 设计

全部要求 `require_user` + `verify_origin`（CSRF）。

### `POST /api/research/{rid}/comments`

创建用户评论。请求体：

```json
{ "anchor_before": "...", "anchor_text": "...", "anchor_after": "...", "body": "..." }
```

**校验**：

- `rid` owner scope（不是作者返回 404，无枚举 oracle — 同 DELETE /api/research 风格）
- `anchor_text` 1..2000 字符
- `body` 1..2000 字符
- `anchor_before` / `anchor_after` 各最多 50 字符
- `research_requests.status` 必须 ∈ {`done`, `failed`} —— `pending`/`running` 返回 `409 request_not_finalized`

**流程**：

1. **单事务**内落用户评论行（author=user，ai_status=NULL）+ 占位 AI 回复行（parent_id 指向用户评论，author=ai，ai_status=pending，body=''）。两行必须同生同灭；若写失败，返回 `500 {"error":"internal"}` 且两行都不落库
2. 事务提交后，用 `asyncio.create_task(...)` 启动 AI 生成；task 加进 `comment_runner._TASKS: set[asyncio.Task]` 持有强引用防 GC，done_callback 负责从 set 摘除 + 记录异常（复用 `research_runner` 同款模式）
3. 返回 `201`：`{ comment: {...用户评论完整字段...}, ai_placeholder: {...pending AI 占位完整字段...} }` —— 字段结构与 GET 响应的评论条目一致

### `DELETE /api/research/{rid}/comments/{cid}`

软删。只能删 `author=user` 的自己的评论。

**级联**：同一事务内，把 `parent_id == cid AND author='ai'` 的行也置 `deleted_at`。

**响应**：`204`。

**不允许**：删 AI 回复（返回 `403` body `{"error": "ai_reply_not_deletable"}`）。

### `GET /api/research/{rid}/comments`

列出该方案下所有非软删评论。返回结构嵌套：顶级用户评论 + `ai_reply` 字段。按 `created_at` 升序。

**返回字段**：除 `user_id`（内部）和 `deleted_at`（过滤用）外，`comments` 表所有字段都会出现在响应里。每条顶级（`author='user'`）评论后面带一个 `ai_reply` 对象（`author='ai'` 那条）；若 AI 占位还没生成内容，`ai_reply.ai_status` 为 `pending`、`body` 为空字符串。

**完整示例**：

```json
{ "comments": [
  {
    "id": "01HXZ...C7R",
    "request_id": "01HXZ...RID",
    "author": "user",
    "anchor_text": "选中的原文",
    "anchor_before": "前 50 字符...",
    "anchor_after": "...后 50 字符",
    "body": "用户评论正文",
    "created_at": "2026-04-20T10:30:00",
    "ai_reply": {
      "id": "01HXZ...AI1",
      "author": "ai",
      "anchor_text": "选中的原文",
      "anchor_before": "前 50 字符...",
      "anchor_after": "...后 50 字符",
      "body": "AI 回复正文",
      "ai_status": "done",
      "ai_error": null,
      "cost_usd": 0.0123,
      "created_at": "2026-04-20T10:30:05"
    }
  }
] }
```

失败态示例（`ai_status="failed"`）：`ai_reply.body` 可能为空字符串或部分内容；`ai_reply.ai_error` 必非空（HARNESS §1）；前端据此渲染错误状态。

**分页**：MVP-1 不做 offset/limit 分页。硬上限 **一次返回最多 200 条**，超过按 `created_at DESC` 截断并在响应头 `X-Comments-Truncated: true` 标注。正常使用下单方案评论数远低于 200；若将来接近就再补正式分页（进 MVP-2 backlog）。

### `GET /api/research/{rid}/comments/stream`

SSE。推送两类事件：

- `ai_delta` `{ comment_id, text }` —— AI 回复流式片段
- `ai_done` `{ comment_id, body, ai_status: "done"|"failed", ai_error?, cost_usd? }`

复用现有 `research_runner` 的 pub/sub 基础设施，channel_id 改为 `f"comment:{comment_id}"`。

---

## 5. AI 回复 pipeline

### 上下文清单（B-Q6=A 全套）

喂给 AI 的上下文包括 5 项：

1. **原始用户问题**（`research_requests.question`）
2. **上传材料清单**（`uploaded_files` 的绝对路径 + `original_name` + `kind`），让 AI 按需 Read
3. **研究方案 markdown**（`plan_path` 内容；failed 时为空，用 error_message 代替）
4. **用户选中的原文**（`anchor_text`）
5. **用户评论正文**（`body`）

### Prompt 模板

新增 `app/templates/prompts/comment_reply.j2`，用 `{% if error_message %}` 切 done / failed 两套分支。

**done 分支**（方案写成功，用户对方案某段评论）：

```jinja
/research-method-designer 的"评论员"角色。请保持评论员身份，不要改写方案、不要重新跑流程。

用户最初问的问题：
---
{{ question }}
---

用户上传的材料（按需 Read）：
{% for f in uploaded_files -%}
- {{ f.original_name }} → {{ f.local_path }}
{% endfor %}

你之前给用户的研究方案：
---
{{ plan_markdown }}
---

用户选中了方案中的这一段：
> {{ anchor_text }}

用户评论：
> {{ user_body }}

要求：
1. 直接回应用户的观点。如果你同意，说清楚为什么；如果不同意，给理由。
2. 如果用户指出了方案的问题，承认并给出改进建议（不要重写整个方案，只说这一段应该怎么改）。
3. 如果评论涉及上传材料里的具体内容，用 Read 工具查证再回。
4. 全中文，口语化，300 字以内。不要前言客套，直接说内容。
```

**failed 分支**（方案生成失败，用户对错误消息评论）：

```jinja
/research-method-designer 的"自我诊断员"角色。用户的研究请求失败了，用户来评论反馈。

用户最初问的问题：
---
{{ question }}
---

用户上传的材料：
{% for f in uploaded_files -%}
- {{ f.original_name }} → {{ f.local_path }}
{% endfor %}

系统失败信息：
---
{{ error_message }}
---

用户选中了失败信息里的这一段：
> {{ anchor_text }}

用户评论：
> {{ user_body }}

要求：
1. 用简明中文分析最可能的失败原因。
2. 给用户 1-2 条具体的下一步建议（例如重新描述问题、补充资料、换个角度提问）。
3. 如果是我们系统的 bug（不是用户输入的问题），明确说"这是我们的 bug，已经记下来"。
4. 全中文，口语化，300 字以内。不要前言客套。
```

### 调用细节

- **模型**：`settings.comment_model`（默认 = `settings.claude_model`，即 Opus 4.7）。加 env `CLAUDE_COMMENT_MODEL` 作为逃生门，若后续观察成本失控再配。**不降级到 Haiku**（B-Q8）
- 工具集：`--allowed-tools Read,Glob,Grep`（HARNESS §3 硬约束一致）
- cwd：`{upload_dir}/{rid}/`（让 AI 能 Read 用户上传的原材料）
- 超时：`settings.claude_comment_timeout_sec`，默认 60（比 research 的 600 短）
- 流式输出：消费 stream-json 的 `delta` 事件，`_publish` 到 SSE channel
- **成本记录**：从 stream-json 的 `result` 事件里读 `total_cost_usd`，写入 `comments.cost_usd`；便于后续观察成本

### 失败处理（HARNESS §1）

所有失败路径必须 `ai_status = 'failed'` + 非空 `ai_error`：

- 子进程 ENOENT / 超时 / exit≠0 → 具体错误写进 `ai_error`
- 生成内容为空 → `ai_error = "claude 未返回内容"`
- SSE 客户端断开不影响落库（DB 是真相）

### 用户评论 body 规范化（防止 Unicode 绕过）

用户评论 body 在注入 prompt 前走一次：

1. 去零宽字符（`\u200b`, `\u200c`, `\u200d`, `\ufeff`）
2. 去 Unicode Bidi 控制字符（`\u202a`..`\u202e`, `\u2066`..`\u2069`）
3. 若经过规范化后 body 变空，拒绝 `400 {"error": "body_empty"}`

规范化后的 body 同时写入 DB 和 prompt，保证显示和传给 AI 的是同一份干净文本。

---

## 6. 前端 UI（MVP-1：底部评论区）

### 方案详情页新增结构（`app/templates/history_detail.html`）

```html
<div class="markdown-body" data-markdown-source="...方案原始 markdown...">
  …渲染的方案…
</div>

<!-- failed 方案：错误信息也要可选 -->
<div class="error-banner" data-markdown-source="error_message 原文">
  …错误信息…
</div>

<!-- 选区浮动工具条（绝对定位） -->
<div id="selection-tool" class="hidden">
  <button id="add-comment-btn">💬 评论</button>
</div>

<!-- 评论区 -->
<section class="comments">
  <h2>评论</h2>
  <ul id="comment-list">
    <!-- 动态渲染 -->
  </ul>
</section>

<!-- 评论创建浮层 -->
<dialog id="comment-compose">
  <blockquote class="selected-excerpt"></blockquote>
  <textarea maxlength="2000" placeholder="写下你的评论..."></textarea>
  <button class="cancel">取消</button>
  <button class="submit">发送</button>
</dialog>
```

### 评论卡片

```html
<li class="comment-card" data-cid="...">
  <blockquote class="excerpt">{{ anchor_text }}</blockquote>
  <div class="user-comment">
    <span class="author">你</span>
    <span class="time">刚刚</span>
    <p class="body">{{ body }}</p>
    <button class="delete-btn" aria-label="删除">×</button>
  </div>
  <div class="ai-reply" data-status="pending|streaming|done|failed">
    <span class="author">AI 评论员</span>
    <p class="body">{{ ai body or loading... }}</p>
  </div>
</li>
```

### JS 交互（`app/static/app.js` 新增 `initComments()`）

- 页面加载：`fetch /api/research/{rid}/comments` → 渲染所有评论
- 页面加载：打开 SSE `/comments/stream`，处理 `ai_delta` / `ai_done` 事件
- 文本选择：mouseup 事件 → 检测选区 → 显示浮动按钮
- 点击"💬 评论" → 弹 dialog，textarea focus
- 提交：POST → 创建占位卡片 → 开 SSE 订阅
- 删除：confirm → DELETE → 淡出移除

### CSS（`app/static/style.css` 新增）

`.comments`, `.comment-card`, `.selection-tool`, `.comment-compose`, `.excerpt`, `.ai-reply[data-status="..."]` 等，与现有 history-detail 页风格一致。

---

## 7. 产出文件表（HARNESS 约定的"output files"）

| 产出文件 | 作用 | 新/改 |
|---|---|---|
| `app/models.py` | 新增 `Comment` ORM 类 + 2 个 Index | 改 |
| `app/services/comment_runner.py` | 新服务：创建 / 级联软删 / AI pipeline / publish；两套 prompt 分支（done + failed） | 新 |
| `app/routers/research.py` | 加 4 个 endpoint（POST / GET / DELETE / SSE） | 改 |
| `app/templates/prompts/comment_reply.j2` | AI 评论员 prompt（done + failed 分支 via `{% if error_message %}`） | 新 |
| `app/templates/history_detail.html` | 加评论区 DOM 骨架 + `data-markdown-source`；failed 态的 `.error-banner` 也可选 | 改 |
| `app/static/app.js` | `initComments()` + selection / 浮动按钮 / dialog 处理 | 改 |
| `app/static/style.css` | `.comments`, `.comment-card`, `.selection-tool`, `.comment-compose` 等 | 改 |
| `app/config.py` | 加 `comment_model: str` + `claude_comment_timeout_sec: int`（env `CLAUDE_COMMENT_MODEL` / `CLAUDE_COMMENT_TIMEOUT_SEC`） | 改 |
| `.env.example` | 新增两条 env 样例：`CLAUDE_COMMENT_MODEL=`（空=回退 `CLAUDE_MODEL`）和 `CLAUDE_COMMENT_TIMEOUT_SEC=60` | 改 |
| `tests/unit/test_comment_runner.py` | 评论服务单元测试 | 新 |
| `tests/integration/test_comment_endpoints.py` | 4 个 endpoint + done / failed 分支的集成测试 | 新 |

---

## 8. 测试点（给 tester sub-agent 的粗稿）

### 单元（`tests/unit/test_comment_runner.py`）

1. 创建用户评论 + AI 占位：DB 行正确，author 区分
2. 级联软删：删用户评论后，所有 parent_id 指向它 / author='ai' 的子评论也 `deleted_at`
3. AI pipeline（done 分支）：模拟 stream 返回 deltas + done，落库 `ai_status=done` + body 完整 + `cost_usd` 非空
4. AI pipeline（failed 分支）：方案 status=failed 时 prompt 用自我诊断模板，能正常生成回复
5. AI pipeline 超时：`ai_status=failed` + `ai_error` 非空（HARNESS §1）
6. AI pipeline claude 子进程 ENOENT：同上
7. AI pipeline 空返回：`ai_error = "claude 未返回内容"`
8. SSE channel 名格式：`comment:{comment_id}`
9. Prompt 上下文完整性：断言 `_build_prompt` 输出里包含 question / 每个 uploaded_file 的 local_path / plan_markdown / anchor_text / user_body 五项

### 集成（`tests/integration/test_comment_endpoints.py`）

1. POST 创建评论成功（done 方案）→ 201 + DB 行 + 占位 AI 行
2. POST 创建评论成功（failed 方案）→ 201 + DB 行 + 占位 AI 行（B-Q7 分支）
3. POST 对 pending / running 方案 → 409 `request_not_finalized`
4. POST 对他人方案 → 404（无枚举 oracle）
5. POST `anchor_text` 超 2000 → 400
6. GET 列表返回嵌套结构：user + ai_reply
7. GET 过滤软删
8. DELETE owner 成功 → 204 + 级联软删
9. DELETE 他人评论 → 404
10. DELETE AI 回复 → 403 `ai_reply_not_deletable`
11. DELETE 未登录 → 401
12. SSE：订阅后能收到新创建评论的 `ai_delta` → `ai_done`

---

## 9. 风险与已知约束

| 风险 | 缓解 |
|---|---|
| Prompt 注入：用户评论 body + 上传文件内容会进 AI prompt，恶意内容可能让 AI 跳出"评论员"角色 | prompt 模板里明确"保持评论员身份，不要改写方案、不要重新跑流程"；body 限 2000 字符；Claude 自身 system 对 role reversal 鲁棒性一般够 |
| XSS：评论 body 显示到前端 | 纯文本显示，不走 markdown 渲染；Jinja / JS 都 autoescape |
| 锚定漂移：未来方案 markdown 如果可编辑，anchor_text + before/after 可能在 DOM 里找不到 | 目前方案不可编辑；MVP-3 的行内高亮阶段再引入模糊匹配 fallback |
| 费用：每条评论触发一次 Opus API 调用 | 记录 `cost_usd` 观察；B-Q5 节流留 backlog；若观察到日均 > $1 / 用户就切 Haiku |
| 并发：一个方案同时收到多条评论 | comments 表写入互不阻塞；AI 回复各自独立 channel，无冲突 |
| 注销账号恢复期限（功能 A8）| 30 天软删 → admin 恢复接口 / 30 天后 cron 硬删；本功能只需遵守 `deleted_at` 过滤，不改逻辑 |

---

## 10. 里程碑

按项目级调整的 10 步（Step 5 skipped），预计工作量 **3 天**：

1. ✅ Step 2a 本文档 + `/design-check`
2. Step 2b 飞书评审已通过（2026-04-20 v2）
3. Step 3 `/tester`
4. Step 4 `/test-quality-check`
5. Step 6 backup + feature branch
6. Step 7 dev loop
7. Step 8 `/review`
8. Step 9 DEV_LOG

### 后续迭代（进 `docs/TODO.md` 的 B 区 backlog）

- MVP-2：`/history` 列表卡片角标"💬 3"评论数徽标 + `cost_usd` dashboard（每个用户近 7 天评论 AI 成本）
- MVP-3：原文行内高亮 + 旁注（TreeWalker + `<mark>`）

---

## 变更记录

- **v2.1 (2026-04-20)**：吸收 `/design-check` BLOCKING + WARN —— GET 响应字段完整列出、加硬 200 条上限、datetime 约定显式、`_TASKS` 模式显式、DB 失败 500 约定、N+1 防范、`.env.example` 产出文件、body Unicode 规范化
- **v2 (2026-04-20)**：吸收飞书 5 条评论 — Prompt 上下文扩为 5 项；默认 Opus；加 failed 分支；`cost_usd` 字段；error_message 也可选；软删 30 天可恢复；评论数徽标挪到 MVP-2
- **v1 (2026-04-20)**：初版
