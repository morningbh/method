# Next Session Brief — 2026-04-21 Session 4 完整收官

> 你是主 agent，R1-R5 硬规则生效（`~/.claude/CLAUDE.md` + `~/.claude/rules/`）。只读这份 brief + `docs/INDEX.md` + `docs/TODO.md` + `docs/DEV_LOG.md` 尾部。**不读代码 / 设计文档正文 / 测试文件**。需要就派 sub-agent。

---

## 你在哪

- **cwd**: 编辑只在 `/home/ubuntu/method-dev/`（memory `project_env_split.md`）
- **prod status**: Issue #5 错误文案 **已上线** (commit `6bf0c2d`，`/deploy-prod` 跑过)。`https://method.xvc.com/api/health = 200`，新 error shape 在 prod 服务（`429 {"error":"rate_limit","message":"请求过于频繁，请稍后再试"}` 验过）。17 users / 20 research / DB integrity ok。Backup: `gdrive:backups/method/20260421-010451-deploy-6bf0c2d/`。
- **branch**: 本地 `main`（包含 Session 4 全部 commit）。`feat/issue-5-error-copy` 已 merge 到本地 main，可以删（`git branch -d feat/issue-5-error-copy`）也可以留。
- **远程 status**: 本地 main 比 `origin/main` 超前 N 个 commit（Session 4 都没推）。下次需要时 `git push origin main` —— **执行前必须用户确认**。
- **uncommitted working tree** 应该是干净的（如果不是，第一步检查 `git status`）。
- **dev `.env` 本地修改**（gitignored，不会推）: `SMTP_FROM_NAME=Method DEV`（Session 3 修）+ `BASE_URL=https://method-dev.xvc.com`（Session 4 末尾改，配套新建的永久 dev 域名）。
- **新增 dev 永久域名 `https://method-dev.xvc.com`**（Session 4 末尾搭）：阿里云 DNS A 记录 → 这台 VM → Nginx (`sites-enabled/method-dev.xvc.com`) → 8002。LE 证书 certbot 自动续期。robots.txt 禁索引。详见 `docs/ops/domain-setup.md` 末尾「Dev 域名」节。**今后改 dev 代码 = `git pull / commit + sudo systemctl restart method-dev.service` + 浏览器刷 `https://method-dev.xvc.com`**，不再需要临时隧道 / SSH 隧道 / 改 `.env` 的舞蹈。

---

## 上一轮干完的事（Session 4，2026-04-20）

1. **F1 ✅** `scripts/deploy.py --dry-run` PASS。修了 2 bug：`DATABASE_URL` → `DB_PATH`；preflight 忽略 `docs/runs/`。
2. **F2 ✅** `/backup-restore-drill` skill + `scripts/restore_drill.py`。`--source local` (~1s) 和 `--source gdrive` (~50s) 都 PASS。失败时 `lark-cli im +messages-send --user-id ou_...` 通知。
3. **F4 ✅** Issue #5 前端 UX 错误文案。完整走 10 步流程：preflight → design (`docs/design/issue-5-error-copy.md`) → design-check PASS → 飞书评审批 → tester (47 tests) → tqc PASS → dev loop GREEN (3 iter) → review PASS → DEV_LOG。merge 到本地 main。
4. **3 个 trivial WARN cleanup** 后再 verify：full regression 342 PASS / 1 xfail / 0 fail。
5. **新 dev 永久域名 `https://method-dev.xvc.com`** —— Aliyun DNS + Nginx + LE。改 dev = `git pull → sudo systemctl restart method-dev.service → 浏览器刷`，不再要临时隧道。
6. **F5 ✅** Issue #5 上线 prod。修了 2 个 deploy.py bug：(a) sub-agent prompt 漏了 `.venv/bin/python` (Phase A preflight 抓到，未碰 prod)；(b) Phase C `/static/app.js` 的 mtime 检查是假阳性（rsync `-a` 保留 mtime），改成 md5 content-hash 比对。prod 实测健康。

详见 `docs/DEV_LOG.md` Session 4 节。

---

## 这轮留给下次的 P0（按优先级）

### F3 — systemd timer 装 weekly drill

```
/etc/systemd/system/method-restore-drill.service
/etc/systemd/system/method-restore-drill.timer    (OnCalendar=Mon 03:00 Asia/Shanghai)
```

需要 sudo 写 `/etc/systemd/system/`。Session 4 没装是因为没用户在场授权。下个 session 用户在场就装。

unit 文件可以参考 `method-dev.service`（已存在 systemd）；`ExecStart=/home/ubuntu/method-dev/.venv/bin/python /home/ubuntu/method-dev/scripts/restore_drill.py --yes`。

失败通知：脚本内置 `lark-cli` Feishu DM。systemd `OnFailure=` 也可以挂一个备用 hook。

### B-MVP-2 / B-MVP-3（feature B comments 后续）

仍在 backlog（详见 TODO §B）。MVP-2 是评论数徽标 + cost_usd 面板；MVP-3 是行内高亮。优先级低于 F5。

---

## 硬约束（简版重申，细则在 `~/.claude/rules/`）

- **R1-R5 / `rules/main-agent-role.md`**：只读 INDEX；sub-agent 写文件；skill 是契约
- **`rules/deploy-discipline.md`**：部署必经 4 阶段；不允许主 agent 临时思考部署步骤
- **`rules/confirmation-policy.md`**：读文件不问；改非系统文件备份后直接改；不可逆系统操作（譬如 `git push`、systemd install）先确认
- **`rules/design-discussion-style.md`**：每轮 ≤ 3 个问题；多选题 A/B/C；长设计走飞书

---

## Kickoff prompt for new session

```
读 /home/ubuntu/method-dev/docs/NEXT_SESSION_BRIEF.md，然后按它说的走。
```

（如果想直接做 F5：「读 brief 然后跑 F5」。如果想先 F3：「读 brief 然后装 systemd timer，sudo 我会手动给」。）

---

## 联系 / 通知

- 状态更新发飞书给用户：`lark-cli im +messages-send --user-id ou_d9e4f77e8e63ddf2e32677fb72b1435b --text "..."` (HOME=/home/ubuntu/.lark-cli-claude)
- 测试邮件：`bhocbot@gmail.com`（agent 自己可读）或 `h@xvc.com` / `morningwilliam@gmail.com`（用户自己）
- **永远不要**把测试邮件发给真实用户
