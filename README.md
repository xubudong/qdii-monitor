# QDII ETF 溢价与公告监控台

本机运行的公开数据监控台，按同一指数分组展示 QDII/跨境 ETF 的场内价、IOPV 与实时溢价率，并可并列展示境外期货参考行情，同时留存交易所申购公告、外管局 QDII 额度公告和配置池内持仓浮盈亏。

本工具只做信息监控，不生成交易指令，不判断套利是否可执行，也不连接券商。

## 安装与启动

Windows PowerShell:

```powershell
cd D:\codex_project\qdii-monitor
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\run_web.ps1
```

默认访问地址为 `http://127.0.0.1:8010`。可使用环境变量覆盖端口：

```powershell
$env:WEB_PORT = "8020"
.\run_web.ps1
```

双击或执行 `stop_web.ps1` 可按 `data/qdii-monitor.pid` 精确停止本项目服务，
不会批量结束其他 Python 进程。

Linux / macOS:

```bash
cd /path/to/qdii-monitor
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
chmod +x run_web.sh stop_web.sh
./run_web.sh
```

默认同样访问 `http://127.0.0.1:8010`。可用环境变量覆盖监听地址和端口：

```bash
WEB_HOST=0.0.0.0 WEB_PORT=8010 ./run_web.sh
```

执行 `./stop_web.sh` 会按 `data/qdii-monitor.pid` 和 `uvicorn qdii_monitor.app:app`
命令行校验后停止服务，避免误杀其他 Python 进程。

若下载期间出现 `ReadTimeoutError` 或代理握手超时，可增加 pip 的超时和重试次数后重新安装：

```powershell
.\.venv\Scripts\python.exe -m pip install --timeout 120 --retries 10 -r requirements.txt
```

东方财富历史 K 线源在部分网络环境中可能无法直接连接。历史日线功能已支持与既有项目相同的 `akshare_proxy_patch`。推荐复制 `.env.example` 为 `.env`，再填写本机代理参数：

```dotenv
QDII_AKSHARE_PROXY_HOST=你的代理地址
QDII_AKSHARE_PROXY_TOKEN=你的访问令牌
QDII_AKSHARE_PROXY_RETRY=30
QDII_AKSHARE_PROXY_HOOK_DOMAINS=push2his.eastmoney.com
```

`QDII_AKSHARE_PROXY_HOOK_DOMAINS` 在 `.env` 中按英文逗号分隔，例如 `push2his.eastmoney.com,another.example.com`；程序会转成列表传递给 `akshare_proxy_patch`。`.env` 已加入忽略规则，不会作为项目文件提交；PowerShell 中显式设置的同名环境变量优先于 `.env`。配置完成后重新运行 `.\run_web.ps1`。这些参数只影响历史日线价格采集，不改变盘中 IOPV、公告或额度来源。

历史日线以“上一工作日”的已完成数据为更新目标，避免为了尚未稳定的当天净值重复消耗代理请求。首次会为缺失历史的标的回填；后续刷新读取 SQLite 中每只 ETF 的最新日期，仅对尚未覆盖目标日期的标的发起历史价格与 NAV 请求。全部标的已更新时，页面会提示未发起代理历史请求。

## 标的配置

编辑 `config/funds.yaml` 中的 `groups`。每个分组代表可比较的同一长期敞口，系统只在组内排序并标记当前最低有效溢价，不会跨组比较。

每只基金的必填字段为：

```yaml
- code: "513100"
  exchange: SSE       # SSE 或 SZSE
  display_name: 纳指ETF
  fund_name: 基金完整名称
  manager: 基金管理人 # 可选
  official_url: 基金公司官网详情页 # 可选
```

没有盘中 IOPV 的 LOF 可配置参考行情加权估值。以下配置表示以最新官方 NAV 为基准，
按纳指期货自该 NAV 估值日收盘以来的累计变化乘以 80% 估算盘中 NAV：

```yaml
nav_estimate:
  reference_code: NQ00Y
  reference_weight: 0.80
  description: 其他未建模资产暂按不变处理
```

估算公式为 `估算NAV = 最新官方NAV × [1 + 参考权重 × 参考行情累计变动]`，
折溢价统一按 `场内最新价 / 估算NAV - 1` 计算。页面会明确标注“估算 NAV”，
该结果不等同于基金公司或交易所发布的 IOPV。

