#!/usr/bin/env bash
# 本地一键更新看板（取数 → 算指标 → 起预览）
#
# tdxrs 需要 Python 3.11。若本机默认 python3 版本不足，
# 会自动尝试常见的 3.11 路径；都找不到时降级为 HTTP 增量（会打印提示）。
set -euo pipefail
cd "$(dirname "$0")"

PY311="${PY311:-/Users/lennoxlv/.workbuddy/binaries/python/versions/3.11.9/bin/python3}"
if [ -x "$PY311" ]; then
  PY="$PY311"
elif python3 -c 'import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)' 2>/dev/null; then
  PY=python3
else
  echo "⚠ 未找到 Python 3.11，tdxrs 不可用，将走 HTTP 降级"
  echo "  如需权威数据：PY311=/path/to/python3.11 ./update.sh"
  PY=python3
fi
echo "使用解释器：$PY ($($PY -V 2>&1))"

echo "── 1/3 取数 ──"
"$PY" tools/fetch_prices.py "$@"

echo "── 2/3 算指标 ──"
python3 tools/build_dashboard.py

echo "── 3/3 预览 ──"
PORT="${PORT:-8901}"
echo "→ http://localhost:$PORT/"
python3 -m http.server "$PORT"
