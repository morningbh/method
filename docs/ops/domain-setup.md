# Method 域名配置指南（`method.xvc.com`）

**目标**：把临时的 `https://<random>.trycloudflare.com` 换成永久的 `https://method.xvc.com`。

**当前条件**：
- 域名 `xvc.com`：**已 ICP 备案**；DNS 解析在**阿里云**
- 服务器：**腾讯云**（有公网 IP）；`method` systemd 服务监听 `127.0.0.1:8001`
- 浏览器跑的 Cloudflare Free 套餐**不允许加子域名 zone**（已确认）

---

## 最终方案：A 记录 + Nginx + Let's Encrypt

**为什么不用 Cloudflare**：
- 免费版不支持子域 zone，付费版 $200+/mo 不划算
- 把整个 `xvc.com` 主域迁到 Cloudflare 风险大（邮件解析、其它子域都要动）

**为什么这条路顺**：
- `xvc.com` 备案已做 → 国内走 80/443 端口合规
- 阿里云加一条 A 记录，5 分钟生效
- Let's Encrypt 免费证书，certbot 自动续期
- 腾讯云 + 阿里云都是你已有的体系，不增加第三方依赖

---

## 全流程（3 大步，~15 分钟）

1. **阿里云 DNS**：加一条 `method` 的 A 记录指向腾讯云公网 IP
2. **腾讯云安全组**：开放 80 / 443 端口
3. **服务器**：装 Nginx 反向代理 → certbot 申请 SSL → 关掉 cloudflared → 改 `.env` → 重启

---

## 前置：拿腾讯云公网 IP

两个办法二选一：

**A. 腾讯云控制台**：云服务器 → 实例 → 找到这台 → 复制"公网 IP"

**B. 服务器上命令**：
```bash
ssh ubuntu@<服务器IP>
curl -4 ifconfig.me
```
输出形如 `123.45.67.89` 的就是公网 IP。

**记下这个 IP**，下面反复用，我用 `<TENCENT_IP>` 代指。

---

## 步骤 1：阿里云 DNS 加 A 记录

阿里云控制台 → **云解析 DNS** → 进 `xvc.com` 解析页 → 添加记录：

| 主机记录 | 记录类型 | 解析线路 | 记录值 | TTL |
|---|---|---|---|---|
| `method` | **A** | 默认 | `<TENCENT_IP>` | 600 |

保存。

**验证**（本地或服务器上）：
```bash
dig A method.xvc.com +short
```
应返回 `<TENCENT_IP>`。没返回就等 5 分钟再试。

---

## 步骤 2：腾讯云安全组开 80 / 443

腾讯云控制台 → **云服务器** → 找到实例 → 点**安全组** → 编辑规则 → 入站规则 → 添加：

| 协议 | 端口 | 源 | 策略 | 备注 |
|---|---|---|---|---|
| TCP | 80 | 0.0.0.0/0 | 允许 | Let's Encrypt HTTP-01 |
| TCP | 443 | 0.0.0.0/0 | 允许 | HTTPS |

保存。

**验证**（本地）：
```bash
nc -zv <TENCENT_IP> 80
nc -zv <TENCENT_IP> 443
```
两条都 `succeeded` 即 OK（此时还没服务监听，连接通表示安全组放行到主机；后面装 nginx 会有服务接收）。

---

## 步骤 3：服务器侧配置（我可以全程接管）

### 3.1 装 Nginx

```bash
ssh ubuntu@<TENCENT_IP>
sudo apt update && sudo apt install -y nginx
sudo systemctl status nginx --no-pager | head -5
```

应该 `active (running)`。

### 3.2 写 Nginx 反向代理配置

```bash
sudo tee /etc/nginx/sites-available/method.xvc.com <<'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name method.xvc.com;

    # Let's Encrypt HTTP-01 挑战
    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    # 其它都 301 到 HTTPS（certbot 装完会自动加这段；先放占位）
    location / {
        return 301 https://$host$request_uri;
    }
}
EOF
sudo ln -sf /etc/nginx/sites-available/method.xvc.com /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

### 3.3 申请 SSL 证书

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d method.xvc.com --non-interactive --agree-tos -m morningwilliam@gmail.com --redirect
```

certbot 会自动：
- 拿到 Let's Encrypt 证书（有效期 90 天）
- 改 `/etc/nginx/sites-available/method.xvc.com` 加 `listen 443 ssl`
- 加 HTTP→HTTPS 301 跳转
- 装一个 cron / systemd timer 自动续期

验证：
```bash
curl -sfS -I https://method.xvc.com | head -3
```
应返回 `HTTP/2 200` 或 `HTTP/1.1 200` 加 `server: nginx/...`。

### 3.4 加反向代理（指向 Method 应用）

编辑配置（certbot 改过，现在再补反代）：