交付配置中包含纳斯达克 100、日经 225 和标普 500 的场内 ETF 监控池。需要在同一板块旁观但不应参加最低溢价比较的产品，可以加入同组并配置 `comparison_eligible: false`；默认纳指页面中的 `159509` 纳指科技 ETF 即按这种方式展示。

每个分组还可配置一个只作观察、不参与组内溢价排序的参考品种。例如纳指组已配置东方财富国际期货行情中的小型纳指当月连续：

```yaml
reference:
  code: NQ00Y
  display_name: 小型纳指当月连续
  source: eastmoney_global_futures
```

示例配置中，纳指与标普使用东方财富国际期货的 `NQ00Y`、`ES00Y`；该列表未提供日经 225 期货，因此日经组使用东方财富全球指数延迟行情的 `N225`。页面会明确显示它是指数参考，不将其描述为期货。

## 数据与刷新

- 行情：通过 AKShare `fund_etf_spot_em()` / `fund_lof_spot_em()` 获取场内最新价与 `IOPV实时估值`，溢价率由系统按 `(最新价 / IOPV - 1)` 重算。批量列表未返回的已配置场内基金，会按证券代码调用东方财富单品种行情作为价格兜底；该兜底不提供 IOPV。
- LOF 估值：`501312`、`161130` 没有可直接使用的盘中 IOPV，系统使用最新官方 NAV、NAV 日期对应的美股收盘时刻纳指期货锚点和当前期货价格，按配置权重估算盘中 NAV，再按 `(最新价 / 估算NAV - 1)` 计算折溢价。当前 `501312` 使用 80% 纳指权重，`161130` 根据官网业绩比较基准使用 95% 纳指权重。夏令时优先取 NAV 日期后一自然日北京时间 `04:00` 附近的 `NQ00Y` 价格；若本地库缺少该锚点，才退回旧的昨结基准并在说明中标注。界面会直接展示类似 `2.2470 × [1 + 80% × (699.94 / 693.70 - 1)] = 2.2632` 的估算公式，现金、债券、汇率及跟踪误差当前未单独建模，因此该值只作参考。
- 参考行情：示例配置统一使用东方财富公开行情；纳指期货 `NQ00Y`、标普期货 `ES00Y` 和日经指数 `N225`。当日曲线通过东方财富 `trends2/get` 分时接口保存该品种当日已返回的分钟线，并直接显示价格、昨结/昨收和较昨结/昨收幅度，不再按区间首点归一化；参考数据不改变溢价排序或最低标记。经接口验证，`NQ00Y`、`ES00Y` 当日序列从北京时间 `06:00` 起，`N225` 从 `08:00` 起。
- 历史日线溢价：通过 AKShare `fund_etf_hist_em()` 读取场内收盘价、`fund_open_fund_info_em(..., indicator="单位净值走势")` 读取官方单位净值，按 `(收盘价 / NAV - 1)` 计算。配置了基金公司官网链接的 `501312`、`161130` 会额外解析官网详情页的最新净值作为补充，避免第三方聚合源更新滞后。该口径仅用于日期历史观察，不代替盘中 IOPV 溢价。
- 天天基金状态/日限额：通过 AKShare `fund_purchase_em()` 读取东方财富/天天基金聚合展示的数据，作为便于观察的辅助字段；经配置池实测，多数场内 ETF 返回“场内交易”且金额为 `0`，该结果不表示暂停或开放申购，页面将隐藏这类无有效结论的单元格内容。
- 最新正式公告：按配置标的查询上交所/深交所基金公告，识别恢复、暂停和限制申购事件并保留原文链接。页面将正式公告与辅助状态并列展示，两者方向不一致或缺少正式事件时提示核验原文。
- QDII 额度：读取国家外汇管理局配置页面，记录页面中出现的新审批表链接及版本变化；该信息不会被解释为基金恢复申购。
- 自动刷新：工作日 `09:30-11:30`、`13:00-15:00` 每分钟刷新盘中行情；北京时间周二到周六 `04:05` 额外采集一次参考行情，用于保存美股收盘锚点；正式公告、辅助申购状态和额度在服务启动时以及每日 `18:30` 检查；历史日线在工作日 `18:35` 更新，也可在历史页手动刷新。
- 数据保存：运行数据保存在 `data/qdii_monitor.sqlite`，可删除数据库以重建空白监控记录。
- 估算快照：对配置了 `nav_estimate` 的 LOF，行情快照会同时保存估算 NAV、官方 NAV 日期/数值、参考代码、参考价、基准价、基准类型、权重、参考涨跌、加权涨跌、公式和说明，便于后续逐日和基金公司公布净值对账。
- 快照筛选：页面快照下拉支持“每天收盘”“每天开盘”“美股收盘”“全部实时”。“美股收盘”按每个自然日选择最接近北京时间 `04:00` 的行情快照，主要用于 QDII 官方 NAV 对账。
- 历史变化：页面使用本地 Plotly 交互图，分为“当日盘中”和“历史日线”两种模式。当日盘中展示本应用在当日保存的实时价格/IOPV 快照以及参考行情原始价格；历史日线默认采集最近 3 年自然日的收盘价/NAV 溢价，并展示最近约 760 个交易日。图中可悬停查看原始值、拖动平移或框选缩放。

