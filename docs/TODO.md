# Method TODO

Pending items parked here. 一次挑 1-2 个聊清楚再动手，别一次铺开。

Legend:
- 🔴 P0 — do this wave (compliance / security / stability gaps that are live now)
- 🟡 P1 — do soon (two weeks-ish)
- 🟢 P2 — defer until there's a reason
- 💬 = design questions need answers before coding can start
- 🆕 = new feature, not yet scoped to a plan

Last updated: 2026-04-20

---

## A. 已上线的缺口：合规 / 安全 / 稳定

Audit context in chat history 2026-04-20. Current state: 8 users, 14 research requests, Tencent Cloud VM + Cloudflare Tunnel, no backup, no monitoring, session cookie `Secure=False`, marked.js unsanitized.

### 🔴 P0 — 第一波（目标：本周）

- [ ] **A1. Session cookie `Secure=True`** — 改 `app/services/auth_flow.py::COOKIE_FLAGS`；一并 sweep `verify_origin` 是否必须配 `BASE_URL`。
- [ ] **A2. Markdown sanitizer** — `app/static/app.js::renderMarkdown` 当前 `marked.parse(md)` 无 sanitize；接入 DOMPurify 或 `marked` 的安全选项；Claude 输出里混 `<script>` 会直接执行。
- [ ] **A3. 数据备份** — `data/method.sqlite` + `data/uploads/` + `data/plans/` 每日备份到 Google Drive（rclone 已配好，参考 `/cloud-backup` skill）。保留 30 天。
- [ ] **A4. Uptime 监控** — 建议 healthchecks.io 或 Better Stack，5 分钟探 `/api/health`。💬 用哪个平台？
- [ ] **A5. 磁盘空间告警** — 同上平台，`/home` 分区 > 80% 告警邮件。
- [ ] **A6. IP 级限流** — `/api/auth/request_code` 当前只有每邮箱 60 秒。加 IP 级 `slowapi` 中间件或在 CF 侧加 WAF rule。💬 在 CF Dashboard 还是服务端代码？
- [ ] **A7. 极简 `/privacy` 页 + 登录页底部链接** — 列收集项、使用方、留存期、删除渠道。明示 Claude 子进程会把内容发到 Anthropic（美国）。
- [ ] **A8. 账号注销按钮** — `/settings/account` 页；软删 user + 所有 research + 文件 + sessions + 评论；保留 30 天；admin 有恢复接口；30 天后 cron 硬删。（决策 2026-04-20：用户希望可恢复，除非合规硬要求立即硬删）

### 🟡 P1 — 第二波（目标：两周内）

- [ ] A9. CSP / X-Frame-Options / HSTS 响应头中间件
- [ ] A10. `BASE_URL` 强制校验 — `verify_origin` 当前 base_url 空时 permissive
- [ ] A11. journald 留存限制 — `SystemMaxUse=500M` 之类
- [ ] A12. CAPTCHA / bot 保护 — 登录页面对公网开放；目前邀请制但风险存在
- [ ] A13. 优雅重启 — 当前 systemctl restart 会杀掉 in-flight claude subprocess（踩过）
- [ ] A14. 线程僵尸：pdfplumber / openpyxl / python-pptx timeout 只取消 await、线程还在跑
- [ ] A15. 上传文件 ClamAV 扫毒（规模小可先不做）
- [ ] A16. 提示词注入说明 — 隐私页加"别上传高机密内容"提示
- [ ] A17. Anthropic API circuit breaker — 挂了时 fail fast
- [ ] A18. `uvicorn --workers` 调优 — 当前单 worker

### 🟢 P2

- [ ] A19. 2FA
- [ ] A20. 审计日志（谁什么时间 delete 了什么）
- [ ] A21. SESSION_SECRET / SMTP_PASSWORD 密钥轮换流程
- [ ] A22. Litestream（SQLite 增量复制到 S3，顺带备份）
- [ ] A23. 按用户 Claude cost ceiling
- [ ] A24. 按用户并发上限
- [ ] A25. 多副本 + 负载均衡
- [ ] A26. ICP / 公安备案（视用户范围）

