# Next Session Brief — 2026-04-20 Session 3 暂停点

> 你是主 agent，R1-R5 硬规则生效（`~/.claude/CLAUDE.md` + `~/.claude/rules/`）。只读这份 brief + `docs/INDEX.md` + `docs/TODO.md` + `docs/DEV_LOG.md` 尾部。**不读代码 / 设计文档正文 / 测试文件**。需要就派 sub-agent。

---

## 你在哪

- **cwd**: 按惯例 `/home/ubuntu/method` 或 `/home/ubuntu/method-dev`；**编辑只在 method-dev**（memory 里有记 `project_env_split.md`）
- **prod status**: 已上线 feature B MVP-1，commit `e441fae`，`https://method.xvc.com/api/health = 200`，prod schema 含 `comments` 表
- **branch**: `main`（干净，上次 merge 是 `e441fae`）
- **uncommitted working tree**（这次 handover 故意留着让下个 session 决定：） 
  ```
  ?? .claude/                                         (project-local skill dir)
  ?? docs/runs/20260420-195611-smoke-dev-featureB.md  (dev e2e smoke 报告)
  ?? scripts/deploy.py                                (draft deploy script, 未冒烟)
  M  docs/DEV_LOG.md                                  (Session 3 entry)
  M  docs/TODO.md                                     (Section F 新开 + Section E 勾选 MVP-1)
  M  docs/NEXT_SESSION_BRIEF.md                       (本 brief)
  ```
- **dev `.env` 本地修改**（gitignored，不会推）: `SMTP_FROM_NAME=Method DEV`（原来是带方括号的 `Method [DEV]`，让 `email.utils.parseaddr` 解析失败）
- **其他 infra 改动（全局，不在 git）**:
  - `~/.claude/CLAUDE.md` 瘦身到 70 行
  - `~/.claude/rules/` 新建（INDEX + 8 rule files）
  - `~/.claude/skills/design-check/SKILL.md` 加了 Category 14（用户可见文案 BLOCKING）

---

## 第一步

**先 commit handover** —— 把上面列的 4 个未 track 文件 + 3 个改动文件做一个干净的 WIP commit。命令可以直接跑（用户 Session 3 授权了此次 handover 的 commit 范围）：

