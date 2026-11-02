#!/usr/bin/env bash
# Longspan 标准安装脚本：后端 Python 依赖 + 前端依赖 + Remotion 渲染依赖。
#
# 用法：
#   bash install.sh             # 安装全部依赖（推荐，部署环境执行这一条）
#   bash install.sh backend     # 只安装后端依赖
#   bash install.sh frontend    # 只安装前端依赖
#   bash install.sh remotion    # 只安装 Remotion 渲染依赖
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
TARGET="${1:-all}"

# 与 start.sh 保持一致：命令行/环境里显式给的值优先，不被本机配置覆盖。
_ENV_PY="${LONGSPAN_PYTHON:-}"
_ENV_NODE="${LONGSPAN_NODE_BIN:-}"
if [ -f "$ROOT/.longspan.local" ]; then
  # shellcheck disable=SC1091
  . "$ROOT/.longspan.local"
fi
[ -n "$_ENV_PY" ] && LONGSPAN_PYTHON="$_ENV_PY"
[ -n "$_ENV_NODE" ] && LONGSPAN_NODE_BIN="$_ENV_NODE"

log() { printf '▸ %s\n' "$*"; }
die() { printf '✗ %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
用法: bash install.sh [all|backend|frontend|remotion]

  all        安装全部依赖（默认，部署环境使用）
  backend    只安装后端 Python 依赖
  frontend   只安装前端 npm 依赖
  remotion   只安装 Remotion 渲染依赖
EOF
}

case "$TARGET" in
  all|backend|frontend|remotion) ;;
  -h|--help|help) usage; exit 0 ;;
  *) usage; die "未知安装目标：$TARGET" ;;
esac

want() { [[ "$TARGET" == "all" || "$TARGET" == "$1" ]]; }

prepare_node() {
  if [ -n "${LONGSPAN_NODE_BIN:-}" ]; then
    export PATH="$LONGSPAN_NODE_BIN:$PATH"
  fi

  command -v node >/dev/null 2>&1 || die "找不到 node（需要 Node 18+）。可安装 Node，或设置 LONGSPAN_NODE_BIN。"
  command -v npm >/dev/null 2>&1 || die "找不到 npm（需要 Node 18+）。可安装 Node，或设置 LONGSPAN_NODE_BIN。"

  local major
  major="$(node -p 'Number(process.versions.node.split(".")[0])')"
  if [ "$major" -lt 18 ]; then
    die "Node 版本过低（需 18+）：$(node -v)"
  fi
}

npm_install_locked() {
  local dir="$1"
  (
    cd "$dir"
    if [ -f package-lock.json ]; then
      npm ci --no-audit --no-fund
    else
      npm install --no-audit --no-fund
    fi
  )
}

install_backend() {
  local python_bin

  # 部署环境默认使用仓库内虚拟环境；显式指定解释器时尊重用户选择。
  if [ -n "${LONGSPAN_PYTHON:-}" ]; then
    python_bin="$LONGSPAN_PYTHON"
  else
    command -v python3 >/dev/null 2>&1 || die "找不到 python3（需要 Python 3.10+）。"
    if [ ! -x "$ROOT/backend/.venv/bin/python" ]; then
      log "创建后端虚拟环境 backend/.venv"
      python3 -m venv "$ROOT/backend/.venv"
    fi
    python_bin="$ROOT/backend/.venv/bin/python"
  fi

  [ -x "$python_bin" ] || die "Python 解释器不可执行：$python_bin"
  if ! "$python_bin" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    die "Python 版本过低（需 3.10+）：$("$python_bin" -V 2>&1)"
  fi

  log "安装后端 Python 依赖"
  "$python_bin" -m pip install -r "$ROOT/backend/requirements.txt"
}

install_frontend() {
  prepare_node
  log "安装前端依赖"
  npm_install_locked "$ROOT/frontend"
}

install_remotion() {
  prepare_node
  log "安装 Remotion 渲染依赖"
  npm_install_locked "$ROOT/backend/media/remotion"

  local remotion_bin="$ROOT/backend/media/remotion/node_modules/.bin/remotion"
  [ -x "$remotion_bin" ] || die "Remotion 安装完成但找不到本地 CLI：$remotion_bin"
  # 必须在 Remotion 工程目录内校验；否则 CLI 会向上找到仓库根 package.json，
  # 误用根目录的 zod 版本并报告不存在的版本冲突。
  (
    cd "$ROOT/backend/media/remotion"
    ./node_modules/.bin/remotion versions >/dev/null
  )
  local version
  version="$(
    node -p "require(process.argv[1]).version" \
      "$ROOT/backend/media/remotion/node_modules/@remotion/cli/package.json"
  )"
  log "Remotion CLI 就绪：$version"
}

if want backend; then install_backend; fi
if want frontend; then install_frontend; fi
if want remotion; then install_remotion; fi

log "安装完成：$TARGET"
