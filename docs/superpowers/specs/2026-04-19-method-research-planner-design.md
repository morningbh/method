# Method — 研究计划生成器 设计文档

**日期**: 2026-04-19
**作者**: 胡博予 + Claude
**状态**: 待审批（Brainstorming → Spec）

---

## 0. 一页概览

**产品**: 一个单页网站，用户输入研究问题 + 上传资料，系统用 `research-method-designer` skill 生成结构化研究方案 (markdown)。

**用户**: 管理员审批制。管理员 = 胡博予 (`morningwilliam@gmail.com`)。

**技术决策一览**:

| 维度 | 选择 |
|---|---|
| 后端 | Python 3.11 + FastAPI + Jinja2 |
| 前端 | 单页 HTML + 原生 JS（无框架），SSE 流式渲染 |
| 存储 | SQLite + 磁盘文件 |
| 引擎 | `claude -p` headless subprocess |
| 模型 | `--model claude-opus-4-7`（固定） |
| Skill | `research-method-designer`（系统安装于 `~/.claude/skills/`） |
| 文件格式 | .md / .txt / .pdf / .docx |
| 文件限额 | 20 个/次，单文件 30 MB，总 100 MB |
| 文件保留 | 永久 |
| 认证 | 邮箱注册 → 管理员审批 → 邮件验证码登录 → HTTP-only cookie session |
| 邮件 | Gmail SMTP + App Password |
| 部署 | 腾讯云服务器，域名 `method.xvc.com`（待配） |
| 仓库 | https://github.com/morningbh/method |

---

## 1. 系统架构

### 1.1 组件拓扑

```
┌─────────────────────────────────────────────────────────────────┐
│                         Browser (单页)                           │
│   ┌──────────┐   ┌────────────────┐   ┌─────────────────┐       │
│   │ 登录/注册 │   │  输入框+上传框 │   │   历史列表       │      │
│   └──────────┘   └────────┬───────┘   └─────────────────┘       │
│                           │ POST /api/research + SSE             │
└───────────────────────────┼─────────────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                    FastAPI app (Python)                         │
│                                                                  │
│   ┌────────────┐  ┌───────────┐  ┌────────────┐  ┌──────────┐   │
│   │ auth 路由   │  │ research  │  │ history    │  │ admin    │  │
│   │ /login     │  │ /api/...  │  │ /api/...   │  │ /approve │  │
│   │ /register  │  │  (SSE)    │  │            │  │          │  │
│   └────────────┘  └─────┬─────┘  └────────────┘  └──────────┘   │
│                         │                                        │
│   ┌──────────┐  ┌───────▼────────┐  ┌───────────────────────┐   │
│   │  mailer │  │ claude_runner  │  │ file_processor        │   │
│   │ (smtp) │  │ (subprocess)   │  │ (pdf/docx → .md)      │   │
│   └──────────┘  └────────────────┘  └───────────────────────┘   │
│                         │                                        │
│   ┌─────────────────────┴────────────────────────────────┐      │
│   │              SQLite + 磁盘文件                       │      │
│   │   users / sessions / login_codes / approval_tokens   │      │
│   │   research_requests / uploaded_files                 │      │
│   │                                                       │      │
│   │   /var/method/uploads/<request_id>/...                │      │
│   │   /var/method/plans/<request_id>.md                   │      │
│   └───────────────────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────────────────┘
                            │
                            ▼
                ┌───────────────────────┐
                │   Claude Code CLI     │
                │  (headless subprocess) │
                │  → Anthropic API       │
                └───────────────────────┘
```

### 1.2 组件职责

| 组件 | 职责 | 依赖 |
|---|---|---|
| `auth` 路由 | 注册、发验证码、验证登录、session 管理 | `mailer`, DB |
| `admin` 路由 | 批准链接 `/admin/approve?token=...` | DB |
| `research` 路由 | 接收问题+文件 → 存盘 → 起子进程 → SSE 推流 | `claude_runner`, `file_processor`, DB |
| `history` 路由 | 列出用户历史 + 读取单条详情 + 下载 | DB |
| `claude_runner` | 纯 subprocess wrapper，不依赖 FastAPI | stdlib `asyncio.subprocess` |
| `file_processor` | pdf/docx → text，生成 `.extracted.md` | `pdfplumber`, `python-docx` |
| `mailer` | SMTP 发送，模板化 | `aiosmtplib` |
| 数据层 | SQLAlchemy 2.x + SQLite | `sqlalchemy`, `aiosqlite` |

**关键边界约定**: `claude_runner`、`file_processor`、`mailer` 都是**纯模块（无 Web 依赖）**，可单独单测。路由层只做：校验 → 调模块 → 组装响应。

---

## 2. 数据模型

### 2.1 SQLite 表

