# Method 域名配置指南（`method.xvc.com`）

**目标**：把临时的 `https://<random>.trycloudflare.com` 换成永久的 `https://method.xvc.com`。

**当前状态**：
- 域名 `xvc.com`：已在中国大陆 ICP 备案；DNS 解析在**阿里云**
- 服务器：腾讯云，`method` + `cloudflared` 两个 systemd 服务都 active
- 隧道模式：quick tunnel（每次重启换 URL，对磁链接邮件不友好）
- 目标模式：**Cloudflare Named Tunnel**（永久 URL + 免费 HTTPS + 无需开端口）

---

## 核心方案：子域名委托（NS Delegation）

**不动 `xvc.com` 的主域解析**。只在阿里云 DNS 里给 `method` 这个子域加两条 NS 记录，把它指到 Cloudflare。Cloudflare 只管 `method.xvc.com` 这一个子域名。

| 受影响 | 不受影响 |
|---|---|
| ✓ `method.xvc.com` 解析切到 Cloudflare | ✗ `xvc.com` ICP 备案 |
| ✓ 通过 Cloudflare Tunnel 暴露服务 | ✗ `xvc.com` 主域 A/CNAME/MX 记录 |
| | ✗ 其它子域（`www.xvc.com`, `mail.xvc.com` 等） |

**合规性**：服务器仍在腾讯云（已备案主体），源站不变；只是对外入口走 Cloudflare。ICP 备案的主体关系与子域单独托管的 DNS 不冲突。

**性能提示**：Cloudflare 免费版走**全球 Edge**，国内用户主要命中香港/新加坡节点。速度比阿里云直连略慢（30-80ms vs 10-20ms），但对研究方案这类 2-4 分钟生成的异步业务完全够用。如果以后要国内优化，可升级 Cloudflare China Network（约 $200/月，走京东云国内节点）。

---

## 全流程（3 大步，~15 分钟）

1. **Cloudflare 侧**：开账号 → 加 `method.xvc.com` 子域 zone → 拿到 2 个 Cloudflare nameservers
2. **阿里云 DNS 侧**：在 `xvc.com` zone 里加 `method` 的 NS 委托记录
3. **服务器侧**：创建 named tunnel → 配 ingress → 改 systemd → 改 `.env` → 重启

---

## 步骤 1：Cloudflare 侧设置

### 1.1 注册/登录 Cloudflare
打开 https://dash.cloudflare.com，没账号就用邮箱注册一个，免费。

### 1.2 添加子域 zone
- 点 `Add a site`
- 输入 **`method.xvc.com`**（注意不是 `xvc.com`，是带子域的完整名字）
- 选 **Free plan**
- 它会提示 DNS 检测，**跳过/Confirm** 即可（因为子域名还没有解析）
- 完成后，Cloudflare 会给你 2 个 nameservers，形如：
  ```
  xxx.ns.cloudflare.com
  yyy.ns.cloudflare.com
  ```
  **把这两个记下**，步骤 2 要用。

> 如果页面提示 "This zone already exists" 或要求加整个 `xvc.com`，说明你误把主域加进去了。删除重来，添加时精确输入 `method.xvc.com`。Cloudflare 在 Free plan 支持子域名 zone。

---

## 步骤 2：阿里云 DNS 侧加 NS 委托

登录阿里云控制台 → **云解析 DNS** → 进 `xvc.com` 域名的解析页。

加两条记录：

| 主机记录 | 记录类型 | 解析线路 | 记录值 | TTL |
|---|---|---|---|---|
| `method` | **NS** | 默认 | `xxx.ns.cloudflare.com.`（步骤 1.2 那两个的第一个，末尾带点） | 600 |
| `method` | **NS** | 默认 | `yyy.ns.cloudflare.com.`（第二个，末尾带点） | 600 |

> 末尾的 `.` 有些阿里云界面自动补；如果不补就报错，手动加上。

等 5-10 分钟 DNS 生效。

验证（在服务器上或本地）：
```bash
dig NS method.xvc.com +short
```
应返回你在 Cloudflare 面板看到的那俩 NS。如果还是旧的或空的，就再等一下。

---

## 步骤 3：服务器上配置 tunnel

### 3.1 登录 cloudflared

```bash
ssh ubuntu@<服务器IP>
cloudflared tunnel login
```

终端打出一个 URL → 复制到本地浏览器打开 → Cloudflare 页面选 **`method.xvc.com`** → 授权。

服务器终端看到 `You have successfully logged in.` 即成功。凭据在 `~/.cloudflared/cert.pem`。

### 3.2 创建 named tunnel

```bash
cloudflared tunnel create method
```

输出形如：
```
Tunnel credentials written to /home/ubuntu/.cloudflared/<tunnel-id>.json
Created tunnel method with id <tunnel-id>
```

**记下 `<tunnel-id>`**（36 字符 UUID）。

### 3.3 路由 DNS

```bash
cloudflared tunnel route dns method method.xvc.com
```

这一步会自动在 Cloudflare 的 `method.xvc.com` zone 里加一条 `CNAME → <tunnel-id>.cfargotunnel.com`。

**在 Cloudflare 面板 → method.xvc.com → DNS 里确认这条记录存在（橙色云 = proxied）**。

### 3.4 写 tunnel 配置文件

