# QQQ-RP 多资产策略监控看板

每日自动更新的多资产风险平价策略监控看板，托管在 GitHub Pages，
数据由 GitHub Actions 每天北京时间 16:10 自动拉取更新。

**在线地址**：`https://<你的用户名>.github.io/<仓库名>/`
（首次部署后在 Settings → Pages 可看到实际 URL）

---

## 功能

| 模块 | 说明 |
|---|---|
| 本周调仓动作 | 每只标的的买入/加仓/减仓/清仓、目标权重、按本金换算的金额 |
| 净值曲线与回撤 | 策略 vs 沪深300ETF，双图时间轴联动 |
| **区间指标条** | 跟随区间选择联动，给出该区间的收益/年化/夏普/回撤/波动/Calmar/胜率/超额 |
| 滚动区间表 | 近 1/3/6/12 月、今年以来、全样本 |
| 逐年收益 | 按日历年 |
| 最长水下期 | 从历史新高到再创新高，附「为什么没有亏损年份却有长水下期」的解释 |
| 历史调仓记录 | 每周选中只数、事前波动、缩放系数、风险仓位、换手 |

## 自动更新

工作流 `.github/workflows/dashboard.yml`：

1. **取数** — tdxrs（通达信直连）权威源，**先监控除权事件再决定增量/全量**
2. **计算** — 策略信号、净值曲线、各区间指标
3. **自检** — 曲线点数、末日净值、数据新鲜度、除权指纹完整性、各标的末日一致性
4. **提交** — 数据变更自动 commit（带 `[skip ci]` 防触发循环）
5. **发布** — 打包并部署到 GitHub Pages

自检任一项不通过就**终止发布**，页面保持上次的正确数据，绝不发布脏数据。

### 定时说明

```yaml
- cron: '10 8 * * *'     # 北京 16:10（主运行）
- cron: '10 10 * * *'    # 北京 18:10（补跑安全网）
```

> ⚠ GitHub Actions 的 cron **按 UTC 解释**，北京时间需减 8 小时。
> 另外官方明确说明定时任务在高峰期**可能延迟数分钟到数十分钟甚至被丢弃**，
> 所以保留了一次补跑。当天若已更新成功，补跑会走「无新交易日」短路，
> 零请求、不提交、不产生多余 commit。

## 首次部署

### 方式 A：一键脚本（推荐）

```bash
./deploy.sh                 # 默认仓库名 qqq-dashboard
./deploy.sh 我的仓库名       # 自定义
```

脚本会依次完成：登录 GitHub → 创建仓库并推送 → 把 Pages 源设为
"GitHub Actions" → 手动触发一次工作流并跟踪结果 → 打印访问地址。

### 方式 B：手动

```bash
# 1. 登录（会打开浏览器授权）
gh auth login --web --scopes 'repo,workflow'

# 2. 创建仓库并推送
gh repo create qqq-dashboard --public --source=. --remote=origin --push

# 3. 开启 Pages（也可在 Settings → Pages → Source 选 "GitHub Actions"）
gh api -X POST repos/<用户名>/qqq-dashboard/pages -f build_type=workflow

# 4. 手动跑一次验证
gh workflow run dashboard.yml
gh run watch
```

> **Pages 与仓库可见性**：GitHub 免费账户的 Pages **只能用于公开仓库**。
> 若要仓库私有同时开 Pages，需要 GitHub Pro。
> 本仓库已用白名单 `.gitignore` 限定为**只发布 `dashboard/`**，
> 研究脚本、中间数据、策略报告、`.codebuddy` 记忆文件都不会被提交
> （实测追踪 15 个文件，已逐项核对）。

## 本地开发

```bash
cd dashboard

# 取数（自动判断增量/全量；需 Python 3.11 + tdxrs）
python3 tools/fetch_prices.py

# 计算指标与信号
python3 tools/build_dashboard.py

# 预览（必须走 HTTP，直接双击 HTML 会因 CORS 读不到 JSON）
python3 -m http.server 8901
```

详细说明见 [`dashboard/README.md`](dashboard/README.md)。

## 目录结构

```
.
├── .github/workflows/dashboard.yml   # 每日更新 + Pages 部署
├── .gitignore                        # 白名单：只发布 dashboard/
└── dashboard/                        # ← GitHub Pages 站点根目录
    ├── index.html                    # 看板前端（零构建，纯静态）
    ├── data/
    │   ├── prices.json               # tdxrs 权威价格基线（增量拼接的基线）
    │   └── dashboard.json            # 前端直接消费的指标数据
    ├── tools/
    │   ├── fetch_prices.py           # 取数：除权监控 + 增量/全量切换
    │   ├── engine.py                 # 策略引擎与指标（纯标准库）
    │   └── build_dashboard.py        # 产出 dashboard.json
    └── vendor/                       # lightweight-charts（本地化，无 CDN 依赖）
```

## ⚠ 风险提示

回测数据基于历史，**不构成投资建议**。

该策略的资产池包含**事后选择偏差**（约 0.5 夏普）——池子是在已知历史表现的情况下
挑出来的。全样本夏普 1.41 不代表未来可复现。

**实盘合理预期：夏普 0.7~1.0、年化 7~10%、最大回撤准备 -15%~-18%。**
