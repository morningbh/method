# Method — Docs Index

> 渐进式披露：主 agent 只读这个索引；任何一行都可以通过 sub-agent 或专门的 skill 进一步打开。禁止主 agent 直接读下面任何文档的正文内容。

## 下次 session 第一份要读的文档

- [`NEXT_SESSION_BRIEF.md`](NEXT_SESSION_BRIEF.md) — 2026-04-20 暂停点：在哪、做什么、R1-R5 硬约束、skill 启动清单。主 agent 新 session 应先读这份。

## 项目约束与流程

- [`HARNESS.md`](HARNESS.md) — 项目级硬约束：research_requests 失败不静默、所有路径绝对路径、claude subprocess 工具白名单、session cookie flags、e2e 测试 opt-in；项目级 10 步流程（Step 5 跳过）
- [`AGENT_CONTEXT.md`](AGENT_CONTEXT.md) — 给 sub-agent 的背景 briefing
- [`TESTER_PROMPT.md`](TESTER_PROMPT.md) — `/tester` skill 的 Method 扩展：固定用 `db_session`/`app_client` fixture、SMTP 用 aiosmtpd、claude subprocess 用 monkeypatch、e2e 独立目录
- [`CODE_REVIEW_PROMPT.md`](CODE_REVIEW_PROMPT.md) — `/review` skill 的 Method 扩展
- [`TODO.md`](TODO.md) — 所有未完成的事（功能 / 合规 / 安全 / 稳定 5 大块 + 当前 Issue 进度）

## 工作运维

- [`ops/dev-prod.md`](ops/dev-prod.md) — dev/prod 环境布局（同一台 VM，8001 prod / 8002 dev）、`scripts/promote-to-prod.sh` 工作流、systemd 命令
- [`ops/domain-setup.md`](ops/domain-setup.md) — 域名 + Cloudflare tunnel 配置记录
- [`DEV_LOG.md`](DEV_LOG.md) — 每次会话的决策和教训；按时间倒序追加

## 功能 / 模块设计（只读入口，主 agent 禁止读正文）

- [`design/issue-1-task-2.3-auth-flow.md`](design/issue-1-task-2.3-auth-flow.md) — M2 Task 2.3：auth_flow service
- [`design/issue-1-task-2.4-auth-routes.md`](design/issue-1-task-2.4-auth-routes.md) — M2 Task 2.4：auth HTTP routes
- [`design/issue-1-task-2.5-e2e-smtp.md`](design/issue-1-task-2.5-e2e-smtp.md) — M2 Task 2.5：e2e + SMTP harness
- [`design/issue-2-task-3.1-file-processor.md`](design/issue-2-task-3.1-file-processor.md) — M3 Task 3.1：file_processor
- [`design/issue-2-task-3.2-claude-runner.md`](design/issue-2-task-3.2-claude-runner.md) — M3 Task 3.2：claude_runner subprocess wrapper
- [`design/issue-2-task-3.3-research-routes.md`](design/issue-2-task-3.3-research-routes.md) — M3 Task 3.3：research endpoints + SSE
- [`design/issue-3-m4-frontend-ui.md`](design/issue-3-m4-frontend-ui.md) — M4 Frontend UI
- [`design/issue-4-feature-b-comments.md`](design/issue-4-feature-b-comments.md) — **Issue #4 功能 B**：选文评论 + AI 回复（v2.1 当前实施中；飞书审核版：https://www.feishu.cn/docx/O37Ndd5BRofarhx40X5cDhPBnpg）

## 部署工件

- `superpowers/specs/2026-04-19-method-research-planner-design.md` — 项目首轮整体设计（参考）
- `superpowers/plans/2026-04-19-method-implementation-plan.md` — 项目首轮实施计划（参考）
- `scripts/promote-to-prod.sh` — dev → prod 发布脚本（带 in-flight 检查、备份、重启、健康验证）

---

## 给未来 session / sub-agent 的约定

如果你是主 agent：你**只读这个文件**。任何更细的内容 spawn sub-agent。
如果你是 sub-agent：你可以读这里指向的任何文件 + 代码；结果写入你的约定文件路径，不要长文返给主 agent。
