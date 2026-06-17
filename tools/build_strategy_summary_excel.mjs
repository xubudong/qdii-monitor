import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const repoRoot = "D:/codex_project/qdii-monitor";
const runsDir = path.join(repoRoot, "data", "strategy_runs");
const outputDir = path.join(repoRoot, "outputs");
const outputPath = path.join(outputDir, "strategy_returns_summary.xlsx");

function round(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return null;
  const factor = 10 ** digits;
  return Math.round(Number(value) * factor) / factor;
}

function pct(value) {
  return round(value, 2);
}

function money(value) {
  return round(value, 2);
}

function codesLabel(codes) {
  const values = Array.isArray(codes) ? codes : [];
  if (values.length <= 4) return values.join(",");
  const has161130 = values.includes("161130");
  const hasFull = values.includes("159941") && values.includes("513870") && values.includes("159513");
  if (hasFull && has161130) return `全纳指ETF+161130(${values.length})`;
  if (hasFull) return `全纳指ETF(${values.length})`;
  return `${values.slice(0, 3).join(",")}...(${values.length})`;
}

function strategyName(config, best) {
  const codes = config.codes || [];
  const codeSet = new Set(codes);
  const mode = config.mode || best.mode;
  const benchmark = config.benchmark || best.benchmark_code;
  const pair = codes.length === 2 && codeSet.has("161130");
  if (pair) return `${benchmark} ↔ 161130 直接换仓`;
  if (codeSet.has("161130")) return `${benchmark} 锚点 + 161130替代仓`;
  if (mode === "dynamic-anchor-cycle") return "动态锚点轮动";
  if (benchmark === "513100") return "513100固定锚点轮动";
  if (benchmark === "513390") return "513390固定锚点轮动";
  return `${benchmark || "-"} ${mode || "-"} 策略`;
}

async function exists(filePath) {
  try {
    await fs.access(filePath);
    return true;
  } catch {
    return false;
  }
}

async function readJson(filePath) {
  return JSON.parse(await fs.readFile(filePath, "utf-8"));
}

async function listRuns() {
  const entries = await fs.readdir(runsDir, { withFileTypes: true });
  const runs = [];
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const runDir = path.join(runsDir, entry.name);
    const manifestPath = path.join(runDir, "manifest.json");
    if (!(await exists(manifestPath))) continue;
    const stat = await fs.stat(manifestPath);
    const manifest = await readJson(manifestPath);
    runs.push({ runId: entry.name, runDir, manifestPath, stat, manifest });
  }
  runs.sort((a, b) => b.stat.mtimeMs - a.stat.mtimeMs);
  return runs;
}

function summaryRow(run) {
  const { manifest, runId } = run;
  const config = manifest.config || {};
  const best = manifest.best || {};
  const codes = config.codes || best.codes || [];
  return {
    runId,
    createdAt: manifest.created_at || "",
    strategy: strategyName(config, best),
    mode: config.mode || best.mode || "",
    benchmark: config.benchmark || best.benchmark_code || "",
    pool: config.pool_name || config.experiment_type || "",
    codesLabel: codesLabel(codes),
    codeCount: codes.length,
    has161130: codes.includes("161130") ? "是" : "否",
    startDate: best.start_date || config.start || "",
    endDate: best.end_date || config.end || "",
    switchThresholdPct: pct(best.threshold_pct),
    returnThresholdPct: pct(best.return_threshold_pct),
    minHoldDays: best.min_hold_days ?? "",
    costBps: pct(best.cost_bps),
    alphaPct: pct(best.alpha_pct),
    alphaValue: money(best.alpha_value),
    totalReturnPct: pct(best.total_return_pct),
    benchmarkReturnPct: pct(best.benchmark_return_pct),
    finalValue: money(best.final_value),
    benchmarkValue: money(best.benchmark_value),
    annualAlphaPct: pct(best.annual_alpha_pct),
    maxDrawdownPct: pct(best.max_drawdown_pct),
    switches: best.switches ?? "",
    anchorSwitches: best.anchor_switches ?? "",
    finalCode: best.final_code || "",
    runDir: run.runDir,
  };
}

