# Docker 部署：Grok Register CPA

把 WebUI + 注册机 + Chromium 打成镜像，浏览器在容器内 **headless** 运行。

**支持架构：** `linux/amd64`（x86_64）和 `linux/arm64`（ARM64 / Apple Silicon）

---

## 前置

- Docker / Docker Desktop / OrbStack
- 本机当前若未安装 Docker，请先安装后再构建

---

## 快速开始

```bash
cd ~/grokRegister-cpa

# 构建并启动
docker compose up -d --build

# 打开 WebUI
open http://127.0.0.1:8787
```

数据目录（自动创建）：

```text
./data/config.json     # 配置（首次从模板生成）
./data/auth/           # 本地 CPA xai-*.json
./data/accounts/       # accounts_*.txt
```

---

## 服务器部署（ARM / AMD）

### 1. 本地构建后推送到服务器

```bash
# Mac (Apple Silicon) 直接构建 arm64 镜像
docker build -t grok-register-cpa .
docker save grok-register-cpa | gzip > grok-register-cpa-arm64.tar.gz
scp grok-register-cpa-arm64.tar.gz user@your-server:~/

# 服务器上加载
ssh user@your-server
docker load < grok-register-cpa-arm64.tar.gz
```

### 2. 服务器上直接构建

```bash
git clone <repo-url> && cd grokRegister-cpa
docker compose up -d --build
```

arm64 编译 `curl_cffi` 可能需要 5-10 分钟，属正常现象。

### 3. 跨平台构建（CI / 推送到 registry）

```bash
# 创建 buildx builder（首次）
docker buildx create --name multiarch --use

# 同时构建 amd64 + arm64 并推送
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t your-registry/grok-register-cpa:latest \
  --push .
```

### 4. docker-compose 指定架构（可选）

```yaml
services:
  grok-register:
    platform: linux/arm64   # 或 linux/amd64
```

---

## 配置

编辑 `./data/config.json`（容器首次启动后自动生成），至少填：

```json
{
  "email_provider": "yyds",
  "yyds_api_key": "你的KEY",
  "proxy": "http://host.docker.internal:7892",
  "cpa_auto_add": true,
  "cpa_auth_dir": "/data/auth",
  "cpa_remote_url": "https://cpa.imissyou.de5.net",
  "cpa_management_key": "你的管理密钥",
  "enable_nsfw": true,
  "register_count": 1
}
```

### 代理

容器内 `127.0.0.1` 指的是容器自己，不是宿主机。

| 环境 | proxy 值 |
|------|----------|
| Mac / Windows | `http://host.docker.internal:7892` |
| Linux | `http://宿主机内网IP:7892` |

在 `docker-compose.yml` 里设置环境变量（可选，config.json 里的 proxy 优先）：

```yaml
environment:
  HTTP_PROXY: "http://host.docker.internal:7892"
  HTTPS_PROXY: "http://host.docker.internal:7892"
```

Linux 还需在 compose 里加：

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

### 远程 CPA

- `cpa_remote_url` 必须带 `https://`
- `cpa_auth_dir` 固定 `/data/auth`

---

## 常用命令

```bash
# 查看日志
docker compose logs -f

# 重建镜像（代码更新后）
docker compose build --no-cache
docker compose up -d

# 进容器
docker compose exec grok-register bash

# 容器内跑上传
docker compose exec grok-register upload

# 停止（保留 ./data）
docker compose down
```

---

## 架构说明

```text
┌─────────────────────────────────────────┐
│  container: grok-register-cpa           │
│  ┌─────────────┐   ┌─────────────────┐  │
│  │ Flask WebUI │──▶│ DrissionPage    │  │
│  │ :8787       │   │ + Chromium head │  │
│  └─────────────┘   └────────┬────────┘  │
│                             │ proxy?    │
│  /data/config.json  /data/auth/*.json   │
└─────────────────────────────────────────┘
          │
          ▼
   远程 CPA / 临时邮箱 API
```

---

## 常见问题

### WebUI 打不开

```bash
docker compose ps
docker compose logs --tail=100
curl -s http://127.0.0.1:8787/api/health
```

### 浏览器启动失败

```bash
docker compose exec grok-register chromium --version
```

确认 `shm_size: "1gb"` 已设置（Chromium 需要足够的共享内存）。

### 代理不通

把 `proxy` 从 `http://127.0.0.1:7892` 改成 `http://host.docker.internal:7892`。

### arm64 构建慢

`curl_cffi` 需要从源码编译 libcurl 绑定，ARM 上约 5-10 分钟。构建完成后 layer cache 会加速后续构建。

### Turnstile 过盾率低

headless 模式下 Cloudflare Turnstile 通过率低于有界面模式，属正常现象。可在有桌面的服务器上用 `BROWSER_HEADLESS=0` + Xvfb 运行。
