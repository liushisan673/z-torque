#!/bin/sh
# 容器启动序列：建目录 → 迁移到 head → 幂等初始化管理员 → 起服务。
# 迁移放在启动期而不是构建期：交付到客户机器的数据库是挂载卷里的既存库，
# 构建期执行迁移只会作用于镜像内那份空库，等于没做。
set -eu

APP_PORT="${APP_PORT:-8000}"
ADMIN_USERNAME="${ADMIN_INITIAL_USERNAME:-admin}"

mkdir -p data/logs data/files data/secrets data/audit-archive

# 既存演示数据导入：/seed 是宿主机 data/ 的只读挂载，/app/data 是容器卷。
# SQLite 的 WAL 模式在 Docker Desktop 的 Windows 绑定挂载上会直接 disk I/O error，
# 因此数据库必须落在卷里；首次启动（卷内无库）时把宿主机准备好的库整份拷进来。
if [ -d /seed ] && [ -f /seed/z_torque.db ] && [ ! -f data/z_torque.db ]; then
    echo "[entrypoint] seeding /app/data from /seed"
    cp -a /seed/. data/ 2>/dev/null || true
    echo "[entrypoint] NOTE: 复用既存演示库，管理员口令沿用该库原有密码（ADMIN_INITIAL_PASSWORD 只对空库生效）"
fi

echo "[entrypoint] database: ${DATABASE_URL:-sqlite:///./data/z_torque.db}"
echo "[entrypoint] alembic upgrade head"
python -m alembic upgrade head

# init_admin 幂等：只补缺失的租户/角色/权限/用户，已存在的管理员密码不会被改写
echo "[entrypoint] init admin: ${ADMIN_USERNAME}"
python -m app.cli

echo "[entrypoint] serving console dist: $(ls console/dist/index.html 2>/dev/null || echo 'MISSING')"
echo "[entrypoint] uvicorn on 0.0.0.0:${APP_PORT}"
exec python -m uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${APP_PORT}" \
    --proxy-headers \
    --forwarded-allow-ips='*' \
    --no-use-colors