function signature(row) {
  return [
    row.strategy,
    row.mode,
    row.benchmark,
    row.codesLabel,
    row.startDate,
    row.switchThresholdPct,
    row.returnThresholdPct,
    row.minHoldDays,
  ].join("|");
}

function findLatest(rows, predicate) {
  return rows.find(predicate) || null;
}

function buildFocusRows(allRows) {
  const focus = [
    {
      label: "原纳指ETF池：513390固定锚点 2.5/1.0/10",
      note: "此前主策略，作为 161130 替代仓的对照组。",
      match: (row) =>
        row.mode === "benchmark-cycle" &&
        row.benchmark === "513390" &&
        row.has161130 === "否" &&
        row.startDate.startsWith("2024") &&
        row.switchThresholdPct === 2.5 &&
        row.returnThresholdPct === 1 &&
        row.minHoldDays === 10,
    },
    {
      label: "动态锚点：60/5/40/0.5% + 2.5/1.0/10",
      note: "每天重算锚点，避免固定 513390 的年度漂移。",
      match: (row) =>
        row.mode === "dynamic-anchor-cycle" &&
        row.startDate.startsWith("2024") &&
        row.switchThresholdPct === 2.5 &&
        row.returnThresholdPct === 1 &&
        row.minHoldDays === 10,
    },
    {
      label: "原纳指ETF池：513100固定锚点 2.5/1.0/10",
      note: "检验 513100 作为锚点时的收益与触发频率。",
      match: (row) =>
        row.mode === "benchmark-cycle" &&
        row.benchmark === "513100" &&
        row.has161130 === "否" &&
        row.startDate.startsWith("2024") &&
        row.switchThresholdPct === 2.5 &&
        row.returnThresholdPct === 1 &&
        row.minHoldDays === 10,
    },
    {
      label: "全纳指ETF+161130：稳健参数 5.0/1.0/10",
      note: "更接近你提出的高出 5 个点再换到低溢价替代仓。",
      match: (row) =>
        row.has161130 === "是" &&
        row.codeCount > 2 &&
        row.startDate.startsWith("2024") &&
        row.switchThresholdPct === 5 &&
        row.returnThresholdPct === 1 &&
        row.minHoldDays === 10,
    },
    {
      label: "全纳指ETF+161130：优化参数 5.2/1.3/1",
      note: "这轮网格里收益更高，但持有期=1，实盘噪声和滑点要更谨慎。",
      match: (row) =>
        row.has161130 === "是" &&
        row.codeCount > 2 &&
        row.startDate.startsWith("2024") &&
        row.switchThresholdPct === 5.2 &&
        row.returnThresholdPct === 1.3 &&
        row.minHoldDays === 1,
    },
    {
      label: "直接二元池：513390 ↔ 161130，2024至今",
      note: "只验证 513390 和 161130 之间直接来回换仓。",
      match: (row) =>
        row.has161130 === "是" &&
        row.codeCount === 2 &&
        row.benchmark === "513390" &&
        row.startDate.startsWith("2024"),
    },
    {
      label: "直接二元池：513390 ↔ 161130，2025至今",
      note: "缩短时间窗，看近一年多策略是否仍有效。",
      match: (row) =>
        row.has161130 === "是" &&
        row.codeCount === 2 &&
        row.benchmark === "513390" &&
        row.startDate.startsWith("2025"),
    },
    {
      label: "直接二元池：513390 ↔ 161130，2026至今",
      note: "只看今年，样本较短，主要用于实盘观察。",
      match: (row) =>
        row.has161130 === "是" &&
        row.codeCount === 2 &&
        row.benchmark === "513390" &&
        row.startDate.startsWith("2026"),
    },
  ];
  return focus
    .map((item) => {
      const row = findLatest(allRows, item.match);
      return row ? { ...row, focusLabel: item.label, note: item.note } : null;
    })
    .filter(Boolean);
}

function toRows(objects, headers) {
  return [
    headers.map((header) => header.title),
    ...objects.map((item) => headers.map((header) => item[header.key] ?? "")),
  ];
}