---

## B. 新功能 1：选文评论 + AI 回复 🆕

详见 chat history 2026-04-20 的两功能讨论。

### 已定决策（2026-04-20）

- B-Q1 展示：**C**（两阶段：MVP-1 底部列表 / MVP-3 行内高亮）
- B-Q2 AI 触发：**X**（每条自动）
- B-Q3 AI 流式：**A**（流式 SSE）
- B-Q4 删除：**A**（级联软删、AI 回复不可单独删）
- B-Q5 节流：**D**（先不设，观察 1-2 周）
- **B-Q6 AI 上下文**：**A**（全套：原始问题 + 上传文件路径 + 方案 + 选段 + 评论）
- **B-Q7 failed 方案评论**：**A**（done + failed 都开放，AI 回复作为 bug report + 自我诊断入口）
- **B-Q8 模型**：**系统默认 Opus**（评论是 skill 进化燃料，不降级 Haiku；保留 `CLAUDE_COMMENT_MODEL` env 逃生门）
- **B-Q9 注销数据**：**软删 30 天可恢复**（联动 A8；合规硬要求时再改硬删）

### 当前进度（Session 3，2026-04-20 — MVP-1 已上线）

- **MVP-1 DONE**: merged `feat/issue-4-comments` → `main` (`e441fae`) + deployed via `scripts/promote-to-prod.sh --apply`
- 45/45 feature tests PASS；246/248 full regression PASS（2 个 E2E gated）；`/review` 12 PASS / 0 FAIL
- 已收到的真实用户反馈：错误文案（`rate_limit` / `invalid_or_expired`）裸出给用户是 UX bug —— 进 Issue #5（F4）
- **Pre-deploy 备份**: `/tmp/method-prod-backup-1776688495`（`/tmp` 重启会丢；之后改走新 `/deploy-prod` skill 持久备份）

MVP-2 / MVP-3 仍在下方 backlog。

### MVP backlog

- MVP-2：`/history` 卡片"💬 3"评论数徽标 + `cost_usd` 观察面板
- MVP-3：行内高亮（TreeWalker + `<mark>`）

### 实现大纲（等 B-Q1..Q5 定了再细化）

- 数据：新增 `comments` 表（id/request_id/user_id/parent_id/author/anchor_before/anchor_text/anchor_after/body/ai_status/created_at/deleted_at）
- 锚定方式：quote + 前后缀（hypothes.is 风格）
- 4 个 API：POST 创建 / GET 列表 / DELETE 软删 / SSE stream
- AI pipeline：复用 research_runner pub/sub；短 prompt；可考虑 haiku 降本

### 分阶段

- MVP-1：底部列表 + 非流式 AI 回复（2-3 天）
- MVP-2：AI 流式（半天）
- MVP-3：行内高亮（1-2 天）

---

## C. 新功能 2：用户级 Playbook 管理 🆕

### 待定的产品决策 💬

- [ ] **C-Q1. 草稿可编辑**：只能"通过/删除"，还是允许在线编辑生成的 markdown 再生效
- [ ] **C-Q2. 一份上传→几条 playbook**：1:1 还是允许 AI 拆成多条候选
- [ ] **C-Q3. 覆盖关系**：用户 playbook 是追加到 skill 自带 playbook，还是可覆盖同名
- [ ] **C-Q4. 适用模式**：通用 + 投资共用 / 仅投资先试 / 按 playbook 打标签选
- [ ] **C-Q5. 数量上限**：活跃 playbook 上限多少（影响 prompt 预算）
- [ ] **C-Q6. 生成质量**：给 Claude 限定严格 schema 还是允许自由发挥

### 实现大纲

