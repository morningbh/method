# Method 域名配置指南（`method.xvc.com`）

**目标**：把临时的 `https://<random>.trycloudflare.com` 换成永久的 `https://method.xvc.com`。

**当前状态**：
- 服务器：腾讯云，`method` + `cloudflared` 两个 systemd 服务都 active
- 域名：`method.xvc.com`（待配）
- 隧道模式：quick tunnel（每次重启换 URL，对磁链接邮件不友好）
- 目标模式：**Cloudflare Named Tunnel**（永久 URL + 免费 HTTPS + 无需开端口）

**不推荐另外两种方案**：
- ❌ A 记录 + Nginx + Let's Encrypt：要开 443/80、要维护证书、腾讯云在国内走公网还有备案问题
- ❌ Cloudflare 代理 + 公网 IP：同样要暴露端口

---

## 总流程（3 大步）

1. **Cloudflare 侧准备**：确保 `xvc.com` 在 Cloudflare 管理
2. **服务器侧配置**：登录 cloudflared → 创建 named tunnel → 路由 DNS → 切 systemd
3. **更新 `.env` 里 `BASE_URL`** + 重启 `method` 服务

全程大约 10 分钟。所有命令都在服务器上跑（`ssh ubuntu@<服务器 IP>`）。

---

## 预备：`xvc.com` 是否已托管在 Cloudflare？

打开 https://dash.cloudflare.com → 看 Websites 列表。

### 情况 A：`xvc.com` 已经在 Cloudflare

跳到下一节「服务器侧配置」。

### 情况 B：`xvc.com` 不在 Cloudflare

两个选择：

**B.1 把 `xvc.com` 整个托管到 Cloudflare**（推荐，彻底）：
1. Cloudflare 面板 → Add a site → 输 `xvc.com` → 选 Free plan
2. Cloudflare 会扫描现有 DNS，你确认一遍
3. 记下 Cloudflare 给的 2 个 nameservers（形如 `xxx.ns.cloudflare.com`）
4. 到你 `xvc.com` 现在的注册商（GoDaddy / 阿里云 / Namecheap）把 nameservers 改成 Cloudflare 的那俩
5. 等 DNS 生效（通常 10 分钟 - 几小时）
6. 回到 Cloudflare 面板，看到 `xvc.com` 状态变 Active ✓

**B.2 只用一条 CNAME 指过去，不换 nameservers**：
- 不需要把整个 `xvc.com` 交给 Cloudflare
- 但 **Cloudflare Named Tunnel 必须能自动管 DNS 记录**，所以这条路**不行**。还是得走 B.1。

---

## 步骤 1：服务器上登录 cloudflared

```bash
ssh ubuntu@<服务器IP>
cloudflared tunnel login
```

终端会打出一个 URL，**复制到本地浏览器**打开。Cloudflare 页面里选 `xvc.com` → 授权。

服务器终端看到 `You have successfully logged in.` 即成功。凭据保存在 `~/.cloudflared/cert.pem`。

---

## 步骤 2：创建 Named Tunnel

```bash
cloudflared tunnel create method
```

输出类似：
```
Tunnel credentials written to /home/ubuntu/.cloudflared/<tunnel-id>.json
Created tunnel method with id <tunnel-id>
```

**记下这个 `<tunnel-id>`**（36 字符 UUID）。

---

## 步骤 3：路由 DNS

```bash
cloudflared tunnel route dns method method.xvc.com
```

这条命令会自动在 Cloudflare 上加一条 CNAME：`method.xvc.com → <tunnel-id>.cfargotunnel.com`。

成功后在 Cloudflare 面板 → `xvc.com` → DNS 里能看到这条记录（Proxied 橙色云图标）。

---

## 步骤 4：写 tunnel 配置文件

```bash
cat > ~/.cloudflared/config.yml <<'EOF'
tunnel: <tunnel-id>               # 替换成上面拿到的 UUID
credentials-file: /home/ubuntu/.cloudflared/<tunnel-id>.json

ingress:
  - hostname: method.xvc.com
    service: http://localhost:8001
  - service: http_status:404
EOF
```

