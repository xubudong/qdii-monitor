const state = { data: null, history: null, metric: "today", snapshotAt: "", snapshotMode: "day", autoRefreshTimer: null, autoRefreshConfigured: false };
const $ = (id) => document.getElementById(id);
const chartColors = ["#10644f", "#bd8721", "#275c8a", "#a43f34", "#67529c", "#008b84", "#c45f28", "#4f6b35", "#bd5484", "#536878", "#ca9c35", "#417d66", "#815733"];
const TABLE_COLUMN_FLAGS = {
  purchaseStatus: false,
  latestNotice: false,
  updateTime: false
};

function esc(value) {
  return String(value ?? "-")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function formatTime(value) {
  return value ? String(value).replace("T", " ") : "-";
}

function money(value) {
  return value == null ? "-" : Number(value).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function compactAmount(value) {
  if (value == null) return "-";
  const amount = Number(value);
  if (!Number.isFinite(amount)) return "-";
  if (Math.abs(amount) >= 100000000) return `${(amount / 100000000).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}亿`;
  if (Math.abs(amount) >= 10000) return `${(amount / 10000).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}万`;
  return amount.toLocaleString("zh-CN", { maximumFractionDigits: 0 });
}

function sizeCell(row) {
  const meta = row.metadata || {};
  const size = meta.asset_size_cny == null ? "-" : compactAmount(meta.asset_size_cny);
  const source = [meta.size_source, meta.metadata_source, formatTime(meta.metadata_fetched_at)].filter(Boolean).join(" · ");
  return `<span title="${esc(source || "暂无规模数据")}">${esc(size)}</span>`;
}

function inceptionCell(row) {
  const meta = row.metadata || {};
  return meta.inception_date ? esc(meta.inception_date) : "-";
}

function rate(value) {
  return value == null ? "-" : `${(Number(value) * 100).toFixed(2)}%`;
}

function rateClass(value) {
  return value > 0 ? "positive" : value < 0 ? "negative" : "";
}

function factorRate(value) {
  if (value == null) return "-";
  return `<span class="${rateClass(value)}">${rate(value)}</span>`;
}

function plainRate(value) {
  return value == null ? "-" : rate(value);
}

function toast(text, isError = false) {
  const node = $("toast");
  node.textContent = text;
  node.className = isError ? "error" : "";
  node.style.display = "block";
  clearTimeout(node.timer);
  node.timer = setTimeout(() => (node.style.display = "none"), 3600);
}

function shortTitle(value) {
  const text = String(value || "");
  return text.length > 160 ? `${text.slice(0, 160)}...` : text;
}

function valueSparkline(points, field, stroke = "#10644f") {
  const values = (points || []).map((point) => point[field]).filter((value) => value != null);
  if (values.length < 2) return '<span class="muted">-</span>';
  const min = Math.min(...values);
  const span = Math.max(Math.max(...values) - min, 0.00001);
  const coords = values.map((value, index) => `${(index / (values.length - 1) * 68 + 1).toFixed(1)},${(25 - ((value - min) / span) * 22).toFixed(1)}`).join(" ");
  return `<svg class="spark" viewBox="0 0 70 26"><polyline fill="none" stroke="${stroke}" stroke-width="1.8" points="${coords}"/></svg>`;
}

function pointDate(point) {
  return String(point?.trade_date || point?.captured_at || "").slice(0, 10);
}

function latestPointDate(points) {
  return (points || []).map(pointDate).filter(Boolean).sort().at(-1) || "";
}

function sameDayPoints(points) {
  const date = latestPointDate(points);
  return date ? (points || []).filter((point) => pointDate(point) === date) : [];
}

function sparkline(points) {
  const today = sameDayPoints(points);
  const tradingPoints = today.filter((point) => isCnTradingTime(point.captured_at));
  return valueSparkline(tradingPoints.length >= 2 ? tradingPoints : today, "premium_rate");
}

function referenceSparkline(points) {
  return valueSparkline(sameDayPoints(points), "latest_price", "#bd8721");
}

function noticePill(notice) {
  if (!notice) return '<span class="muted" title="交易所公告源中尚未匹配到恢复、暂停或限制申购事件；不代表当前申购状态">未抓到相关公告</span>';
  const kind = notice.notice_type || "公告";
  const cls = kind.includes("恢复") ? "notice-recover" : kind.includes("暂停") ? "notice-pause" : "";
  return `<a class="pill ${cls}" href="${esc(notice.url)}" target="_blank" title="最近匹配到的公告事件：${esc(notice.title)}">${esc(kind)}</a>`;
}

function purchaseLimit(status) {
  if (String(status.purchase_status || "").includes("场内交易")) return "限额未提供";
  const value = status.daily_limit;
  if (value == null) return "-";
  const amount = Number(value);
  if (amount >= 100000000) return "不限";
  if (amount >= 10000) return `${(amount / 10000).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}万元/日`;
  return `${amount.toLocaleString("zh-CN", { maximumFractionDigits: 2 })}元/日`;
}

function purchaseStatusCell(row) {
  const status = row.purchase_status;
  if (!status) return '<span class="muted">待获取</span>';
  const text = status.purchase_status || "未知";
  const exchangeOnly = text.includes("场内交易");
  if (exchangeOnly) {
    return '<span class="muted" title="天天基金仅返回“场内交易”，未提供可用于判断申购或限额的信息">-</span>';
  }
  const cls = text.includes("暂停") ? "notice-pause" : text.includes("开放") ? "notice-recover" : "status-limited";
  const sourceTitle = `天天基金辅助状态 · 采集于 ${formatTime(status.captured_at)}`;
  const warning = row.purchase_verification
    ? `<small class="verify-warning" title="${esc(row.purchase_verification)}">${esc(row.purchase_verification)}</small>`
    : "";
  return `<span class="pill ${cls}" title="${esc(sourceTitle)}">${esc(text)}</span><small class="limit">${esc(purchaseLimit(status))}</small>${warning}`;
}

function rotationSignalClass(level) {
  if (level === "action") return "action";
  if (level === "watch") return "watch";
  if (level === "missing") return "missing";
  return "idle";
}

function rotationSignal(group) {
  const rule = group.rotation_rule;
  if (!rule) return "";
  const signal = group.rotation_signal || {};
  const spreadText = signal.spread_to_lowest == null ? "-" : rate(signal.spread_to_lowest);
  const heldText = signal.held_code ? `当前持有 ${esc(signal.held_code)}` : "未导入持仓或持有基准";
  const minHold = rule.min_hold_days == null ? "-" : `${rule.min_hold_days} 个交易日`;
  const lowest = signal.lowest_code ? `${esc(signal.lowest_code)} ${rate(signal.lowest_premium)}` : "-";
  const pair = rule.candidate_code ? `${esc(rule.benchmark_code)} ↔ ${esc(rule.candidate_code)}` : esc(rule.benchmark_code);
  return `<div class="rotation-signal ${rotationSignalClass(signal.level)}">
    <div class="rotation-rule">
      <span class="signal-label">轮动观察</span>
      <strong>${pair}</strong>
      <span>大阈值 ${rate(rule.switch_threshold)}</span>
      <span>回归阈值 ${rate(rule.return_threshold)}</span>
      <span>最短持有 ${esc(minHold)}</span>
    </div>
    <div class="rotation-status">
      <strong>${esc(signal.title || "等待行情")}</strong>
      <span>${esc(signal.message || "行情刷新后判断是否触发提醒。")}</span>
      <small>当前差值 ${esc(spreadText)} · 最低候选 ${lowest} · ${heldText}</small>
    </div>
  </div>`;
}

function rotationGapCell(row, group) {
  const rule = group.rotation_rule;
  if (!rule) return "-";
  const threshold = rate(rule.switch_threshold);
  if (row.is_rotation_benchmark) {
    const gap = row.rotation_gap_to_lowest;
    return `<div class="rotation-gap ${rotationSignalClass(row.rotation_gap_level)}">
      <strong>${rate(gap)}</strong>
      <small>对最低 · 阈值 ${threshold}</small>
    </div>`;
  }
  if (row.rotation_gap_from_benchmark == null) return '<span class="muted">-</span>';
  const marker = row.is_rotation_lowest_candidate ? '<span class="pill low">最低候选</span>' : "";
  return `<div class="rotation-gap ${rotationSignalClass(row.rotation_gap_level)}">
    <strong>${rate(row.rotation_gap_from_benchmark)}</strong>
    <small>${esc(rule.benchmark_code)} - 本标的 ${marker}</small>
  </div>`;
}

function rotationGapCell(row, group) {
  const rule = group.rotation_rule;
  if (!rule) return "-";
  const threshold = rate(rule.switch_threshold);
  if (row.is_rotation_benchmark) {
    const gap = row.rotation_gap_to_lowest;
    return `<div class="rotation-gap ${rotationSignalClass(row.rotation_gap_level)}">
      <strong>${rate(gap)}</strong>
      <small>对候选 · 阈值 ${threshold}</small>
    </div>`;
  }
  if (row.rotation_gap_from_benchmark == null) return '<span class="muted">-</span>';
  const marker = row.is_rotation_lowest_candidate ? '<span class="pill low">轮动候选</span>' : "";
  return `<div class="rotation-gap ${rotationSignalClass(row.rotation_gap_level)}">
    <strong>${rate(row.rotation_gap_from_benchmark)}</strong>
    <small>${esc(rule.benchmark_code)} - 本标的 ${marker}</small>
  </div>`;
}

function renderReference(reference, history) {
  if (!reference) return "";
  const hasQuote = reference.latest_price != null;
  const sources = {
    "eastmoney_global_futures": "东方财富国际期货",
    "eastmoney.futures_global_spot": "东方财富国际期货",
    "eastmoney_global_index": "东方财富全球指数",
    "eastmoney.global_index": "东方财富全球指数"
  };
  const sourceName = sources[reference.source] || reference.source;
  return `<div class="reference">
    <span class="reference-label">盘中参考，不参与溢价排序</span>
    <strong>${esc(reference.display_name)}</strong>
    <small>${esc(reference.code)}</small>
    <span>最新 ${hasQuote ? money(reference.latest_price) : "-"}</span>
    <span class="${rateClass(reference.change_rate)}">较昨结 ${rate(reference.change_rate)}</span>
    <span>${referenceSparkline(history[reference.code])}</span>
    <span class="muted">${esc(reference.captured_at ? formatTime(reference.captured_at) : "等待行情刷新")} · ${esc(sourceName)}</span>
  </div>`;
}

function renderPremarketAnchor(anchor) {
  if (!anchor) {
    return '<div class="premarket-anchor missing"><strong>盘前锚点</strong><span class="muted">等待参考行情刷新</span></div>';
  }
  const expected = anchor.expected_change_rate;
  const futures = anchor.futures_change_rate;
  const currentTime = formatTime(anchor.captured_at);
  const baselineTime = formatTime(anchor.baseline_captured_at);
  return `<div class="premarket-anchor" title="${esc(anchor.note || "")}">
    <div>
      <strong>盘前预计涨跌 <span class="${rateClass(expected)}">${rate(expected)}</span></strong>
      <small>${esc(anchor.reference_name)} ${esc(anchor.reference_code)} · 当前采集 ${esc(currentTime)} · 基准 ${esc(baselineTime)}</small>
    </div>
    <div class="premarket-anchor-parts">
      <span>当前 ${money(anchor.reference_price)} · ${esc(currentTime)}</span>
      <span>最近A股收盘参考 ${money(anchor.baseline_price)} · ${esc(baselineTime)}</span>
      <span>期货自身 ${rate(futures)} · ${esc(currentTime)}</span>
    </div>
  </div>`;
}

function rotationBadges(row) {
  const badges = [];
  if (row.is_rotation_benchmark) badges.push('<span class="pill benchmark">轮动锚点</span>');
  if (row.is_rotation_lowest_candidate) badges.push('<span class="pill benchmark">轮动候选</span>');
  if (row.is_lowest_premium) badges.push('<span class="pill low">组内最低</span>');
  if (row.comparison_eligible === false) {
    badges.push('<span class="pill special" title="LOF/特殊参考，不参与组内ETF最低标记">特殊参考</span>');
  }
  return badges.join("");
}

function optionalHeaderCells() {
  return [
    TABLE_COLUMN_FLAGS.purchaseStatus ? '<th class="purchase-cell">天天基金状态 / 日限额</th>' : "",
    TABLE_COLUMN_FLAGS.latestNotice ? "<th>最新正式公告</th>" : "",
    TABLE_COLUMN_FLAGS.updateTime ? "<th>更新时间</th>" : ""
  ].join("");
}

function optionalRowCells(row) {
  return [
    TABLE_COLUMN_FLAGS.purchaseStatus ? `<td class="purchase-cell">${purchaseStatusCell(row)}</td>` : "",
    TABLE_COLUMN_FLAGS.latestNotice ? `<td>${noticePill(row.latest_notice)}</td>` : "",
    TABLE_COLUMN_FLAGS.updateTime ? `<td class="muted">${esc(formatTime(row.captured_at))}</td>` : ""
  ].join("");
}

function premiumFactorCells(row) {
  const factor = row.premium_factor || {};
  const title = `窗口 ${factor.window_days || 20} 日 · 权重 A ${factor.time_weight ?? 0.7} / B ${factor.cross_weight ?? 0.3}`;
  return `
    <td title="${esc(title)}">${plainRate(factor.history_mean)}</td>
    <td title="${esc("A = 当前溢价 - 20日均值")}">${factorRate(factor.time_deviation)}</td>
    <td>${plainRate(factor.pool_median)}</td>
    <td title="${esc("B = 当前溢价 - 池中位数")}">${factorRate(factor.cross_deviation)}</td>
    <td title="${esc("综合 = 0.7 × A + 0.3 × B")}">${factorRate(factor.combined_deviation)}</td>
  `;
}

function fundNameCell(row) {
  const official = row.official_url
    ? `<a class="official-link" href="${esc(row.official_url)}" target="_blank" rel="noopener noreferrer" title="打开基金公司官网详情页">官网</a>`
    : "";
  return `<strong>${esc(row.display_name)} ${rotationBadges(row)} ${official}</strong><small>${esc(row.code)}</small>`;
}

function navEstimateCell(row) {
  const label = row.premium_source === "estimated_nav" || row.premium_source === "estimated_nav_no_price"
    ? '<small class="estimate-label">估算 NAV</small>'
    : row.premium_source === "estimated_nav_unavailable"
      ? '<small class="estimate-label unavailable">估算条件不足</small>'
    : "";
  const formula = row.premium_formula
    ? `<small class="estimate-formula" title="${esc(row.premium_note || "")}">${esc(row.premium_formula)}</small>`
    : "";
  return `<span title="${esc(row.premium_note || "")}">${money(row.iopv)}</span>${label}${formula}`;
}

function premiumCell(row) {
  const label = row.premium_source === "estimated_nav"
    ? '<small class="estimate-label">参考估算</small>'
    : row.premium_source === "estimated_nav_no_price"
      ? '<small class="estimate-label unavailable">缺场内价</small>'
    : row.premium_source === "estimated_nav_unavailable"
      ? '<small class="estimate-label unavailable">暂不计算</small>'
    : "";
  return `<span class="${rateClass(row.premium_rate)}" title="${esc(row.premium_note || "")}">${rate(row.premium_rate)}</span>${label}`;
}

function renderGroups(groups, history, referenceHistory) {
  $("groups").innerHTML = groups.map((group) => `
    <article class="group">
      <div class="group-head">
        <h3>${esc(group.name)}</h3>
        ${renderPremarketAnchor(group.premarket_anchor)}
        ${rotationSignal(group)}
      </div>
      ${renderReference(group.reference, referenceHistory)}
      <div class="table-scroll"><table>
        <thead><tr><th>标的</th><th>交易所</th><th>最新价</th><th>涨跌幅</th><th>IOPV/估算NAV</th><th>折溢价率</th><th>20日均值</th><th>A偏离</th><th>池中位数</th><th>B偏离</th><th>综合偏离</th><th>规模</th><th>成立时间</th><th>今日成交额</th><th>近期轨迹</th><th>持有</th>${optionalHeaderCells()}</tr></thead>
        <tbody>${group.rows.map((row) => `
          <tr class="${row.is_rotation_benchmark ? "rotation-benchmark-row" : ""}">
            <td class="name">${fundNameCell(row)}</td>
            <td>${esc(row.exchange)}</td>
            <td>${money(row.latest_price)}</td>
            <td class="${rateClass(row.change_rate)}">${rate(row.change_rate)}</td>
            <td>${navEstimateCell(row)}</td>
            <td>${premiumCell(row)}</td>
            ${premiumFactorCells(row)}
            <td>${sizeCell(row)}</td>
            <td>${inceptionCell(row)}</td>
            <td>${compactAmount(row.turnover_amount)}</td>
            <td>${sparkline(history[row.code])}</td>
            <td>${row.holding ? '<span class="pill">持有</span>' : "-"}</td>
            ${optionalRowCells(row)}
          </tr>`).join("")}</tbody>
      </table></div>
    </article>`).join("");
}

function chartTime(value) {
  return formatTime(value).slice(0, 22);
}

function latestIntradayDate(payload) {
  const premiumDates = Object.values(payload.premium_history || {}).flatMap((rows) => rows.map((row) => String(row.captured_at || "").slice(0, 10)));
  const anchorDates = Object.values(payload.premarket_anchor_history || {}).flatMap((rows) => rows.map((row) => String(row.captured_at || "").slice(0, 10)));
  const dates = [...premiumDates, ...anchorDates];
  return dates.filter(Boolean).sort().at(-1) || "";
}

function isCnTradingTime(value) {
  const time = String(value || "").slice(11, 16);
  return (time >= "09:30" && time <= "11:30") || (time >= "13:00" && time <= "15:00");
}

function plotConfig() {
  return { responsive: true, displaylogo: false, scrollZoom: true, modeBarButtonsToRemove: ["lasso2d", "select2d"] };
}

function plotLayout(title, yTitle, options = {}) {
  const xaxis = { type: "date", rangeslider: { visible: true, thickness: 0.08 }, gridcolor: "#e5e8e2" };
  if (options.compactCnTradingBreaks) {
    xaxis.rangebreaks = [
      { pattern: "hour", bounds: [11.5, 13] }
    ];
  }
  return {
    title: { text: title, font: { size: 14 }, x: 0.01 },
    margin: { l: 62, r: 18, t: 46, b: 52 },
    dragmode: "pan",
    hovermode: "x unified",
    hoverlabel: { align: "left", bgcolor: "#ffffff", bordercolor: "#dbe1dc", font: { color: "#14201e", size: 12 }, namelength: -1 },
    paper_bgcolor: "#ffffff",
    plot_bgcolor: "#fcfcfa",
    legend: { orientation: "h", y: -0.2 },
    xaxis,
    yaxis: { title: yTitle, ticksuffix: yTitle.includes("%") ? "%" : "", gridcolor: "#e5e8e2", zerolinecolor: "#cdd3cb" }
  };
}

function plotEmpty(id, text) {
  $(id).innerHTML = `<div class="history-empty">${esc(text)}</div>`;
}

function renderHistory(payload) {
  const daily = state.metric === "daily";
  const intradayDate = latestIntradayDate(payload);
  $("historyNote").textContent = daily
    ? "历史日线按场内收盘价 / 官方单位净值（NAV）计算。悬停仅显示各标的溢价率；点击图例可隐藏曲线，双击图例可只查看单只标的。"
    : `当日盘中按最新价 / IOPV 计算，当前展示 ${intradayDate || "尚无采样日"} 的已保存快照。悬停仅显示各标的溢价率；参考行情仍保留原始价格与昨结供核对。`;
  $("historyCharts").innerHTML = payload.groups.map((group, index) => `
    <article class="history-card">
      <h3>${esc(group.name)}</h3>
      <p class="muted chart-subhead">${daily ? "ETF 日线溢价：收盘价 / NAV - 1" : "ETF 盘中溢价：最新价 / IOPV - 1"}</p>
      ${!daily ? `<div class="chart-title-row"><p class="history-chart-title">盘前价格锚点</p><button type="button" class="chart-fullscreen" data-chart="anchorPlot${index}">全屏查看</button></div><div id="anchorPlot${index}" class="history-chart reference-chart"></div>` : ""}
      <div class="chart-title-row"><p class="history-chart-title">${daily ? "历史日线溢价率" : "当日盘中溢价率"}</p><button type="button" class="chart-fullscreen" data-chart="premiumPlot${index}">全屏查看</button></div>
      <div id="premiumPlot${index}" class="history-chart"></div>
      ${!daily && group.reference ? `<div class="chart-title-row"><p class="history-chart-title">盘中参考原始行情：${esc(group.reference.display_name)} ${esc(group.reference.code)}</p><button type="button" class="chart-fullscreen" data-chart="referencePlot${index}">全屏查看</button></div><div id="referencePlot${index}" class="history-chart reference-chart"></div>` : ""}
    </article>`).join("");

  if (!window.Plotly) {
    $("historyCharts").innerHTML = '<div class="history-empty">Plotly 资源未加载，无法绘制交互曲线。</div>';
    return;
  }
  payload.groups.forEach((group, index) => {
    const traces = group.rows.map((row, position) => {
      const points = daily
        ? (payload.daily_premium_history?.[row.code] || [])
        : (payload.premium_history[row.code] || []).filter((point) => String(point.captured_at).startsWith(intradayDate) && isCnTradingTime(point.captured_at));
      return {
        name: `${row.display_name} ${row.code}${row.comparison_eligible === false ? "（特殊参考）" : ""}`,
        x: points.map((point) => point.trade_date || point.captured_at),
        y: points.map((point) => point.premium_rate == null ? null : Number(point.premium_rate) * 100),
        customdata: points.map((point) => daily
          ? [formatTime(point.trade_date), point.close_price, point.nav]
          : [formatTime(point.captured_at), point.latest_price, point.iopv]),
        type: "scatter",
        mode: "lines+markers",
        line: { color: chartColors[position % chartColors.length], width: 2, dash: row.comparison_eligible === false ? "dash" : "solid" },
        marker: { size: daily ? 4 : 6 },
        connectgaps: true,
        hovertemplate: "%{fullData.name} | <b>%{y:.2f}%</b><br>%{customdata[0]}<extra></extra>"
      };
    }).filter((trace) => trace.x.length);
    if (traces.length) {
      Plotly.newPlot(
        `premiumPlot${index}`,
        traces,
        plotLayout(daily ? "历史日线溢价率" : "当日盘中溢价率", "溢价率 (%)", { compactCnTradingBreaks: !daily }),
        plotConfig()
      );
    } else {
      plotEmpty(`premiumPlot${index}`, daily ? "尚无历史日线数据，请先刷新历史日线；若按钮提示接口不存在，请重启本地服务。" : "当日尚无有效 IOPV 盘中快照。");
    }
    if (!daily) {
      const anchors = (payload.premarket_anchor_history?.[group.id] || []).filter((row) => String(row.captured_at).startsWith(intradayDate));
      if (anchors.length) {
        Plotly.newPlot(
          `anchorPlot${index}`,
          [
            {
              name: "盘前预计涨跌",
              x: anchors.map((row) => row.captured_at),
              y: anchors.map((row) => row.expected_change_rate == null ? null : Number(row.expected_change_rate) * 100),
              customdata: anchors.map((row) => [row.futures_change_rate == null ? null : Number(row.futures_change_rate) * 100, formatTime(row.baseline_captured_at), formatTime(row.captured_at)]),
              type: "scatter",
              mode: "lines+markers",
              line: { color: "#275c8a", width: 2 },
              marker: { size: 6 },
              hovertemplate: "预计 <b>%{y:.2f}%</b><br>期货自身 %{customdata[0]:.2f}%<br>当前 %{customdata[2]}<br>基准 %{customdata[1]}<extra></extra>"
            }
          ],
          plotLayout("盘前锚点：当前参考 / 最近A股收盘参考 - 1", "预计涨跌(%)"),
          plotConfig()
        );
      } else {
        plotEmpty(`anchorPlot${index}`, "当日尚无盘前锚点快照。");
      }
    }
    if (!daily && group.reference) {
      const rows = (payload.reference_history[group.reference.code] || []).filter((row) => String(row.captured_at).startsWith(intradayDate));
      if (rows.length) {
        const trace = {
          name: `${group.reference.display_name} ${group.reference.code}`,
          x: rows.map((row) => row.captured_at),
          y: rows.map((row) => row.latest_price),
          customdata: rows.map((row) => [formatTime(row.captured_at), row.change_rate == null ? null : Number(row.change_rate) * 100, row.previous_settle]),
          type: "scatter",
          mode: "lines+markers",
          line: { color: "#bd8721", width: 2 },
          marker: { size: 6 },
          connectgaps: true,
          hovertemplate: "价格 <b>%{y:.2f}</b><br>较昨结 %{customdata[0]:.2f}% | 昨结 %{customdata[1]:.2f}<extra></extra>"
        };
        Plotly.newPlot(`referencePlot${index}`, [trace], plotLayout("原始参考行情（不参与溢价排序）", "价格"), plotConfig());
      } else {
        plotEmpty(`referencePlot${index}`, "当日尚无参考行情快照。");
      }
    }
  });
  bindFullscreenButtons();
}

function resizePlot(plot) {
  if (plot && window.Plotly && plot.classList.contains("js-plotly-plot")) {
    window.Plotly.Plots.resize(plot);
  }
}

function bindFullscreenButtons() {
  document.querySelectorAll(".chart-fullscreen").forEach((button) => {
    button.addEventListener("click", async () => {
      const plot = $(button.dataset.chart);
      if (!plot || !plot.classList.contains("js-plotly-plot")) return;
      try {
        await plot.requestFullscreen?.();
        window.setTimeout(() => resizePlot(plot), 120);
      } catch (error) {
        toast("浏览器未能进入全屏模式", true);
      }
    });
  });
}

function renderHoldings(payload) {
  $("portfolioTotals").innerHTML = `
    <div class="metric"><span>配置池持仓市值</span><strong>${money(payload.total_value)}</strong></div>
    <div class="metric"><span>未实现盈亏</span><strong class="${rateClass(payload.total_unrealized_pnl)}">${money(payload.total_unrealized_pnl)}</strong></div>`;
  if (!payload.rows.length) {
    $("holdings").innerHTML = '<p class="muted">尚未上传匹配配置池的持仓文件。</p>';
    return;
  }
  $("holdings").innerHTML = `<div class="table-scroll"><table><thead><tr><th>标的</th><th>份额</th><th>成本</th><th>现价</th><th>市值</th><th>浮盈亏</th><th>收益率</th></tr></thead><tbody>${
    payload.rows.map((row) => `<tr><td>${esc(row.display_name || row.name)}</td><td>${money(row.shares)}</td><td>${money(row.average_cost)}</td><td>${money(row.latest_price)}</td><td>${money(row.market_value)}</td><td class="${rateClass(row.unrealized_pnl)}">${money(row.unrealized_pnl)}</td><td class="${rateClass(row.pnl_rate)}">${rate(row.pnl_rate)}</td></tr>`).join("")
  }</tbody></table></div>`;
}

function renderNotices(notices) {
  $("notices").innerHTML = notices.length ? notices.slice(0, 30).map((row) => `
    <div class="event"><a href="${esc(row.url)}" target="_blank">${esc(row.title)}</a>
      <p>${esc(row.code)} · ${esc(row.source)} · ${esc(row.notice_type)} · ${esc(formatTime(row.published_at || row.fetched_at))}</p>
    </div>`).join("") : '<p class="muted">尚未采集到配置标的的申购相关公告。</p>';
}

function renderQuota(quota) {
  if (!quota) {
    $("quota").innerHTML = '<p class="muted">尚未采集 QDII 额度公告。</p>';
    return;
  }
  const items = quota.items || [];
  $("quota").innerHTML = `
    <div class="event"><a href="${esc(quota.url)}" target="_blank">${esc(quota.title)}</a><p>采集于 ${esc(formatTime(quota.fetched_at))} · 本次新增链接 ${quota.new_items.length}</p></div>
    ${items.slice(0, 8).map((item) => `<div class="event"><a href="${esc(item.url)}" target="_blank">${esc(item.title)}</a></div>`).join("")}`;
}

function renderTasks(tasks) {
  const labels = { quotes: "溢价快照", premarket_anchor: "盘前锚点", daily_premium: "历史日线", notices: "公告/辅助状态", quota: "QDII 额度" };
  $("taskStatus").innerHTML = ["quotes", "premarket_anchor", "daily_premium", "notices", "quota"].map((key) => {
    const task = tasks.find((item) => item.task === key);
    if (!task) return `<span class="task">${labels[key]}：等待首次采集</span>`;
    const time = formatTime(task.last_succeeded_at || task.last_started_at);
    const error = task.status === "error";
    const warning = task.status === "warning";
    const statusText = error ? "失败" : warning ? "参考缺失" : "已更新";
    return `<span class="task ${error ? "error" : warning ? "warning" : ""}" title="${esc(shortTitle(task.last_error))}">${labels[key]}：${statusText} ${esc(time)}</span>`;
  }).join("");
}

function renderSnapshotSelect(payload) {
  const select = $("snapshotSelect");
  const snapshots = payload.snapshots || [];
  const current = state.snapshotAt;
  select.innerHTML = [
    '<option value="">最新快照</option>',
    ...snapshots.map((row) => {
      const selected = row.captured_at === current ? " selected" : "";
      return `<option value="${esc(row.captured_at)}"${selected}>${esc(formatTime(row.captured_at))} (${esc(row.fund_count)}只)</option>`;
    })
  ].join("");
}

async function loadDashboard() {
  const params = new URLSearchParams();
  params.set("snapshot_mode", state.snapshotMode);
  if (state.snapshotAt) params.set("snapshot_at", state.snapshotAt);
  const query = `?${params.toString()}`;
  const response = await fetch(`/api/dashboard${query}`);
  if (!response.ok) throw new Error("仪表盘读取失败");
  state.data = await response.json();
  applyAutoRefreshConfig(state.data);
  renderSnapshotSelect(state.data);
  renderGroups(state.data.groups, state.data.premium_history || {}, state.data.reference_history || {});
  renderHoldings(state.data.holdings);
  renderQuota(state.data.quota);
  renderNotices(state.data.notices);
  renderTasks(state.data.tasks);
}

function applyAutoRefreshConfig(payload) {
  if (state.autoRefreshConfigured) return;
  const seconds = Number(payload?.refresh_config?.frontend_auto_refresh_seconds);
  if (!Number.isFinite(seconds) || seconds < 0) return;
  const select = $("autoRefreshSelect");
  const value = seconds === 0 ? "0" : String(Math.round(seconds * 1000));
  if (![...select.options].some((option) => option.value === value)) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = `每 ${Math.max(1, Math.round(seconds / 60))} 分钟`;
    select.insertBefore(option, select.lastElementChild);
  }
  select.value = value;
  setAutoRefreshInterval(value);
  state.autoRefreshConfigured = true;
}

async function loadHistory() {
  const response = await fetch("/api/history?limit=2000&daily_limit=760");
  if (response.ok) {
    state.history = await response.json();
  } else if (response.status === 404 && state.data) {
    state.history = {
      groups: state.data.groups,
      premium_history: state.data.premium_history || {},
      daily_premium_history: {},
      reference_history: state.data.reference_history || {},
      premarket_anchor_history: state.data.premarket_anchor_history || {}
    };
  } else {
    throw new Error("历史数据读取失败");
  }
  renderHistory(state.history);
}

function setMetric(metric) {
  state.metric = metric;
  document.querySelectorAll(".metric-tab").forEach((button) => {
    const active = button.dataset.metric === metric;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  $("historyRefresh").hidden = metric !== "daily";
  if (state.history) renderHistory(state.history);
}

async function switchView(view) {
  document.querySelectorAll(".view-tab").forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  $("liveView").hidden = view !== "live";
  $("historyView").hidden = view !== "history";
  if (view === "history") {
    await loadHistory();
  }
}

async function refreshDailyHistory(button) {
  button.disabled = true;
  try {
    const response = await fetch("/api/refresh/history?days=1095", { method: "POST" });
    if (response.status === 404) {
      throw new Error("当前运行的服务尚未加载历史日线接口，请重启服务后再刷新。");
    }
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "历史日线采集失败");
    await loadDashboard();
    await loadHistory();
    if (payload.price_requests === 0) {
      toast(`历史日线已是最新：截至 ${payload.target_date}，未发起代理历史请求`);
    } else {
      toast(`历史日线已刷新：请求 ${payload.price_requests} 只标的，写入 ${payload.written} 条，跳过 ${payload.skipped_codes} 只`);
    }
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function refresh(kind, button) {
  button.disabled = true;
  try {
    const response = await fetch(`/api/refresh/${kind}`, { method: "POST" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "采集失败");
    await loadDashboard();
    toast("刷新完成");
  } catch (error) {
    await loadDashboard().catch(() => {});
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function autoRefreshQuotes() {
  try {
    const response = await fetch("/api/refresh/quotes", { method: "POST" });
    if (!response.ok) throw new Error("auto refresh failed");
    if (!state.snapshotAt) await loadDashboard();
  } catch (error) {
    await loadDashboard().catch(() => {});
  }
}

function setAutoRefreshInterval(ms) {
  if (state.autoRefreshTimer) {
    clearInterval(state.autoRefreshTimer);
    state.autoRefreshTimer = null;
  }
  const interval = Number(ms || 0);
  if (interval > 0) {
    state.autoRefreshTimer = setInterval(() => autoRefreshQuotes(), interval);
  }
}

$("quoteRefresh").addEventListener("click", () => {
  state.snapshotAt = "";
  refresh("quotes", $("quoteRefresh"));
});
$("noticeRefresh").addEventListener("click", () => refresh("notices", $("noticeRefresh")));
$("quotaRefresh").addEventListener("click", () => refresh("quota", $("quotaRefresh")));
$("autoRefreshSelect").addEventListener("change", () => {
  state.autoRefreshConfigured = true;
  setAutoRefreshInterval($("autoRefreshSelect").value);
  toast($("autoRefreshSelect").value === "0" ? "已停止自动刷新" : `自动刷新间隔已改为 ${$("autoRefreshSelect").selectedOptions[0].textContent}`);
});
$("snapshotModeSelect").addEventListener("change", async () => {
  state.snapshotMode = $("snapshotModeSelect").value;
  state.snapshotAt = "";
  await loadDashboard().catch((error) => toast(error.message, true));
});
$("snapshotSelect").addEventListener("change", async () => {
  state.snapshotAt = $("snapshotSelect").value;
  await loadDashboard().catch((error) => toast(error.message, true));
});
$("snapshotAll").addEventListener("click", () => {
  state.metric = "today";
  switchView("history").catch((error) => toast(error.message, true));
});
document.querySelectorAll(".view-tab").forEach((button) => {
  button.addEventListener("click", () => switchView(button.dataset.view).catch((error) => toast(error.message, true)));
});
document.querySelectorAll(".metric-tab").forEach((button) => {
  button.addEventListener("click", () => setMetric(button.dataset.metric));
});
$("historyRefresh").addEventListener("click", () => refreshDailyHistory($("historyRefresh")));
document.addEventListener("fullscreenchange", () => {
  const plot = document.fullscreenElement;
  if (plot) {
    window.setTimeout(() => resizePlot(plot), 120);
  } else {
    document.querySelectorAll(".history-chart").forEach((chart) => window.setTimeout(() => resizePlot(chart), 120));
  }
});
$("uploadButton").addEventListener("click", async () => {
  const file = $("holdingFile").files[0];
  if (!file) return toast("请选择券商导出的 table.xls", true);
  const form = new FormData();
  form.append("file", file);
  try {
    const response = await fetch("/api/holdings/upload", { method: "POST", body: form });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "上传失败");
    await loadDashboard();
    toast(`已导入 ${payload.count} 个配置池持仓`);
  } catch (error) {
    toast(error.message, true);
  }
});

loadDashboard().catch((error) => toast(error.message, true));
setAutoRefreshInterval($("autoRefreshSelect").value);
