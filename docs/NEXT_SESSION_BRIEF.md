# Next Session Brief — 2026-04-20 暂停点

> 你是主 agent，只读这份 brief + `docs/INDEX.md` + `docs/TODO.md`。**不要读设计文档正文、代码、测试文件**。需要看就派 sub-agent。参见 `~/.claude/CLAUDE.md` 的 "Main Agent Role — Dispatcher Only" 五条硬规则 (R1–R5)。

## 你在哪

- **cwd**: `/home/ubuntu/method-dev/`
- **branch**: `feat/issue-4-comments`
- **last commit**: `docs: checkpoint session 2 — INDEX.md, DEV_LOG entry, TODO refresh`
- **feature**: Issue #4 功能 B（选文评论 + AI 回复），Step 7 dev loop 中
- **test status**（上次停之前）: 40/45 passing；5 个 failing 的 fix 已写代码**未验证**

## 第一步

让 `/run-tests` 跑这两个文件：

```
tests/unit/test_comment_runner.py
tests/integration/test_comment_endpoints.py
```

期望：**45/45 PASS**（理由：5 个 fix 都落盘了）。

## 如果 45/45 PASS

继续走项目级 10 步流程（Step 5 跳过，`docs/HARNESS.md` 有调整说明）：

1. `/run-tests` 跑全项目回归 — 确认没动到别的测试
2. `/review #B` — Step 8 代码评审（sub-agent，不要自己评）
3. 根据 review 结果修 FAIL 点（若有）
4. DEV_LOG 里补 Session 2 的最终提交摘要（Step 9）
5. Merge `feat/issue-4-comments` → `main`
6. `./scripts/promote-to-prod.sh` 先 dry-run，看 diff → `--apply` 推到生产
7. 重启生产 `sudo systemctl restart method.service`，health check
8. Feishu bot 通知用户：功能上线

## 如果还有 failing

看 `docs/TODO.md` 的"当前进度（Session 2 暂停点）"段落，里面列了 5 个 failing 的具体 fix 意图。如果 fix 本身不够，调整实现或测试代码。**不要在主 agent 里直接写实现**：大块改动派 sub-agent。

## 硬约束重申（R1–R5，来自 `~/.claude/CLAUDE.md`）

- 读：`INDEX.md` / `TODO.md` / `HARNESS.md` 短文 / `DEV_LOG.md` 尾部 / 本 brief
- 不读：设计文档正文、`app/**/*.py`、测试文件、模板、静态资源
- 所有 sub-agent 用 skill 启动（`/tester` / `/review` / `/run-tests` / `/design-check` 等）
- 主 agent 的 sub-agent 启动消息：1-3 行，只说"哪个 skill + 参数 + 为什么"，不贴 prompt 正文
- sub-agent 结果落文件（让 skill 决定路径），主 agent 只看 `{VERDICT, ≤200 字摘要, 报告文件路径}`
- 没对应 skill 的任务 → 停下，提示用户创建 skill

## 已知 skill 缺口（有精力再做）

- **`/implement-from-tests`**（Step 7 dev loop）：吃 test 文件列表 + design doc，产出最小实现让 RED → GREEN，输出到文件。上次是主 agent 自己写的实现 → 上下文爆了。**如果上面第一步 GREEN 了就跳过这条**；若要继续别的 feature 时再补。

## 联系 / 通知

- 发给用户的状态用 `/feishu` skill（bot 身份）发 `ou_d9e4f77e8e63ddf2e32677fb72b1435b`
- 测试邮件只发 `h@xvc.com` / `morningwilliam@gmail.com`，永远不碰真实用户