```bash
cat > ~/.cloudflared/config.yml <<'EOF'
tunnel: <tunnel-id>             # 替换成 3.2 的 UUID
credentials-file: /home/ubuntu/.cloudflared/<tunnel-id>.json

ingress:
  - hostname: method.xvc.com
    service: http://localhost:8001
  - service: http_status:404
EOF
```

**把 `<tunnel-id>` 替换成 3.2 拿到的 UUID**（两处）。

### 3.5 前台测一下

```bash
cloudflared tunnel --config ~/.cloudflared/config.yml run method
```

看到 `Registered tunnel connection` × 4 条 + 没 error 即 OK。

**新开一个终端**（或本地）跑：
```bash
curl -fsS https://method.xvc.com/api/health
```
预期：`{"ok":true,"version":"0.0.1"}`

回到 tunnel 前台 **Ctrl+C 停掉**。

### 3.6 切 systemd unit

```bash
sudo sed -i 's|ExecStart=.*|ExecStart=/usr/local/bin/cloudflared tunnel --config /home/ubuntu/.cloudflared/config.yml run method|' /etc/systemd/system/cloudflared.service

sudo systemctl daemon-reload
sudo systemctl restart cloudflared
sleep 3
systemctl status cloudflared --no-pager | head -15
```

状态应是 `active (running)`，日志里有 `Registered tunnel connection`。

### 3.7 更新 `.env` + 重启 method

```bash
sed -i 's|^BASE_URL=.*|BASE_URL=https://method.xvc.com|' /home/ubuntu/method/.env
sudo systemctl restart method
sleep 2
curl -sfS https://method.xvc.com/api/health
```

预期：`{"ok":true,"version":"0.0.1"}`

---

## 验收

- [ ] `dig NS method.xvc.com` 返回 Cloudflare 的 NS（NS 委托生效）
- [ ] `https://method.xvc.com` 浏览器打开 → 跳 `/login`
- [ ] 登录页 UI 正常（cream 背景 + tangerine 按钮）
- [ ] 邮箱 + 验证码登录，进工作台
- [ ] 提一个研究问题，历史页能看完整结果
- [ ] 审批邮件里链接是 `https://method.xvc.com/admin/approve?token=...`
- [ ] 重启整机 `sudo reboot`，起来后 URL 不变、服务自动恢复
- [ ] `xvc.com` 的其它子域名（比如 `www.xvc.com` 如果有）**不受影响**

---

## 回滚

### 快速回到 quick tunnel（服务器侧）

```bash
sudo sed -i 's|ExecStart=.*|ExecStart=/usr/local/bin/cloudflared tunnel --url http://127.0.0.1:8001|' /etc/systemd/system/cloudflared.service
sudo systemctl daemon-reload
sudo systemctl restart cloudflared
sleep 5
journalctl -u cloudflared --since "1 minute ago" | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1
# 把这个 URL 写回 .env BASE_URL，重启 method
```

### 完全放弃 method.xvc.com

- 阿里云 DNS：删掉刚加的两条 NS 记录
- Cloudflare：删掉 `method.xvc.com` zone（不收费不用删，留着也没影响）
- 服务器：按上面的快速回滚

---

## 常见问题

**Q: Cloudflare 能不能直接 CNAME 到 `<tunnel-id>.cfargotunnel.com`，不做 NS 委托？**
A: 不行。Cloudflare Tunnel 的 TLS 证书颁发依赖它对该域名的 DNS 控制权。不做 NS 委托，访问会报证书不匹配。

**Q: NS 委托后，阿里云上 `xvc.com` 主域的记录还能改吗？**
A: 能，完全不受影响。NS 委托只把 `method.xvc.com` 这一个子域的解析交给 Cloudflare；其它记录（`xvc.com` A 记录、`www.xvc.com`、MX 等）都还在阿里云。

**Q: ICP 备案会因此失效吗？**
A: 不会。备案挂在主体 + 域名本身，不关心 DNS 提供商。你只是换了 `method.xvc.com` 这一个子域的解析服务商。

**Q: `cloudflared tunnel route dns` 报 `zone not found`？**
A: 步骤 1.2 或 2 没做完。检查：
- Cloudflare 里确认 `method.xvc.com` zone 状态是 Active（不是 Pending Nameserver Update）
- `dig NS method.xvc.com` 确认返回 Cloudflare 的 NS

**Q: 访问报 502 / 530？**
A: 检查 `method.service` 是否 active（`systemctl is-active method`）。它监听 `127.0.0.1:8001`。

**Q: 国内访问慢怎么办？**
A: 免费版 Cloudflare 走境外 Edge，典型延迟 30-80ms，首字节 <500ms。如果慢到影响体验：
- Cloudflare → `method.xvc.com` → Speed → 开 Auto Minify + Brotli
- 继续慢就考虑 Cloudflare Pro/Business 套 China Network，或者改用腾讯云 EdgeOne（同服务商生态更一致）

**Q: 让 Claude 来做？**
A: 步骤 1（Cloudflare 网页操作）和步骤 2（阿里云 DNS）必须你本人点，之后步骤 3 全程我可以接管。告诉我 "前两步我做完了，你来跑步骤 3"。

---

## 事后可选

- Cloudflare 免费版自带基础 WAF / DDoS 保护
- 启用 `Always Use HTTPS`（Cloudflare → SSL/TLS → Edge Certificates）
- 开 `method.xvc.com` 的访问分析（Cloudflare Analytics → Traffic）
- 监控：`/api/health` 接 UptimeRobot，5 分钟一探