async function collectTrades(mainRows) {
  const trades = [];
  for (const row of mainRows) {
    const filePath = path.join(row.runDir, "best_trades.json");
    if (!(await exists(filePath))) continue;
    const items = await readJson(filePath);
    for (const [index, trade] of items.entries()) {
      trades.push({
        runId: row.runId,
        strategy: row.strategy,
        tradeNo: index + 1,
        tradeDate: trade.trade_date,
        fromCode: trade.from_code,
        toCode: trade.to_code,
        anchorCode: trade.anchor_code || row.benchmark,
        spreadPct: pct(Number(trade.spread) * 100),
        fromPremiumPct: pct(Number(trade.from_premium) * 100),
        toPremiumPct: pct(Number(trade.to_premium) * 100),
        valueAfterCost: money(trade.value_after_cost),
      });
    }
  }
  return trades;
}

function writeSheet(sheet, rows, widths) {
  sheet.showGridLines = false;
  if (!rows.length) return;
  sheet.getRangeByIndexes(0, 0, rows.length, rows[0].length).values = rows;
  sheet.freezePanes.freezeRows(1);
  for (let col = 0; col < widths.length; col += 1) {
    sheet.getRangeByIndexes(0, col, rows.length, 1).format.columnWidth = widths[col];
  }
  const header = sheet.getRangeByIndexes(0, 0, 1, rows[0].length);
  header.format.fill.color = "#0f766e";
  header.format.font.color = "#ffffff";
  header.format.font.bold = true;
}

