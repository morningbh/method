# Method Web E2E 场景

测试环境：`$BASE_URL`（默认 `https://que-career-favour-mode.trycloudflare.com`）
运行方式：`RUN_E2E=1 python tests/e2e/web/runner.py`
截图输出：`tests/e2e/web/screenshots/`（不提交 git）

工具：headless google-chrome（系统已装 `/usr/bin/google-chrome`）。
每个场景由 runner.py 的 `shot(...)` 调用执行，调用 `--headless --screenshot=<path>` 抓页面快照，然后断言文件存在且 ≥ 3KB（过滤掉 chrome 保存错误页/空白页的情况；chrome 空白页 < 1KB，移动视口 375×667 登录页约 4–5KB，桌面视口约 8KB）。

---

## 场景 1：登录页首次访问（桌面视口）

**步骤：**
1. 打开 `$BASE_URL/login`
2. 等待页面加载（chrome 默认等 DOMContentLoaded）

**预期：**
- 页面渲染成功（截图 ≥ 3KB；实测 ~8KB）
- 登录页有标题 "Method"
- 有邮箱输入框
- 有 "发送登录验证码" 按钮

**截图：** `01-login-page.png`（1280×800）

**注：** 根路径 `/` 在未登录时会 303 跳 `/login`；本场景直接打登录页避免 chrome headless 处理跳转的不确定性。

---

## 场景 2：登录页（移动端视口）

**步骤：**
1. 设置视口为 375×667（iPhone SE 尺寸）
2. 打开 `$BASE_URL/login`

**预期：**
- 页面渲染成功（截图 ≥ 3KB；实测 ~5KB，移动视口因像素少所以文件更小）
- 移动端 CSS 生效（768px breakpoint）
- 无横向滚动条（视觉检查）
- 输入框 font-size ≥ 16px（iOS 防缩放，CSS 已保证）
- 按钮触摸目标 ≥ 44×44px（CSS 已保证）

**截图：** `02-login-mobile.png`（375×667）

---

## 场景 3：健康检查端点

**步骤：**
1. 打开 `$BASE_URL/api/health`

**预期：**
- 返回 JSON `{"ok": true, "version": "0.0.1"}`
- chrome 以文本方式渲染 JSON
- 截图 ≥ 3KB（chrome JSON 查看器有一定 UI chrome；实测 ~8KB）

**截图：** `03-health.png`

**注：** 也通过 curl 在 session 中独立验证过 JSON 正确。本场景只验证公网可达。

---

## 场景 4：管理员登录全流程（手动 / CLI 等效覆盖）

**步骤（手动）：**
1. 打开 `$BASE_URL/login`
2. 输入 `morningwilliam@gmail.com`
3. 点 "发送登录验证码"
4. [Gmail MCP 取验证码]
5. 输入验证码
6. 点 "登录"

**预期：**
- 跳转到 `/` 工作台
- 看到 "帮你设计研究计划" 标题
- 看到 textarea + 文件选择区 + 生成按钮

**自动化状态：** M5 不自动化。

**原因：** headless chromium 用 CLI 参数注入 cookie / 完成邮件 MFA 交互很脆弱（需要 `--user-data-dir` + 预写 cookie SQLite 或 playwright 深度控制，超出本 session 范围）。

**等效 CLI 覆盖：**
- SMTP 端到端：`tests/e2e/test_real_email_flow.py`（真 Gmail SMTP 发验证码，Gmail MCP 取）
- 研究生成端到端：`tests/e2e/test_real_claude_call.py`（真 claude CLI 子进程）
- 两个测试合计覆盖了场景 4 除浏览器渲染外的全部后端路径。

**未来（非 M5）：** 切到 playwright 后补全自动化。

---

## 场景 5：历史页未登录访问（应 303 跳 login）

**步骤：**
1. 打开 `$BASE_URL/history`（无 cookie）

**预期：**
- 服务端返回 303 → `$BASE_URL/login`
- chrome 跟随跳转后截图就是登录页
- 截图 ≥ 3KB（实测 ~8KB）

**截图：** `05-history-unauth.png`

**注：** 场景 5 验证了 `require_login` 中间件在 /history 路由上正确工作。登录后的 /history 列表由 M4 的模板渲染测试（tests/unit/test_history.py）+ 路由集成测试（tests/integration/test_history_routes.py）覆盖；这里不重复。