```sql
CREATE TABLE users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  email         TEXT NOT NULL UNIQUE,
  status        TEXT NOT NULL CHECK(status IN ('pending','active','rejected')),
  created_at    DATETIME NOT NULL,
  approved_at   DATETIME
);

CREATE TABLE login_codes (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL REFERENCES users(id),
  code_hash     TEXT NOT NULL,       -- sha256 of 6-digit code + per-row salt
  salt          TEXT NOT NULL,
  expires_at    DATETIME NOT NULL,   -- now + 10 min
  used_at       DATETIME,
  created_at    DATETIME NOT NULL
);

CREATE TABLE sessions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL REFERENCES users(id),
  token_hash    TEXT NOT NULL UNIQUE,  -- sha256 of cookie token
  expires_at    DATETIME NOT NULL,     -- now + 30 days
  created_at    DATETIME NOT NULL
);

CREATE TABLE approval_tokens (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL REFERENCES users(id),
  token_hash    TEXT NOT NULL,         -- sha256 of admin email token
  expires_at    DATETIME NOT NULL,     -- now + 7 days
  used_at       DATETIME
);

CREATE TABLE research_requests (
  id            TEXT PRIMARY KEY,      -- ULID
  user_id       INTEGER NOT NULL REFERENCES users(id),
  question      TEXT NOT NULL,
  status        TEXT NOT NULL CHECK(status IN ('pending','running','done','failed')),
  plan_path     TEXT,                  -- /var/method/plans/<id>.md
  error_message TEXT,
  model         TEXT NOT NULL,         -- 'claude-opus-4-7'
  created_at    DATETIME NOT NULL,
  completed_at  DATETIME
);

CREATE TABLE uploaded_files (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id      TEXT NOT NULL REFERENCES research_requests(id),
  original_name   TEXT NOT NULL,
  stored_path     TEXT NOT NULL,       -- /var/method/uploads/<req>/<uuid>.ext
  extracted_path  TEXT,                -- /var/method/uploads/<req>/<uuid>.extracted.md
  size_bytes      INTEGER NOT NULL,
  mime_type       TEXT NOT NULL,
  created_at      DATETIME NOT NULL
);

CREATE INDEX idx_requests_user_created ON research_requests(user_id, created_at DESC);
CREATE INDEX idx_sessions_token ON sessions(token_hash);
CREATE INDEX idx_login_codes_user ON login_codes(user_id, expires_at);
```

### 2.2 关键设计决策

1. **`request_id` 用 ULID**（时间有序、URL 安全、26 字符），不用 auto-increment（防枚举）也不用 UUID（太长）。
2. **验证码和 session token 只存 hash**（sha256 + 每行 salt），明文只发给用户。
3. **`status = failed`** 必须有：claude 超时或报错时前端能看到而不是永远 running。
4. **`plan_path` 指向磁盘**，markdown 不进 DB（避免大 TEXT 字段退化性能、方便 grep/备份）。
5. **`uploaded_files.extracted_path`**：pdf/docx 先本地转 .md，claude 只读 .md，避免子进程还要启动 PDF 解析器。

---

## 3. REST API

### 3.1 路由清单

| Method | Path | Auth | 说明 |
|---|---|---|---|
| `GET` | `/` | session 可选 | 主页 (登录态 → 工作台，未登录 → 跳 `/login`) |
| `GET` | `/login` | — | 登录/注册页 |
| `POST` | `/api/auth/request_code` | — | 发验证码（自动注册，返回 "pending" 或 "sent"） |
| `POST` | `/api/auth/verify_code` | — | 校验验证码，设置 session cookie |
| `POST` | `/api/auth/logout` | session | 清 session |
| `GET` | `/admin/approve` | query `token` | 管理员点邮件链接，激活用户，返回 HTML "已批准" |
| `POST` | `/api/research` | session | multipart/form-data：`question` + `files[]`；返回 `request_id` |
| `GET` | `/api/research/<id>/stream` | session, 自己的 | SSE 推流，event: `delta` / `done` / `error` |
| `GET` | `/api/research/<id>` | session, 自己的 | 单条详情 JSON（状态 + 最终 markdown） |
| `GET` | `/api/research/<id>/download` | session, 自己的 | 下载 `.md`，`Content-Disposition: attachment` |
| `GET` | `/api/history` | session | 当前用户历史列表 |
| `GET` | `/history/<id>` | session | 历史详情页（HTML） |
| `GET` | `/api/health` | — | `{"ok": true}` |

### 3.2 请求/响应样例

**发验证码**
```
POST /api/auth/request_code
Content-Type: application/json

{"email": "alice@example.com"}

→ 200 {"status": "sent"}      // 账号已激活，验证码已发
→ 200 {"status": "pending"}   // 新注册或未审批，不发码，提示等审批
→ 200 {"status": "rejected"}  // 被拒
→ 429 {"error": "rate_limit"} // 60 秒内限发一次
```

**发起研究**
```
POST /api/research
Content-Type: multipart/form-data

question: "research whether moated consumer AI apps can exist"
files[]: (file1), (file2), ...

→ 201 {"request_id": "01HX...", "status": "pending"}
```