async function main() {
  const runs = await listRuns();
  const allRows = runs.map(summaryRow);
  const focusRows = buildFocusRows(allRows);

  const seen = new Set();
  const mainRows = [];
  for (const row of allRows) {
    const key = signature(row);
    if (seen.has(key)) continue;
    seen.add(key);
    mainRows.push(row);
  }
  mainRows.sort((a, b) => Number(b.alphaPct ?? -999) - Number(a.alphaPct ?? -999));

  const tradeRows = await collectTrades(mainRows.slice(0, 12));
  const workbook = Workbook.create();
  const focus = workbook.worksheets.add("关注策略对比");
  const main = workbook.worksheets.add("主要策略收益");
  const all = workbook.worksheets.add("全部运行记录");
  const trades = workbook.worksheets.add("交易明细");
  const notes = workbook.worksheets.add("说明");

  const summaryHeaders = [
    { key: "strategy", title: "策略" },
    { key: "mode", title: "模式" },
    { key: "benchmark", title: "锚点/Benchmark" },
    { key: "codesLabel", title: "标的池" },
    { key: "startDate", title: "开始" },
    { key: "endDate", title: "结束" },
    { key: "switchThresholdPct", title: "大阈值%" },
    { key: "returnThresholdPct", title: "回归阈值%" },
    { key: "minHoldDays", title: "最短持有" },
    { key: "alphaPct", title: "Alpha%" },
    { key: "alphaValue", title: "Alpha金额" },
    { key: "totalReturnPct", title: "策略收益%" },
    { key: "benchmarkReturnPct", title: "Benchmark收益%" },
    { key: "finalValue", title: "最终金额" },
    { key: "benchmarkValue", title: "Benchmark金额" },
    { key: "annualAlphaPct", title: "年化Alpha%" },
    { key: "maxDrawdownPct", title: "最大回撤%" },
    { key: "switches", title: "切仓次数" },
    { key: "anchorSwitches", title: "锚点切换" },
    { key: "finalCode", title: "最终持仓" },
    { key: "runId", title: "Run ID" },
  ];

  const focusHeaders = [
    { key: "focusLabel", title: "关注策略" },
    { key: "note", title: "备注" },
    { key: "benchmark", title: "锚点" },
    { key: "codesLabel", title: "标的池" },
    { key: "startDate", title: "开始" },
    { key: "endDate", title: "结束" },
    { key: "switchThresholdPct", title: "大阈值%" },
    { key: "returnThresholdPct", title: "回归阈值%" },
    { key: "minHoldDays", title: "最短持有" },
    { key: "alphaPct", title: "Alpha%" },
    { key: "alphaValue", title: "Alpha金额" },
    { key: "totalReturnPct", title: "策略收益%" },
    { key: "benchmarkReturnPct", title: "Benchmark收益%" },
    { key: "maxDrawdownPct", title: "最大回撤%" },
    { key: "switches", title: "切仓次数" },
    { key: "finalCode", title: "最终持仓" },
    { key: "runId", title: "Run ID" },
  ];
  writeSheet(focus, toRows(focusRows, focusHeaders), [260, 360, 80, 160, 90, 90, 85, 95, 85, 85, 100, 95, 115, 100, 85, 85, 125]);
  writeSheet(main, toRows(mainRows, summaryHeaders), [210, 150, 100, 160, 90, 90, 85, 95, 85, 85, 100, 95, 115, 100, 115, 100, 100, 85, 85, 85, 125]);

  const allHeaders = [
    { key: "runId", title: "Run ID" },
    { key: "createdAt", title: "创建时间" },
    ...summaryHeaders.slice(0, -1),
    { key: "codeCount", title: "标的数" },
    { key: "has161130", title: "含161130" },
    { key: "runDir", title: "目录" },
  ];
  writeSheet(all, toRows(allRows, allHeaders), [125, 140, 210, 150, 100, 160, 90, 90, 85, 95, 85, 85, 100, 95, 115, 100, 115, 100, 100, 85, 85, 85, 85, 85, 420]);

  const tradeHeaders = [
    { key: "runId", title: "Run ID" },
    { key: "strategy", title: "策略" },
    { key: "tradeNo", title: "序号" },
    { key: "tradeDate", title: "日期" },
    { key: "fromCode", title: "从" },
    { key: "toCode", title: "到" },
    { key: "anchorCode", title: "锚点" },
    { key: "spreadPct", title: "价差%" },
    { key: "fromPremiumPct", title: "原溢价%" },
    { key: "toPremiumPct", title: "新溢价%" },
    { key: "valueAfterCost", title: "换仓后金额" },
  ];
  writeSheet(trades, toRows(tradeRows, tradeHeaders), [125, 210, 55, 90, 70, 70, 70, 75, 80, 80, 110]);

  const noteRows = [
    ["项目", "说明"],
    ["关注策略对比", "按当前讨论主线手工挑选策略家族，方便快速比较。"],
    ["主要策略收益", "按策略签名去重，保留最新一次运行，并按 Alpha% 从高到低排序。"],
    ["全部运行记录", "保留 data/strategy_runs 下全部 manifest 记录，方便后续复盘。"],
    ["交易明细", "列出主要策略收益页前 12 个策略的 best_trades。"],
    ["口径", "金额基于回测初始资金，Alpha=策略最终金额-Benchmark最终金额。"],
    ["注意", "161130 是 LOF/联接基金，不是 ETF，实盘还要看盘口、成交额和申赎状态。"],
  ];
  writeSheet(notes, noteRows, [160, 620]);

  await fs.mkdir(outputDir, { recursive: true });
  const inspect = await workbook.inspect({
    kind: "table",
    range: "关注策略对比!A1:Q10",
    include: "values",
    tableMaxRows: 10,
    tableMaxCols: 17,
  });
  console.log(inspect.ndjson);
  const errors = await workbook.inspect({
    kind: "match",
    searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
    options: { useRegex: true, maxResults: 100 },
    summary: "formula error scan",
  });
  console.log(errors.ndjson);
  const renderChecks = [
    { sheetName: "关注策略对比", range: "A1:Q10" },
    { sheetName: "主要策略收益", range: "A1:U14" },
    { sheetName: "全部运行记录", range: "A1:W18" },
    { sheetName: "交易明细", range: "A1:K18" },
    { sheetName: "说明", range: "A1:B8" },
  ];
  for (const check of renderChecks) {
    await workbook.render({ ...check, scale: 1, format: "png" });
  }
  const output = await SpreadsheetFile.exportXlsx(workbook);
  await output.save(outputPath);
  console.log(outputPath);
}

await main();