```bash
cd /home/ubuntu/method-dev
git add .claude/ scripts/deploy.py \
        docs/DEV_LOG.md docs/TODO.md \
        docs/NEXT_SESSION_BRIEF.md \
        docs/runs/20260420-195611-smoke-dev-featureB.md
git commit -m "chore(infra): /deploy-prod skill draft + Session 3 handover

- New .claude/skills/deploy-prod/ (project-local contract)
- New scripts/deploy.py (4-phase deterministic deploy, NOT YET SMOKE-TESTED)
- DEV_LOG Session 3 entry (deploy retrospective + infra refactor)
- TODO Section F (F1-F9 deploy infra backlog)
- NEXT_SESSION_BRIEF refreshed for pickup

Draft; Phase A dry-run is first task next session (F1).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

（如果要分多次 commit 可以，但上面是最省事的一口吃法。）

---

## 接下来的工作（从 `docs/TODO.md` Section F 抄出来，按优先级）

### F1 — `scripts/deploy.py --dry-run` 冒烟（今天头等事）

```
cd /home/ubuntu/method-dev
.venv/bin/python scripts/deploy.py --dry-run --skip-human-smoke --yes
```

`--skip-human-smoke` 是因为 F1 只是验证 Phase A 管线，不是真上线；`--dry-run` 就不会进 Phase B/C。期望：
- `/home/ubuntu/backups/<ts>-deploy-<sha>/` 本地生成，含 `code/` `db/` `env/` `uploads/` `plans/`
- DB `PRAGMA integrity_check = ok`；行数对齐
- rclone 成功传到 `gdrive:backups/method/<ts>-deploy-<sha>/`
- 报告写到 `docs/runs/<ts>-deploy-*.md`

有 bug 就改 `scripts/deploy.py`，直到跑通。bug 可能点：

1. pytest.main 返回非 0（deploy.py 把测试当 gate 但我们这次已经知道 246/248 PASS，所以应该绿；可能因为 import path 问题要调整）
2. rclone 到 `gdrive:backups/method/` 首次可能要先 `rclone mkdir gdrive:backups/method`
3. `sudo journalctl` 在 phase C 需要免密——这个 dry-run 不会触发，phase B/C 后面才验

### F2 — 写 `/backup-restore-drill` skill + `scripts/restore_drill.py`

目录：`.claude/skills/backup-restore-drill/SKILL.md` + `scripts/restore_drill.py`。

设计要点：
- 输入：`--source gdrive` 或 `--source local`，默认 `gdrive`
- 动作：
  1. `rclone lsl gdrive:backups/method/` 取最新一份
  2. 下载到 `/tmp/restore-drill-<ts>/`
  3. 起独立 uvicorn（随机端口如 9999）指向备份的 sqlite
  4. `curl /api/health` + 1 个读 API（譬如 `/api/history` 如果有 cookie 就跳）
  5. SIGTERM + 清理 `/tmp/`
- 输出：`docs/runs/<ts>-restore-drill.md`，退出 0/1
- 失败通知：内置 Feishu bot（用户聊天 ID `ou_d9e4f77e8e63ddf2e32677fb72b1435b`，或走 `/feishu` skill 脚本化）

### F3 — systemd timer

周一 03:00 CST 跑 drill；位置 `/etc/systemd/system/method-restore-drill.{service,timer}`（需要 sudo 装）。Template 见 `method-dev.service` 对照。

### F4 — Issue #5 前端 UX 错误文案

按照 `~/.claude/skills/design-check/SKILL.md` 的 **Category 14** 走完整 10 步流程：

- Step 2a：设计稿里必须有"错误码 → 中文 copy"映射表（grep 出所有 `{"error": "..."}` 后端返回点作为覆盖基线，至少 `rate_limit` / `bad_origin` / `invalid_or_expired` / `body_invalid` / `anchor_text_invalid` / `anchor_context_too_long` / 5xx / 网络异常 / 上传失败）
- 设计定：后端加 `message` 字段 还是 前端字典 —— 推荐后端加（错误码和文案同源，前端 dumb render）
- Step 2b 飞书审一下映射表是否每条文案都能自洽

---

## 硬约束（简版重申，细则在 `~/.claude/rules/`）

- **R1-R5 / `rules/main-agent-role.md`**：只读 INDEX；sub-agent 写文件；skill 是契约
- **`rules/deploy-discipline.md`**：部署必经 4 阶段，Phase A 必须备份 + 验证 + 异地 + 脚本化；不允许主 agent 临时思考部署步骤
- **`rules/confirmation-policy.md`**：读文件不问；改非系统文件备份后直接改；不可逆系统操作先确认
- **`rules/design-discussion-style.md`**：每轮 ≤ 3 个问题；多选题 A/B/C；长设计走飞书

---

## Kickoff prompt for new session

（把下面这条贴进新的 Claude Code 会话，复用今天这种"极简入口 + brief-driven"的模式）：

```
读 /home/ubuntu/method-dev/docs/NEXT_SESSION_BRIEF.md，然后按它说的走。
```

（主 agent 读到这里就知道：先 commit handover，然后 F1 dry-run，然后 F2 写 drill skill。如果你想跳过 F1 直接进别的任务，在 kickoff 里加一句例如 "跳过 F1 直接做 F4"。）

---

## 联系 / 通知

- 状态更新发飞书给用户：`/feishu` skill，chat id `ou_d9e4f77e8e63ddf2e32677fb72b1435b`
- 测试邮件：`bhocbot@gmail.com`（agent 自己可读，见 memory `reference_test_email_bhocbot.md`）或 `h@xvc.com` / `morningwilliam@gmail.com`（用户自己的收件箱）
- **永远不要**把测试邮件发给真实用户