**SSE 推流**
```
GET /api/research/01HX.../stream
Accept: text/event-stream

event: delta
data: {"text": "# 1. 问题重述\n\n..."}

event: delta
data: {"text": "..."}

event: done
data: {"request_id": "01HX...", "elapsed_ms": 67400}

event: error
data: {"message": "claude subprocess exited with code 1"}
```

---

## 4. 认证流程

### 4.1 注册 + 审批 + 登录 状态机

```
[访客]
  │ POST /api/auth/request_code { email }
  ▼
┌────────────────────────────────────────────────┐
│ 后端：查 users.email                           │
│   不存在 → INSERT user (status='pending')      │
│           → 生成 approval_token                │
│           → 发邮件给 admin（含 approve 链接）  │
│           → 返回 {status: "pending"}           │
│   存在 pending/rejected → 直接返回对应 status  │
│   存在 active → 生成 login_code (6 digit)      │
│                → 发邮件给用户                  │
│                → 返回 {status: "sent"}         │
└────────────────────────────────────────────────┘
                    │
                    ▼
        [用户邮箱里拿到验证码]
                    │
                    │ POST /api/auth/verify_code { email, code }
                    ▼
┌────────────────────────────────────────────────┐
│ 后端：校验 code_hash + expires_at + !used_at   │
│   成功 → INSERT session                        │
│         → Set-Cookie: method_session=<token>   │
│                          HttpOnly; Secure;     │
│                          SameSite=Lax;         │
│                          Max-Age=30d           │
│   失败 → 400 {error: "invalid_or_expired"}     │
└────────────────────────────────────────────────┘

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[管理员邮箱收到审批邮件]
  │ 点击 https://method.xvc.com/admin/approve?token=<raw>
  ▼
┌────────────────────────────────────────────────┐
│ 后端：校验 ADMIN_SECRET 不需要 — token 自身可防伪 │
│   查 approval_tokens.token_hash, expires, used │
│   成功 → UPDATE users SET status='active',     │
│                            approved_at=now     │
│         → UPDATE approval_tokens.used_at=now   │
│         → 返回 HTML "已批准 alice@example.com" │
│         → 额外发一封邮件给用户："账号已激活"   │
│   失败 → 返回 HTML "链接无效或已过期"          │
└────────────────────────────────────────────────┘
```

### 4.2 邮件模板

**验证码邮件（发给普通用户）**
```
Subject: Method 登录验证码

你好，

你的登录验证码是：123456

10 分钟内有效。如果不是你本人操作，忽略即可。

— Method
```

**审批邮件（发给管理员）**
```
Subject: [Method] 新用户注册待审批：alice@example.com

新用户 alice@example.com 申请注册 Method。
点击批准：
https://method.xvc.com/admin/approve?token=<64字符随机>

链接 7 天内有效。不批准则无需操作。
```

**激活通知邮件（发给用户）**
```
Subject: Method 账号已激活

你的 Method 账号 (alice@example.com) 已通过审批。

现在可以访问 https://method.xvc.com 登录了。

— Method
```

### 4.3 限流 & 安全

- **`/api/auth/request_code`**：同一 email 60 秒内只能触发一次。用 SQLite 查最近的 `login_codes.created_at` 实现（无需 Redis）。
- **`/api/auth/verify_code`**：同一 email 连续错 5 次 → 该 email 被拒绝验证 15 分钟（已发出的验证码一并失效，需重新走 request_code）。
- **验证码**：6 位数字，有效期 10 分钟，一次性（`used_at` 设上就失效）。
- **session cookie**：`HttpOnly`, `Secure`, `SameSite=Lax`, 30 天。
- **approval token**：32 字节随机 urlsafe base64（≈43 字符），7 天失效，一次性。
- **CSRF**：SameSite=Lax 够用；表单外调用全部 JSON，且会校验 `Origin` 头。

---

## 5. Claude Code Subprocess 集成

### 5.1 调用命令

```python
cmd = [
    "claude",
    "-p", prompt,
    "--output-format", "stream-json",
    "--model", "claude-opus-4-7",
    "--allowed-tools", "Read,Glob,Grep",
    "--permission-mode", "acceptEdits",
    "--cwd", sandbox_dir,    # /var/method/uploads/<request_id>/
]
```

- `-p` = print 模式（一次性 prompt，不进入交互）
- `--output-format stream-json` = 每个模型 delta 作为一行 JSON 输出，方便转 SSE
- `--allowed-tools Read,Glob,Grep` = 白名单；禁止 Write/Edit/Bash 保证沙箱
- `--permission-mode acceptEdits` = 不在 stdin 等交互批准（只读 + 查找，无副作用）
- `--cwd` = 限定工作目录 = 用户此次上传文件所在的临时目录

### 5.2 Prompt 模板

```
/research-method-designer

用户的研究问题：
{{ user_question }}

{% if uploaded_files %}
用户上传了以下资料（相对于当前工作目录）：
{% for f in uploaded_files %}
- {{ f.original_name }} → {{ f.local_path }}
{% endfor %}

请根据需要用 Read 工具阅读这些文件，把相关内容纳入研究方案的"已有材料"部分。
{% endif %}

请直接产出完整的研究方案 markdown（按 skill 要求的 10 节结构）。
```