- 文件布局：`data/playbooks/{user_id}/{drafts,active}/*.md` + `index.md`
- 数据：新增 `playbooks` 表（id/user_id/status/title/summary/content_path/source_files/applicable_modes/timestamps）
- 生成 pipeline：file_processor 提取文本 → 新 prompt 模板 `playbook_draft.j2` → claude subprocess → 落盘草稿
- 运行时接入：research 调用时 `--add-dir data/playbooks/{user_id}/active` + prompt 尾部提示
- 6 个 API：上传生成 / 列表 / 详情 / 生效 / 删除 / 生成进度 SSE

### 关键风险

- Prompt 注入：playbook 内容会注入到 research prompt。对策：固定包裹头 + 禁止覆盖 skill 指令
- Prompt 膨胀：让 Claude 用 Glob 按需读而非全量注入

### 分阶段

- MVP-1：上传→生成→预览→生效/删除（3-4 天）
- MVP-2：生成流式 + 运行时接入（1-2 天）
- MVP-3：草稿在线编辑（1-2 天）
- MVP-4：playbook 标签 + 按场景选择性注入（2 天）

---

## D. 横跨两个新功能的共同决策 💬

- [ ] **D-Q1. 数据持久化**：新功能（comments / playbooks）存在现有 `method.sqlite`，还是新建独立的表 / 文件布局？若存现有 DB，需要 `ALTER TABLE` 迁移策略（当前 `init_db` 只有 `CREATE TABLE IF NOT EXISTS`）
- [ ] **D-Q2. 隐私政策起草方**：你来写一段中文我放进去 / 我来写第一版你审
- [ ] **D-Q3. 数据出境通知形式**：只在 `/privacy` 页写一句 / 首次登录弹确认框
- [ ] **D-Q4. 失败重试的 UI 状态**：pending → failed 的状态怎么显示 + 重试按钮要不要有

---

## F. 部署流程与备份基础设施（Session 3 新开；优先于新功能）

Session 3 2026-04-20 定了"部署确定性化 + 备份完备性"的方向，draft 已出，待验证 + 补齐。

### 🔴 P0 — 下个 session 先做

- [x] **F1. `scripts/deploy.py --dry-run` 冒烟** ✓ Session 4 (2026-04-20) — bug fixes: REQUIRED_ENV_KEYS aligned with Method (DB_PATH not DATABASE_URL); preflight ignores docs/runs/. Phase A end-to-end PASS, gdrive backup at `gdrive:backups/method/20260420-231522-deploy-8fee480/`, report `docs/runs/20260420-231522-deploy-8fee480.md`.
- [x] **F2. 写 `/backup-restore-drill` skill + `scripts/restore_drill.py`** ✓ Session 4 (2026-04-20) — 6 phases (SELECT/DOWNLOAD/EXTRACT/BOOT/EXERCISE/CLEANUP), exercises GET /api/history (expect 401), Feishu notify on FAIL via `lark-cli im +messages-send --user-id`. Both `--source local` (~1s) and `--source gdrive` (~50s) PASS smoke.
- [ ] **F3. systemd timer 周一 03:00 CST 跑 drill**：unit 文件 `method-restore-drill.{service,timer}`，`systemctl --user enable` 或 root；失败通知走 F2 脚本内置的 Feishu 调用（`/feishu` skill 脚本化版）
- [x] **F4. Issue #5 前端 UX 错误文案映射** ✓ Session 4 (2026-04-20) — 24 后端码 + 7 前端 fallback + 3 模板 fallback 统一在 `app/services/error_copy.py::ERROR_COPY` + `message_for(code)`。后端响应统一为 `{"error": <code>, "message": <中文>}`；`app/main.py` 新增统一 `StarletteHTTPException` handler 覆盖 12 处 `not_found`；`app/static/app.js` `showError()` central renderer。`feat/issue-5-error-copy` 已 merge 到本地 `main`；47 tests / 96 cases / 1 xfail / 0 fail，full regression 342 PASS。**未推 remote、未 prod deploy**（F5 留给用户）。
- [x] **F5. 第一次用 `/deploy-prod` skill 做真实发布** ✓ Session 4 (2026-04-21 ~01:00 CST) — Issue #5 错误文案首发。两次迭代：(1) Phase A preflight 抓到 sub-agent 用 system python 缺 pytest，未碰 prod；修 SKILL.md mandate `.venv/bin/python`。(2) Phase A+B PASS、Phase C false positive on app.js mtime（rsync `-a` 保留 mtime）；prod 实测健康（17 users / 20 research / DB integrity ok / 0 errors / 新 error shape live）；修 freshness check 改为 md5 content-hash 比对。Backup: `gdrive:backups/method/20260421-010451-deploy-6bf0c2d/`。详见 `docs/runs/20260421-010451-deploy-6bf0c2d.md` + DEV_LOG Session 4 addendum L-S4.4/4.5/4.6。

