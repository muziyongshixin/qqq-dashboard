#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════
# GitHub Pages 一键部署
# ══════════════════════════════════════════════════════════════════
# 用法：
#   ./deploy.sh                    # 用默认仓库名 qqq-dashboard
#   ./deploy.sh 我的仓库名          # 自定义仓库名
#
# 本脚本做的事：
#   1. 检查 gh 登录状态（未登录则引导你登录）
#   2. 创建 GitHub 仓库并推送（仓库已存在则直接推送）
#   3. 把 Pages 的构建源切换为 "GitHub Actions"
#   4. 立即手动触发一次工作流并等待结果
#   5. 打印最终访问地址
#
# 前置条件：只需要 gh CLI（已确认已安装）。不需要 SSH key。
set -euo pipefail

REPO="${1:-qqq-dashboard}"
cd "$(dirname "$0")"

C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_DIM=$'\033[2m'; C_N=$'\033[0m'
step() { echo; echo "${C_DIM}────────────────────────────────────────${C_N}"; echo "▸ $1"; }
ok()   { echo "${C_OK}✓${C_N} $1"; }
warn() { echo "${C_WARN}⚠${C_N} $1"; }
die()  { echo "${C_ERR}✗${C_N} $1" >&2; exit 1; }

command -v gh >/dev/null || die "未找到 gh CLI，请先 brew install gh"
command -v git >/dev/null || die "未找到 git"

# ── 1. 登录 ──────────────────────────────────────────────────────
step "检查 GitHub 登录状态"
if ! gh auth status >/dev/null 2>&1; then
  warn "尚未登录 GitHub，现在开始登录流程"
  echo "${C_DIM}  会打开浏览器让你授权；若在纯终端环境，选 'Paste an authentication token'${C_N}"
  echo
  gh auth login --hostname github.com --git-protocol https --web \
    --scopes 'repo,workflow' || die "登录失败"
fi
USER_LOGIN="$(gh api user --jq .login)"
ok "已登录为 ${USER_LOGIN}"

# git 身份（未配置会导致 commit 失败）
if ! git config user.name >/dev/null 2>&1 && ! git config --global user.name >/dev/null 2>&1; then
  git config user.name  "${USER_LOGIN}"
  git config user.email "${USER_LOGIN}@users.noreply.github.com"
  ok "已设置本仓库的 git 身份为 ${USER_LOGIN}"
fi

# ── 2. 创建仓库并推送 ────────────────────────────────────────────
step "创建 / 推送仓库 ${USER_LOGIN}/${REPO}"
echo "${C_DIM}  注意：GitHub 免费账户的 Pages 只支持公开仓库${C_N}"
echo "${C_DIM}  本仓库用白名单 .gitignore 限定为只发布 dashboard/，研究代码不会上传${C_N}"

if gh repo view "${USER_LOGIN}/${REPO}" >/dev/null 2>&1; then
  ok "仓库已存在，直接推送"
  git remote get-url origin >/dev/null 2>&1 \
    || git remote add origin "https://github.com/${USER_LOGIN}/${REPO}.git"
  git push -u origin main
else
  gh repo create "${REPO}" --public --source=. --remote=origin --push \
    --description "QQQ-RP 多资产策略监控看板 · 每日自动更新" \
    || die "创建仓库失败"
fi
ok "代码已推送到 main 分支"

# ── 3. 开启 Pages（源 = GitHub Actions）─────────────────────────
step "配置 GitHub Pages（构建源 = GitHub Actions）"
# 已存在时 POST 会 409，改用 PUT 更新；两者任一成功即可
if gh api "repos/${USER_LOGIN}/${REPO}/pages" >/dev/null 2>&1; then
  gh api -X PUT "repos/${USER_LOGIN}/${REPO}/pages" \
    -f "build_type=workflow" >/dev/null && ok "Pages 已更新为 GitHub Actions 源"
else
  gh api -X POST "repos/${USER_LOGIN}/${REPO}/pages" \
    -f "build_type=workflow" >/dev/null 2>&1 \
    && ok "Pages 已开启（源 = GitHub Actions）" \
    || warn "自动开启失败，请手动到 Settings → Pages → Source 选 'GitHub Actions'"
fi

# ── 4. 触发工作流 ────────────────────────────────────────────────
step "手动触发一次工作流验证"
gh workflow run dashboard.yml --repo "${USER_LOGIN}/${REPO}" || die "触发失败"
echo "${C_DIM}  等待 8 秒让任务进入队列…${C_N}"
sleep 8
RUN_ID="$(gh run list --repo "${USER_LOGIN}/${REPO}" \
  --workflow dashboard.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
if [ -n "${RUN_ID}" ]; then
  echo "${C_DIM}  运行 ID ${RUN_ID}，实时跟踪中（Ctrl-C 可退出，任务仍会继续）${C_N}"
  gh run watch "${RUN_ID}" --repo "${USER_LOGIN}/${REPO}" --exit-status \
    && ok "工作流执行成功" \
    || warn "工作流未成功，查看日志：gh run view ${RUN_ID} --repo ${USER_LOGIN}/${REPO} --log-failed"
fi

# ── 5. 输出地址 ──────────────────────────────────────────────────
step "完成"
PAGE_URL="$(gh api "repos/${USER_LOGIN}/${REPO}/pages" --jq .html_url 2>/dev/null || true)"
echo
echo "  看板地址：${C_OK}${PAGE_URL:-https://${USER_LOGIN}.github.io/${REPO}/}${C_N}"
echo "  仓库地址：https://github.com/${USER_LOGIN}/${REPO}"
echo "  Actions ：https://github.com/${USER_LOGIN}/${REPO}/actions"
echo
echo "  ${C_DIM}定时：每天北京 16:10 主运行、18:10 补跑（cron 按 UTC，已换算）${C_N}"
echo "  ${C_DIM}首次 Pages 生效可能需要 1~2 分钟${C_N}"
echo