`/research-method-designer` 是 skill 的 invocation 字符串（skill.md 中 `user-invocable: true`）。

### 5.3 流式输出解析

`claude --output-format stream-json` 每行一个 JSON，形如：

```json
{"type":"assistant","message":{"content":[{"type":"text","text":"..."}]}}
{"type":"assistant","message":{"content":[{"type":"tool_use",...}]}}
{"type":"result","subtype":"success","result":"...final...","total_cost_usd":0.42}
```

`claude_runner.stream(prompt, cwd)` async 生成器：
- 读 stdout 每行 → JSON decode
- `type=assistant + content[*].type=text` → yield `("delta", text)`
- `type=result` → yield `("done", final_markdown, cost_usd, elapsed)`
- subprocess 非零退出 → yield `("error", stderr.decode())`

### 5.4 超时 & 并发

- **单请求超时**: 10 分钟。超时 → `kill -TERM`（宽限 5 秒）→ `kill -KILL`，`status=failed`。
- **全局并发**: 最多 3 个 claude 子进程同时跑（`asyncio.Semaphore(3)`）。多的排队，前端看到 `status=pending`。
- **调用成本**：每个 opus-4-7 子进程约 $0.3–$1.5，SSE 事件里把 `total_cost_usd` 回显到历史页（仅本人可见）。

---

## 6. 文件上传 & 处理

### 6.1 流水线

```
[multipart upload]
     │
     ▼
[保存到 /var/method/uploads/<request_id>/<uuid>.<ext>]
     │
     ▼
┌───────────────┐
│ 按扩展名分流  │
├───────────────┤
│ .md, .txt    │ → 直接登记，Claude Read 即可
│ .pdf         │ → pdfplumber 提取文字 → .extracted.md
│ .docx        │ → python-docx 提取文字 → .extracted.md
│ 其他         │ → 400 拒收
└───────────────┘
     │
     ▼
[插入 uploaded_files 行：stored_path + extracted_path]
     │
     ▼
[prompt 模板中，优先用 extracted_path 对应的 .md]
```

### 6.2 限额校验（逐层）

1. **前端**：`<input accept=".md,.txt,.pdf,.docx">`，单文件 `> 30 MB` 选中后本地提示，不上传。
2. **FastAPI 层**：`UploadFile.size` 校验；总尺寸累加校验 100 MB；文件数 > 20 直接 400。
3. **存储前**：再次 `stat()` 校验（防止 multipart 声明大小 ≠ 实际）。
4. **MIME 嗅探**（`python-magic`）：确认内容确实是声明的类型，防止改后缀恶搞。

### 6.3 失败处理

- pdf/docx 解析失败（损坏、加密）：登记 `extracted_path=NULL` + `mime_type` 保留，claude 不读这个文件（但仍在 prompt 里列出"此文件解析失败，已忽略"）。
- 解析超时（10 秒/文件）：同上。
- 整个 research 请求失败：所有上传文件**保留**，便于调试和重跑。

---

## 7. UI / 页面样式

### 7.1 设计基调

- **极简**，灵感来自 Google 搜索首页：大量留白 + 一个中央元素 + 极少颜色。
- **色板**：
  - 主背景 `#FAFAF7`（米白，不刺眼）
  - 强调色 `#1F2937`（近黑）
  - 辅助色 `#6B7280`（中灰）
  - 按钮色 `#111827`（纯黑），hover `#374151`
  - 错误色 `#B91C1C`
  - 链接色 `#1D4ED8`
- **字体**：
  - 正文 `-apple-system, 'Segoe UI', 'PingFang SC', sans-serif`
  - 等宽（代码 / markdown 渲染）`'SF Mono', 'Menlo', monospace`
- **间距**：8px 栅格，卡片最大宽 `720px` 居中。
- **无 JS 框架**：原生 fetch + EventSource。markdown 渲染用 `marked.js`（单文件 CDN 引入，≈50KB）。

### 7.2 页面线框

#### A. `/login` — 登录/注册页

```
╭──────────────────────────────────────────────────────╮
│                                                       │
│                                                       │
│                                                       │
│                    Method                             │
│           研究问题 → 研究方案                          │
│                                                       │
│      ┌─────────────────────────────────────────┐     │
│      │  你的邮箱                                │     │
│      └─────────────────────────────────────────┘     │
│                                                       │
│             ┌───────────────────────┐                 │
│             │   发送登录验证码       │                 │
│             └───────────────────────┘                 │
│                                                       │
│    首次使用会自动注册，需管理员批准后才能登录           │
│                                                       │
│                                                       │
╰──────────────────────────────────────────────────────╯
```

提交后，同一位置切换为：