### 🟡 P1

- [ ] **F6. 把 `~/.claude/` 的 CLAUDE.md + rules/ + skills/ 纳入 dotfiles git repo**。机器迁移 / 多机同步 / 回滚都方便；单独 repo，不混进项目 repo。
- [ ] **F7. 审`/tester` / `/review` / `/test-quality-check` / `/design-check` / `/run-tests` 这 5 个 sub-agent skill 的 "sub-agent 输出落盘" 合规度**：R4 说 sub-agent 必须写文件 + 回 ≤ 200 字摘要，目前至少 `/run-tests` 还在往主 agent 回全量 pytest 输出。逐一审，不达标就改 skill。

### 🟢 P2

- [ ] **F8. Deploy 预览环境**（非 prod 非 dev 的第三个环境 staging）：本来 dev 既当开发又当 QA，prod 出事只能回滚。staging 单独一份完整 prod 拷贝（含真实数据快照），`/deploy-prod` 可指定目标环境。
- [ ] **F9. Schema migration 机制**：当前 `init_db` 只 `CREATE TABLE IF NOT EXISTS`；新增列 / 改表结构需要显式 migration（Alembic 或手写 SQL），`/deploy-prod` Phase A 运行前跑 dry-run migration check。TODO D-Q1 的上级。

---

## E. 已完成的功能（reference，别再讨论）

- 扫描 PDF fix（原 kind=failed 改为 pdf_scan）
- mode selector（通用 / 投资 beta）
- 历史记录删除（DELETE /api/research + hover 的垃圾桶图标）
- pptx / xlsx / 5 种图片支持；前端粘贴截图；size gate；50 MB/100 MB/20 个上限
- 登录双提交 race 修复（按钮 disabled 到请求完成）
- Prompt 模板加 4-6 条"用户友好"规则（禁 Type A/B、禁学术黑话、省略问题类型分类章节）
- service 重启流程（检查 in-flight → `sudo systemctl restart method.service`）
- **B-MVP-1 选文评论 + AI 回复 MVP-1**（2026-04-20 Session 3 上线 `e441fae`）：4 个 API、45 个测试全绿、设计完整性审过、prod schema 已建表。UX 错误文案修和 MVP-2/MVP-3 仍在 B/F backlog。
- **全局规则架构重构**（2026-04-20 Session 3）：`~/.claude/CLAUDE.md` 瘦身到 70 行索引；细则落 `~/.claude/rules/`；skill + 脚本一律按项目走；`/design-check` 加 Category 14 用户可见文案 BLOCKING。

---

## 使用说明

- 聊新话题前问我："要聊 A / B / C / D 哪一块？"
- 每次只拿 1-2 条出来聊；全局 CLAUDE.md 规定每轮最多 1-3 个选择题
- 大块设计改动生成飞书文档让我审（CLAUDE.md 有规则）
- 每完成一项，勾掉并挪到 E 段落