交易所或监管站点可能调整网页/API 格式；采集失败时，页面会保留上次成功数据并在采集状态区提示错误。

## 持仓导入

页面支持上传券商导出的 `table.xls` 文本表格格式，解析规则与既有 ETF 监控应用一致。仅配置文件中已列出的代码会被保留。页面按上传的份额与平均成本，以及最新场内价，计算当前市值和未实现盈亏；不记录成交历史或已实现收益。

## API

- `GET /api/dashboard`：所有面板汇总数据。
- `GET /api/groups`：分组与最新溢价排序。
- `GET /api/history?limit=240`：指定最近快照数量的溢价率及盘中参考历史数据。
- `POST /api/refresh/quotes`：刷新行情。
- `POST /api/refresh/notices`：刷新申购公告。
- `POST /api/refresh/history?days=1095`：刷新按收盘价/NAV 计算的历史日线溢价。
- `POST /api/refresh/quota`：刷新 QDII 额度页面。
- `POST /api/holdings/upload`：以 multipart 文件字段 `file` 上传持仓。
- `GET /api/health`：配置、数据库及任务状态。

## 溢价轮动策略回测

项目内置一个命令行模型，用本地 `daily_premium_history` 的收盘价和 NAV 溢价回测同指数 ETF 换仓规则。默认池为纳指 100 的 `513100,513110,513390,159660,159632,159659`，基准为买入并持有 `513100`，初始资金 50000 元。

```powershell
.\.venv\Scripts\python.exe -m qdii_monitor.strategy `
  --thresholds 1.0:4.0:0.25 `
  --min-hold-days 1,3,5,10,20 `
  --cost-bps 10 `
  --top 12 `
  --show-trades `
  --output data\strategy_grid_latest.csv
```

核心参数：

- `--thresholds`：触发换仓的溢价差阈值，支持百分比区间，例如 `1.0:4.0:0.25`。
- `--min-hold-days`：换仓后的最小持有天数，用于减少过度交易。
- `--cost-bps`：单次换仓总成本估计，单位 bps。
- `--codes`：自定义同指数 ETF 池。
- `--benchmark`：计算 alpha 的买入持有基准。
- `--max-buy-premiums`：可选，只允许买入溢价不高于指定值的 ETF。

输出中的 `alpha_value` 和 `alpha_pct` 是策略相对基准买入持有的超额收益；该模型使用场内收盘价计算，不只比较纸面溢价差。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest
```

测试使用本地样本采集器，不请求外部数据源。

## 刷新与 Linux 部署补充

服务端会在无人打开页面时继续按计划保存快照，前端下拉框只控制当前浏览器页面的自动刷新。

常用环境变量：

- `QDII_QUOTE_REFRESH_MINUTES=5`：A 股交易时段行情快照间隔。
- `QDII_PREMARKET_REFRESH_MINUTES=5`：盘前锚点刷新间隔。
- `QDII_FRONTEND_AUTO_REFRESH_SECONDS=300`：页面默认自动刷新间隔，设为 `0` 表示默认停止。
- `QDII_US_CLOSE_REFRESH_TIMES=04:05,04:20,05:05,05:20`：美股收盘附近的关键刷新时间，北京时间；同时覆盖夏令时和冬令时。
- `QDII_LOG_FILE=data/qdii-monitor.log`：Linux/Windows 启动脚本写入的日志文件。

Linux/VPS 上执行 `./run_web.sh` 会后台启动服务，写入 `data/qdii-monitor.pid` 并输出日志路径；停止使用 `./stop_web.sh`。如需公网访问，可用：

```bash
WEB_HOST=0.0.0.0 WEB_PORT=8010 ./run_web.sh
```

美股收盘快照用于后续核对 QDII 官方净值：调度会在上述关键时间同时刷新参考行情、盘前锚点和一份完整场内行情快照。