```
╭──────────────────────────────────────────────────────╮
│                    Method                             │
│                                                       │
│      验证码已发送到 alice@example.com                 │
│                                                       │
│      ┌─────────────────────────────────────────┐     │
│      │  ______                                  │     │
│      └─────────────────────────────────────────┘     │
│                                                       │
│             ┌───────────────────────┐                 │
│             │        登录           │                 │
│             └───────────────────────┘                 │
│                                                       │
│                                  [换个邮箱]           │
╰──────────────────────────────────────────────────────╯
```

如果后端返回 `pending`：

```
╭──────────────────────────────────────────────────────╮
│                    Method                             │
│                                                       │
│      ✉️  注册已提交                                    │
│                                                       │
│      alice@example.com 已提交管理员审批。             │
│      批准后会发邮件通知你，然后即可登录。              │
│                                                       │
│                                  [换个邮箱]           │
╰──────────────────────────────────────────────────────╯
```

#### B. `/` — 工作台（已登录）

```
╭──────────────────────────────────────────────────────────────╮
│   Method                           alice@... | 历史 | 登出    │   ← 顶栏 fixed
├──────────────────────────────────────────────────────────────┤
│                                                               │
│                                                               │
│                                                               │
│                    帮你设计研究计划                            │
│                                                               │
│     ┌───────────────────────────────────────────────────┐   │
│     │                                                     │   │
│     │  把研究问题写在这里...                              │   │
│     │                                                     │   │
│     │                                                     │   │
│     └───────────────────────────────────────────────────┘   │
│                                                               │
│     📎 拖拽上传资料，或 [选择文件]                            │
│        支持 .md / .txt / .pdf / .docx  ·  最多 20 个          │
│                                                               │
│     ┌────────────┐  ┌─────────────┐                           │
│     │ file1.pdf × │  │ notes.md ×  │   ← 已选文件 chip         │
│     └────────────┘  └─────────────┘                           │
│                                                               │
│              ┌────────────────────────────┐                   │
│              │     生成研究方案            │                   │
│              └────────────────────────────┘                   │
│                                                               │
╰──────────────────────────────────────────────────────────────╯
```

#### C. `/history/<id>` — 生成过程 / 结果页

```
╭──────────────────────────────────────────────────────────────╮
│   Method                           alice@... | 历史 | 登出    │
├──────────────────────────────────────────────────────────────┤
│   ← 返回历史                                                  │
│                                                               │
│   研究问题                                                    │
│   ┌─────────────────────────────────────────────────────┐   │
│   │ research whether moated consumer AI apps can exist  │   │
│   └─────────────────────────────────────────────────────┘   │
│                                                               │
│   上传资料：file1.pdf, notes.md                               │
│                                                               │
│   状态：● 生成中 (47s)                    [复制] [下载 .md]  │
│                                                               │
│   ┌─────────────────────────────────────────────────────┐   │
│   │ # 1. 问题重述                                        │   │
│   │                                                       │   │
│   │ 用户原问题：consumer AI apps moat 是否存在。         │   │
│   │                                                       │   │
│   │ 转写为可研究目标：在 2026–2028 年时间窗内，是否存在  │   │
│   │ 稳定机制使得 consumer AI app 能……▊                  │   │
│   │                                                       │   │
│   └─────────────────────────────────────────────────────┘   │
│                                                               │
╰──────────────────────────────────────────────────────────────╯
```

`▊` 表示正在流式输入。完成后"生成中"变"● 已完成"，按钮变可点。

#### D. `/history` — 历史列表

```
╭──────────────────────────────────────────────────────────────╮
│   Method                           alice@... | 历史 | 登出    │
├──────────────────────────────────────────────────────────────┤
│                                                               │
│   历史记录                                                    │
│                                                               │
│   ┌─────────────────────────────────────────────────────┐   │
│   │ ● research whether moated consumer AI apps can...   │   │
│   │ 2026-04-19 15:02  ·  2 files  ·  $0.42              │   │
│   └─────────────────────────────────────────────────────┘   │
│   ┌─────────────────────────────────────────────────────┐   │
│   │ ● how should I evaluate LP's PMF signal             │   │
│   │ 2026-04-18 21:47  ·  0 files  ·  $0.28              │   │
│   └─────────────────────────────────────────────────────┘   │
│   ┌─────────────────────────────────────────────────────┐   │
│   │ ✗ research X Y Z                        [失败]      │   │
│   │ 2026-04-18 10:15  ·  错误：subprocess timeout       │   │
│   └─────────────────────────────────────────────────────┘   │
│                                                               │
╰──────────────────────────────────────────────────────────────╯
```

#### E. 管理员审批邮件 - 点击后页面

```
╭──────────────────────────────────────────────────────╮
│                    Method                             │
│                                                       │
│      ✓ 已批准                                         │
│                                                       │
│      alice@example.com 已激活。                       │
│      系统已自动发送通知邮件。                          │
│                                                       │
╰──────────────────────────────────────────────────────╯
```

### 7.3 响应式 & 移动端适配

**目标**：在 iPhone SE (375×667)、iPhone 14 Pro (393×852)、iPad (768×1024) 上可用，无横向滚动。

