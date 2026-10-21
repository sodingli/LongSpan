#!/usr/bin/env bash
# Longspan 一键启动：后端(8000) + 前端(5173)
#
# 用法：  bash start.sh
# 打开：  http://localhost:5173
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"

# 本机专属配置（不进仓库，被 .gitignore 忽略）：可在此固定解释器/node 路径
#   LONGSPAN_PYTHON=/path/to/python
#   LONGSPAN_NODE_BIN=/path/to/node/bin
# 注意：命令行/环境里显式给的值优先，不被这个文件覆盖。
_ENV_PY="${LONGSPAN_PYTHON:-}"
_ENV_NODE="${LONGSPAN_NODE_BIN:-}"
if [ -f "$ROOT/.longspan.local" ]; then
  # shellcheck disable=SC1091
  . "$ROOT/.longspan.local"
fi
[ -n "$_ENV_PY" ] && LONGSPAN_PYTHON="$_ENV_PY"
[ -n "$_ENV_NODE" ] && LONGSPAN_NODE_BIN="$_ENV_NODE"

# 一个可用的解释器 = 版本够 + 装了依赖（uvicorn）。只看版本会选到裸解释器。
python_usable() {
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null \
    && "$1" -c 'import uvicorn' 2>/dev/null
}

# 找 Python：显式指定 → 仓库内虚拟环境 → PATH 里的 python3/python（按可用性挑）
find_python() {
  local cands=()
  [ -n "${LONGSPAN_PYTHON:-}" ] && cands+=("$LONGSPAN_PYTHON")
  cands+=("$ROOT/backend/.venv/bin/python" "$ROOT/.venv/bin/python")
  local p3 p
  p3="$(command -v python3 2>/dev/null || true)"; [ -n "$p3" ] && cands+=("$p3")
  p="$(command -v python 2>/dev/null || true)"; [ -n "$p" ] && cands+=("$p")

  local c
  for c in "${cands[@]}"; do
    [ -x "$c" ] && python_usable "$c" && { echo "$c"; return 0; }
  done
  # 没有可用的：回退到第一个存在的，交给调用处给出针对性报错
  for c in "${cands[@]}"; do
    [ -x "$c" ] && { echo "$c"; return 0; }
  done
  return 1
}

# 找 node/npm：优先 $LONGSPAN_NODE_BIN（指定版本的 bin 目录），否则用 PATH
if [ -n "${LONGSPAN_NODE_BIN:-}" ]; then
  export PATH="$LONGSPAN_NODE_BIN:$PATH"
fi

PY="$(find_python || true)"
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  echo "找不到 Python（需要 3.10+）。请安装，或用 LONGSPAN_PYTHON 指定解释器路径。" >&2
  exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
  echo "Python 版本过低（需 3.10+）：$PY ($("$PY" -V 2>&1))" >&2
  exit 1
fi
if ! "$PY" -c 'import uvicorn' 2>/dev/null; then
  echo "解释器缺少依赖：$PY" >&2
  echo "  先装依赖：  $PY -m pip install -r backend/requirements.txt" >&2
  echo "  或指定已装好的解释器：写进 .longspan.local（该文件不入库）" >&2
  exit 1
fi
if ! command -v npm >/dev/null 2>&1; then
  echo "找不到 npm（需要 Node 18+）。请安装，或用 LONGSPAN_NODE_BIN 指定 node 的 bin 目录。" >&2
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
(cd "$ROOT/frontend" && exec npm run dev -- --host 127.0.0.1 --port 5173) &
FRONTEND_PID=$!

echo ""
echo "================================================"
echo "  打开浏览器访问： http://localhost:5173"
echo "  按 Ctrl+C 停止全部服务"
echo "================================================"
echo ""

wait
