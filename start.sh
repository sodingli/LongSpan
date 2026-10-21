#!/usr/bin/env bash
# Longspan 一键启动：后端(8000) + 前端(5173)
#
# 用法：  bash start.sh
# 打开：  http://localhost:5173
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PY=/Users/lidashuai5/.workbuddy/binaries/python/envs/default/bin/python
NODE_BIN=/Users/lidashuai5/.workbuddy/binaries/node/versions/22.22.2-6/bin

if [ ! -x "$PY" ]; then
  echo "找不到 Python 运行环境：$PY" >&2
  exit 1
fi

cleanup() {
  echo ""
  echo "正在停止服务…"
  kill "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "▸ 启动后端 http://127.0.0.1:8000"
(cd "$ROOT/backend" && exec "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port 8000) &
BACKEND_PID=$!

# 等后端就绪再拉前端，避免首屏就报"连不上后端"
for _ in $(seq 1 30); do
  if curl -s --noproxy '*' -m 1 http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
    echo "  后端就绪"
    break
  fi
  sleep 0.4
done

echo "▸ 启动前端 http://localhost:5173"
(cd "$ROOT/frontend" && PATH="$NODE_BIN:$PATH" exec npm run dev -- --host 127.0.0.1 --port 5173) &
FRONTEND_PID=$!

echo ""
echo "================================================"
echo "  打开浏览器访问： http://localhost:5173"
echo "  按 Ctrl+C 停止全部服务"
echo "================================================"
echo ""

wait