**硬规则（所有页面必须满足）：**

1. `<meta name="viewport" content="width=device-width, initial-scale=1">` 写进 `base.html` 的 `<head>`。
2. **断点**：单一断点 `768px`。`< 768px` 走移动布局，`≥ 768px` 走 §7.2 的桌面线框。
3. **触摸目标**：所有可点击元素最小 `44×44px`（iOS HIG）。按钮默认 padding `12px 20px`。
4. **文本输入框** `font-size: 16px;` 起（防 iOS 自动缩放）。
5. **flex-wrap**：文件 chip 列表、顶栏链接都 `flex-wrap: wrap`。
6. **字号基准**：root `font-size: 16px`，正文 `1rem`，标题用 rem。

**桌面 vs 移动差异（`< 768px` 生效）：**

| 组件 | 桌面 (≥768px) | 移动 (<768px) |
|---|---|---|
| 顶栏邮箱 | 全展示 `alice@example.com` | 截断 `alice@e...`，长按显示完整 |
| 顶栏 "历史/登出" | 文字链接 | 改图标（🕒 / ⏻），节省横向空间 |
| 主页 textarea 宽度 | 720px 居中 | `calc(100% - 32px)` 两侧 16px margin |
| 文件选择区 | "📎 拖拽上传，或 [选择文件]" | "📎 [选择文件]"，去掉拖拽提示 |
| 生成按钮 | 居中，宽 240px | 100% 宽，sticky 到视口底部 |
| 历史卡片 | 720px 居中 | 满宽（仍有 16px 两侧边距） |
| textarea autofocus | 开启 | 关闭（防键盘弹出盖住内容） |
| markdown 代码块 | 正常 | `overflow-x: auto` |
| markdown 表格 | 正常 | 外包 `display:block; overflow-x:auto` |

**移动端已知限制（呼应 §8 错误处理）：**

- **iOS Safari 切后台 / 锁屏会断 SSE**：后端研究仍在跑（是后台进程），用户返回页面时前端检测到 `EventSource.readyState === CLOSED` → 改走 `GET /api/research/<id>` 轮询，直到 `status=done` 后再次读取完整 markdown。
- **Android Chrome 上传 >20MB 文件**：部分低端机型会白屏数秒，属可接受降级，不做特殊处理。

**手动测试清单追加到 §9.3：**

- [ ] iPhone Safari 完整跑通：注册 → 收邮件 → 登录 → 提交研究 → 历史 → 下载
- [ ] Chrome DevTools 模拟 iPhone SE (375×667)，无横向滚动、所有按钮可点
- [ ] 研究生成中切后台 30 秒返回，页面能显示最终结果（走轮询降级）
- [ ] textarea 聚焦时，生成按钮不被键盘遮住

---

## 8. 错误处理 & 边界情况

| 场景 | 处理 |
|---|---|
| SMTP 发信失败（Gmail 限流 / 网络断） | 重试 3 次（指数退避），仍失败 → `login_codes` 回滚，前端收到 503 "邮件发送失败，稍后再试" |
| 管理员邮件发不出去 | 用户注册不被回滚，但打 ERROR 级日志，附加 metric `mailer_admin_fail_total`；需要你手动查 DB 后手动批 |
| Claude CLI 不存在 / 子进程启动失败 | 启动时 fail fast（app 启动做 `claude --version` 探活），运行时失败 → `status=failed` + 错误信息 |
| Claude 流式卡住（10 分钟无 delta） | Watchdog：最后一次 delta 超过 120 秒无新 → 杀进程，`status=failed` |
| 用户在流式进行中刷新页面 | SSE 支持从 last-event-id 续传？**不支持**——页面刷新后从 DB 读最新 `plan_path`（如已 done）或显示 "仍在生成中，刷新后可见最终结果" |
| 同一用户并发发起 3 个请求 | 允许（受全局 Semaphore 限制，个别会排队） |
| DB 文件损坏 | SQLite WAL 模式 + nightly cron 备份到 `/var/method/backups/`（7 天滚动） |
| 磁盘满（uploads 增长失控） | Nginx `client_max_body_size 120m`；应用内检查 `shutil.disk_usage`，< 2 GB 拒绝新 research 请求 |
| 用户点击过期 approval link | 页面提示"链接已过期，请让用户重新注册" |
| 用户提交空问题 | 400 "question 不能为空" |
| 上传了 0 字节文件 | 400 拒绝 |
| 上传了 100 MB 的 txt | 走正常流程；只是 token 成本高，SSE `done` 事件回传 `total_cost_usd` 让用户看到 |
| 跨用户访问（alice 的 session 去看 bob 的 request） | 404（不是 403，不泄露存在性） |
| 管理员已在 ADMIN_EMAIL 中，但自己注册 | 特殊处理：如果注册邮箱 == `ADMIN_EMAIL`，直接 `status=active`，跳过审批 |

---

## 9. 测试策略

遵循 TDD，按全局 CLAUDE.md 的 10 步工作流：