```bash
sudo tee /etc/nginx/sites-available/method.xvc.com <<'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name method.xvc.com;

    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name method.xvc.com;

    ssl_certificate     /etc/letsencrypt/live/method.xvc.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/method.xvc.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;

    # 上传最大 120 MB（对应 spec：20 个 × 30MB = 600MB 理论上限，100MB 总额；留 120 给余量）
    client_max_body_size 120M;

    # SSE 超时（claude 生成 3-10 分钟）
    proxy_read_timeout 700s;
    proxy_send_timeout 700s;
    proxy_buffering off;          # SSE 必须关
    proxy_cache off;
    proxy_http_version 1.1;
    proxy_set_header Connection "";

    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF
sudo nginx -t && sudo systemctl reload nginx
```

验证：
```bash
curl -sfS https://method.xvc.com/api/health
```
预期：`{"ok":true,"version":"0.0.1"}`

### 3.5 关掉 cloudflared（不再用快速隧道）

```bash
sudo systemctl stop cloudflared
sudo systemctl disable cloudflared
```

### 3.6 改 `.env` + 重启 method

```bash
sed -i 's|^BASE_URL=.*|BASE_URL=https://method.xvc.com|' /home/ubuntu/method/.env
sudo systemctl restart method
sleep 2
curl -sfS https://method.xvc.com/api/health
```

---

## 验收

- [ ] `dig A method.xvc.com` 返回 `<TENCENT_IP>`
- [ ] `https://method.xvc.com` 浏览器打开 → 跳 `/login`
- [ ] 证书有效（地址栏锁图标），颁发者 `Let's Encrypt`
- [ ] HTTP 自动 301 跳到 HTTPS（`curl -I http://method.xvc.com`）
- [ ] 邮箱 + 验证码登录，进工作台
- [ ] 提一个研究问题，历史页能看完整结果
- [ ] 审批邮件里链接是 `https://method.xvc.com/admin/approve?token=...`
- [ ] 重启整机 `sudo reboot`，起来后服务自动恢复
- [ ] 证书自动续期（`sudo certbot renew --dry-run` 应该 success）

---

## 回滚到 Cloudflare Quick Tunnel

```bash
sudo systemctl enable cloudflared
sudo systemctl start cloudflared
sleep 5
URL=$(journalctl -u cloudflared --since "1 minute ago" | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1)
sed -i "s|^BASE_URL=.*|BASE_URL=$URL|" /home/ubuntu/method/.env
sudo systemctl restart method
echo "URL: $URL"
```

Nginx 继续占用 80/443 也没问题——tunnel 不走这两个端口。

---

## 常见问题

**Q: `certbot --nginx` 报 `Timeout during connect (likely firewall problem)`？**
A: 腾讯云安全组没放 80 端口。回步骤 2 确认。certbot 用 HTTP-01 挑战，必须 80 能从外网访问。

**Q: 浏览器访问提示"不安全"或"证书无效"？**
A: 3 个可能：
1. 证书还没申请成功（重跑 3.3）
2. DNS 还没生效（等 10 分钟，`dig` 再验）
3. 浏览器缓存了旧的（无痕窗口再试）

**Q: SSE 10 分钟后断连，"生成中"卡住？**
A: 我已在步骤 3.4 把 `proxy_read_timeout` 设 `700s` 且 `proxy_buffering off`。如果还断，可能是腾讯云网络层的 idle 超时（罕见），联系腾讯云工单。

**Q: 上传大文件（接近 100MB）报 413？**
A: `client_max_body_size` 已设 120M，够用。如果仍报 413，先 `sudo nginx -t` 检查配置是否生效，再 `sudo systemctl reload nginx`。

**Q: Let's Encrypt 每 3 个月过期？**
A: certbot 装了自动续期 timer：`sudo systemctl list-timers | grep certbot`。测试续期流程：`sudo certbot renew --dry-run`。

**Q: 以后要加备用域名 / 别名怎么办？**
A: 阿里云加第二条 A 记录 → `sudo certbot --nginx -d method.xvc.com -d new.xvc.com --expand`，一条命令扩证书。

**Q: 国内访问慢？**
A: 腾讯云服务器在国内、证书是 Let's Encrypt、路径最短。没啥可优化。如果实测慢，先 `traceroute` 看是哪一跳。

**Q: Claude 你来做？**
A: 步骤 1（阿里云 DNS）+ 步骤 2（腾讯云安全组）必须你本人在控制台点。做完告诉我"前两步好了，`<TENCENT_IP>` 是 xxx"，我跑完步骤 3 全部。

---

## 事后可选

- 加监控：`/api/health` 接 UptimeRobot（免费，5 分钟一探）
- 加 Nginx 访问日志分析（`/var/log/nginx/access.log`）
- Nginx 前面套 Tencent EdgeOne 的免费版做 CDN（国内访问加速），后面再考虑