把 `<tunnel-id>` 替换成你自己的（两处）。

测试一下配置：
```bash
cloudflared tunnel --config ~/.cloudflared/config.yml run method
```

看到 `Registered tunnel connection` × 多条 + 没 error 即 OK。访问 https://method.xvc.com/api/health 应返回 `{"ok":true,"version":"0.0.1"}`。

按 `Ctrl+C` 停掉前台运行。

---

## 步骤 5：切 systemd unit

当前的 `cloudflared.service` 跑的是 quick tunnel。换成 named tunnel：

```bash
sudo sed -i 's|ExecStart=.*|ExecStart=/usr/local/bin/cloudflared tunnel --config /home/ubuntu/.cloudflared/config.yml run method|' /etc/systemd/system/cloudflared.service

sudo systemctl daemon-reload
sudo systemctl restart cloudflared
sleep 3
systemctl status cloudflared --no-pager | head -15
```

状态应该是 `active (running)`，日志里有 `Registered tunnel connection`。

---

## 步骤 6：更新 `BASE_URL` + 重启 method

```bash
sed -i 's|^BASE_URL=.*|BASE_URL=https://method.xvc.com|' /home/ubuntu/method/.env
sudo systemctl restart method
sleep 2
curl -sfS https://method.xvc.com/api/health
```

预期：`{"ok":true,"version":"0.0.1"}`

---

## 验收

- [ ] 浏览器打开 `https://method.xvc.com` → 跳 `/login`，登录页正常渲染
- [ ] 邮箱 + 验证码登录，进工作台
- [ ] 提一个短问题，等几分钟，历史页能看到完整 markdown
- [ ] 审批邮件里的链接是 `https://method.xvc.com/admin/approve?token=...`（不再是 trycloudflare 的随机 URL）
- [ ] 重启整机（`sudo reboot`），起来后两个服务自动恢复，URL 不变

---

## 回滚

如果 named tunnel 出问题，秒切回 quick tunnel：

```bash
sudo sed -i 's|ExecStart=.*|ExecStart=/usr/local/bin/cloudflared tunnel --url http://127.0.0.1:8001|' /etc/systemd/system/cloudflared.service
sudo systemctl daemon-reload
sudo systemctl restart cloudflared
# 等 quick tunnel URL 出来
sleep 5
journalctl -u cloudflared --since "1 minute ago" | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1
# 把这个 URL 写回 .env
```

---

## 常见问题

**Q: `cloudflared tunnel login` 打开的 URL 要求登录 Cloudflare，但我还没注册？**
A: 去 https://dash.cloudflare.com 免费注册一个账号，再回来点授权链接。

**Q: `cloudflared tunnel route dns` 报 `zone not found`？**
A: `xvc.com` 还没在 Cloudflare 托管。回到「情况 B」把整个 `xvc.com` 的 nameservers 指到 Cloudflare。

**Q: 访问 `https://method.xvc.com` 报 502 / 530？**
A: 检查 `method.service` 是不是 active（`systemctl is-active method`）。它监听 `127.0.0.1:8001`，cloudflared 从那儿拿内容。

**Q: 审批邮件里的链接仍然是旧 trycloudflare URL？**
A: 说明 `.env` 里 `BASE_URL` 没改成功，或者 `method` 服务没重启。`grep BASE_URL /home/ubuntu/method/.env` 检查一下，然后 `sudo systemctl restart method`。

**Q: 不想自己操作，让 Claude 来做？**
A: 告诉我 "Claude 你来配"，我会要你先完成「步骤 1 `cloudflared tunnel login`」（必须你本人浏览器授权），之后的命令我可以接管。

---

## 事后可选项

- 加 Cloudflare WAF 规则（免费套餐有基础版），防机器人扫刷
- 开 Cloudflare Access (Zero Trust) 给 `method.xvc.com` 加一层 SSO 前置认证（目前应用内邮箱审批已足够）
- 监控：Cloudflare Analytics 看流量；`method` 服务本身的 `/api/health` 可以接 UptimeRobot