### 9.1 单元测试（pytest + pytest-asyncio）

| 模块 | 测试要点 |
|---|---|
| `claude_runner` | 用 `monkeypatch` 替换 subprocess，模拟 stream-json 输出，断言 delta / done / error 事件顺序 |
| `file_processor` | 小 pdf / docx fixture，断言提取出预期文本 |
| `mailer` | 用 `aiosmtpd` 起本地假 SMTP，断言三种邮件模板生成正确 |
| `auth` 逻辑 | 校验 code hash、TTL、限流计数 |
| 数据层 | 各表 CRUD + 约束（status CHECK、email UNIQUE） |

### 9.2 集成测试（httpx.AsyncClient）

覆盖完整路径（**遵循 L1**：至少一个不 mock `claude_runner` 的端到端用例）：

1. 注册 → 拿 approval token → 批准 → 发码 → 验证码登录 → 发 research（用 mock claude_runner 快速返回）→ 看历史
2. **真实端到端**：跑一次 `claude -p "hello world"` 短 prompt，断言 SSE 能收到 delta + done。默认 skip（需环境变量 `RUN_E2E=1` 开启）；本地部署前手动跑一次，CI 里以 nightly job 形式跑一次。
3. 跨用户隔离：alice 拿 bob 的 request_id → 404
4. 未登录访问 `/api/research` → 401

### 9.3 手动测试清单（部署前）

- [ ] Gmail 真实发件：验证码邮件到达、审批邮件到达、激活通知到达（含中文）
- [ ] 管理员审批链接能打开，能激活
- [ ] 上传一个真实 PDF（一份 TechCrunch 文章），生成研究方案，内容引用了资料
- [ ] 同时起 4 个请求，第 4 个排队
- [ ] 在 Safari / Chrome / iOS Safari 上打开，SSE 正常

---

## 10. 部署

### 10.1 基础设施

- **服务器**：现有腾讯云（CLAUDE.md 中提到的）
- **域名**：`method.xvc.com`（你稍后配）
- **反代**：nginx `listen 443 ssl;`，`proxy_pass http://127.0.0.1:8001;`
- **TLS**：Let's Encrypt via `certbot --nginx -d method.xvc.com`
- **进程管理**：systemd unit `method.service`，`Restart=always`
- **日志**：stdout → journalctl，额外结构化日志写 `/var/method/logs/app.log`（rotate daily, keep 30）

### 10.2 systemd unit（规划）

```ini
[Unit]
Description=Method — Research Planner
After=network.target

[Service]
Type=simple
User=method
WorkingDirectory=/opt/method
EnvironmentFile=/opt/method/.env
ExecStart=/opt/method/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8001
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 10.3 环境变量（`.env`）

```
ADMIN_EMAIL=morningwilliam@gmail.com
BASE_URL=https://method.xvc.com

SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=morningwilliam@gmail.com
SMTP_PASSWORD=<gmail-app-password>
SMTP_FROM=morningwilliam@gmail.com

CLAUDE_BIN=/usr/local/bin/claude
CLAUDE_MODEL=claude-opus-4-7
CLAUDE_TIMEOUT_SEC=600
CLAUDE_CONCURRENCY=3

DB_PATH=/var/method/db/method.sqlite
UPLOAD_DIR=/var/method/uploads
PLAN_DIR=/var/method/plans
LOG_DIR=/var/method/logs

