# Z-Torque 社区版演示/交付镜像：
#   阶段1 构建控制台（vite build）→ 阶段2 运行后端，由后端在 / 上同源托管 console/dist。
#   单端口（8000）即完整控制台 + API，前端不再需要 vite 进程，也不需要 CORS 配置。
# 基础镜像不写 `# syntax=docker/dockerfile:1`：本镜像不使用 buildx 专有语法，
# 省掉一次 docker.io 前端镜像拉取（内网/受限网络下这一步最容易失败）。

FROM node:20-slim AS console-builder
WORKDIR /build/console
# 先只拷依赖清单，利用层缓存：源码改动不触发 npm ci 重装
COPY console/package.json console/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY console/tsconfig.json console/vite.config.ts console/index.html ./
COPY console/src ./src
COPY console/public ./public
# npm run build = tsc --noEmit && vite build（类型错误会直接失败，不带病出镜像）
RUN npm run build


FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000 \
    SERVE_CONSOLE=true
WORKDIR /app

# curl 供 HEALTHCHECK 使用；tzdata 保证容器内时间与宿主一致（审计时间戳可读性）
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/*

# 依赖层：pyproject 未变则复用缓存
COPY pyproject.toml alembic.ini ./
COPY alembic ./alembic
COPY app ./app
RUN pip install --no-cache-dir .

# 运行时内容：MD 技能包、插件市场离线包、控制台构建产物
COPY md-skills ./md-skills
COPY plugins_market ./plugins_market
COPY --from=console-builder /build/console/dist ./console/dist

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
    && mkdir -p data/logs data/files data/secrets data/audit-archive

EXPOSE 8000
HEALTHCHECK --interval=20s --timeout=5s --start-period=40s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
