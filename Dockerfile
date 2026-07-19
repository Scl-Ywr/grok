# Grok Register CPA — WebUI + Chromium registration worker
# 支持 linux/amd64 和 linux/arm64
# Build:  docker build -t grok-register-cpa .
# Build multi-platform:  docker buildx build --platform linux/amd64,linux/arm64 -t grok-register-cpa --push .
# Run:    docker compose up -d

FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    RUNNING_IN_DOCKER=1 \
    BROWSER_HEADLESS=1 \
    CHROME_PATH=/usr/bin/chromium \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# 编译依赖：curl_cffi / lxml 等需要编译原生扩展
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libcurl4-openssl-dev \
        libssl-dev \
        libffi-dev \
        libxml2-dev \
        libxslt-dev \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Chromium + 字体 + headless 运行时库
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        chromium-driver \
        fonts-liberation \
        fonts-noto-cjk \
        fonts-noto-color-emoji \
        ca-certificates \
        curl \
        dumb-init \
        libnss3 \
        libatk-bridge2.0-0 \
        libgtk-3-0 \
        libx11-xcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxrandr2 \
        libgbm1 \
        libasound2 \
        libpangocairo-1.0-0 \
        libpango-1.0-0 \
        libcups2 \
        libdrm2 \
        libxshmfence1 \
        libxkbcommon0 \
        libcurl4 \
        libssl3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps（利用 layer cache；arm64 编译 curl_cffi 可能较慢）
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip setuptools wheel \
    && pip install -r /app/requirements.txt \
    && pip install gunicorn \
    && rm -rf /root/.cache

# 清理编译依赖（减小镜像体积）
RUN apt-get purge -y --auto-remove \
        build-essential \
        libcurl4-openssl-dev \
        libssl-dev \
        libffi-dev \
        libxml2-dev \
        libxslt-dev \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# App code
COPY grok_register_ttk.py \
     sso_to_auth_json.py \
     upload_to_cpa.py \
     webui.py \
     account_health.py \
     cf_mail_debug.py \
     convert_accounts.sh \
     config.example.json \
     /app/
COPY webui_static /app/webui_static
COPY docker /app/docker

# Runtime dirs + default docker config
RUN mkdir -p /data /data/auth /data/accounts /app/turnstilePatch \
    && if [ -f /app/docker/config.docker.json ]; then \
         cp /app/docker/config.docker.json /app/config.docker.json; \
       else \
         cp /app/config.example.json /app/config.docker.json; \
       fi \
    && chmod +x /app/docker/entrypoint.sh

# Persist config + outputs under /data
ENV CONFIG_FILE=/data/config.json \
    CPA_AUTH_DIR=/data/auth \
    ACCOUNTS_DIR=/data/accounts \
    DATA_DIR=/data \
    WEBUI_HOST=0.0.0.0 \
    WEBUI_PORT=8787

EXPOSE 8787

VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=8s --start-period=40s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${WEBUI_PORT}/api/health" || exit 1

ENTRYPOINT ["/usr/bin/dumb-init", "--", "/app/docker/entrypoint.sh"]
CMD ["webui"]