SESSION_SECRET=<32-byte random>
SESSION_TTL_DAYS=30
LOGIN_CODE_TTL_MIN=10
APPROVAL_TOKEN_TTL_DAYS=7
```

### 10.4 发布流程（MVP 阶段：push-to-deploy）

1. 本地开发 → push 到 GitHub `main`
2. 服务器 cron 每分钟 `git pull` + `systemctl restart method`（或手动）
3. 后续加 GitHub Actions → ssh deploy

---

## 11. 项目目录结构

```
method/
├── README.md
├── pyproject.toml
├── .env.example
├── .gitignore
├── LICENSE
├── HARNESS.md                          ← 项目级约束（继承 global CLAUDE.md）
├── CLAUDE.md                           ← 指向 HARNESS.md 的壳
│
├── docs/
│   ├── DEV_LOG.md                      ← 每会话追加决策
│   ├── CODE_REVIEW_PROMPT.md           ← /review 模板
│   ├── TESTER_PROMPT.md                ← /tester 模板
│   ├── TEST_QUALITY_CHECKER_PROMPT.md  ← /test-quality-check 扩展规则
│   └── superpowers/
│       └── specs/
│           └── 2026-04-19-method-research-planner-design.md   ← 本文档
│
├── app/
│   ├── __init__.py
│   ├── main.py                         ← FastAPI app 入口
│   ├── config.py                       ← pydantic-settings 读 .env
│   ├── db.py                           ← SQLAlchemy async session
│   ├── models.py                       ← ORM 定义
│   │
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── auth.py
│   │   ├── admin.py
│   │   ├── research.py
│   │   ├── history.py
│   │   └── health.py
│   │
│   ├── services/
│   │   ├── __init__.py
│   │   ├── claude_runner.py            ← subprocess wrapper
│   │   ├── file_processor.py           ← pdf/docx → md
│   │   ├── mailer.py                   ← SMTP
│   │   └── auth_flow.py                ← code/session/approval 逻辑
│   │
│   ├── templates/
│   │   ├── base.html
│   │   ├── login.html
│   │   ├── index.html
│   │   ├── history.html
│   │   ├── history_detail.html
│   │   └── emails/
│   │       ├── login_code.txt
│   │       ├── admin_approval.txt
│   │       └── activation.txt
│   │
│   └── static/
│       ├── style.css
│       ├── app.js
│       └── vendor/
│           └── marked.min.js
│
├── tests/
│   ├── __init__.py
│   ├── conftest.py                     ← fixtures: db, app client, mocked smtp
│   ├── unit/
│   │   ├── test_claude_runner.py
│   │   ├── test_file_processor.py
│   │   ├── test_mailer.py
│   │   └── test_auth_flow.py
│   ├── integration/
│   │   ├── test_auth_endpoints.py
│   │   ├── test_research_endpoints.py
│   │   ├── test_history_endpoints.py
│   │   └── test_cross_user_isolation.py
│   └── e2e/
│       └── test_real_claude_call.py    ← RUN_E2E=1 才跑
│
├── scripts/
│   ├── init_db.py                      ← 首次部署建表
│   ├── backup_db.sh                    ← cron 每日备份
│   └── deploy.sh                       ← push-to-deploy 用
│
└── deploy/
    ├── nginx.method.xvc.com.conf
    └── method.service                  ← systemd unit
```

### 项目文档说明

- **README.md**：简短的项目说明 + 本地开发命令（`make dev`）
- **HARNESS.md**：项目级硬约束（继承全局 CLAUDE.md，补充 Method 项目特有规则，如 "所有 research 相关错误必须记到 `research_requests.error_message`"）
- **DEV_LOG.md**：每次会话在此 append 决策和教训
- **CODE_REVIEW_PROMPT.md / TESTER_PROMPT.md / TEST_QUALITY_CHECKER_PROMPT.md**：各 sub-agent 的项目特化扩展

---

## 12. 实施路线图（里程碑预览）

以下是实现阶段的粗略拆分，**细节进入 writing-plans 阶段再定**。每个里程碑对应一个"可评审可交付"的边界。

| M | 名称 | 核心产出 | 验收 |
|---|---|---|---|
| M1 | 脚手架 | 仓库、目录、配置、依赖、CI、HARNESS.md | `pytest` 跑通一个 smoke test，`uvicorn` 能起 |
| M2 | 认证闭环 | 注册/审批/登录全流程 + 邮件 | 本地端到端能走完一轮，管理员邮箱能收到邮件 |
| M3 | research 核心 | upload + claude_runner + SSE | 本地真实 claude-opus-4-7 调用能出结果 |
| M4 | 历史 & 下载 | 历史页、详情页、下载 | UI 打磨完毕 |
| M5 | 部署上线 | 腾讯云 + nginx + systemd + TLS | `https://method.xvc.com` 可访问 |

---

## 13. 开放问题（需要审批决策）

以下几点在本 spec 里选了默认，请你审时明确是否同意：

1. 管理员自己注册时**跳过审批**（`status=active` 直通）——同意？
2. SSE 不支持断线续传，刷新页面后从 DB 读最终 markdown——同意？
3. 失败的 research 请求**保留上传文件**用于调试，不自动清理——同意？
4. 文件处理的 pdf/docx 解析失败时，claude prompt 里标注"已忽略"继续跑（而不是整个 request 400）——同意？
5. 暂不加"拒绝用户"按钮（邮件里只有"批准"）——如果需要，加一张 `/admin/reject?token=...`？
6. 所有研究的**成本信息**（cost_usd）对用户可见（历史页显示）——还是只对管理员可见？
7. 服务器时区 `Asia/Shanghai`，所有 `DATETIME` 存 UTC，UI 按北京时间显示——同意？

---

## 14. 不做的事（YAGNI）

明确暂不实现的功能，防止 scope creep：

- ❌ 多 admin 支持（单 admin 够用）
- ❌ 邀请链接 / 预审机制
- ❌ 研究方案的版本/修订历史
- ❌ 研究方案的分享链接
- ❌ 全文检索（超出 SQLite FTS 必要性）
- ❌ 公开 API / 第三方集成
- ❌ 用户自助改邮箱
- ❌ 多模型选择 UI（M1 固定 opus-4-7）
- ❌ 付费 / 计费
- ❌ 移动 App

---

## 15. 变更记录

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-04-19 | v1 | 初稿，brainstorming 完成 |
| 2026-04-19 | v1.1 | 补 §7.3 响应式 & 移动端适配（viewport / 768px 断点 / 触摸目标 / iOS SSE 后台降级等） |
