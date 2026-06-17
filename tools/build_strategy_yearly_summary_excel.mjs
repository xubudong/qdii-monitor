import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const repoRoot = "D:/codex_project/qdii-monitor";
const runsDir = path.join(repoRoot, "data", "strategy_runs");
const outputDir = path.join(repoRoot, "outputs");
const outputPath = path.join(outputDir, "strategy_returns_by_year.xlsx");
const years = ["2024", "2025", "2026"];

function round(value, digits = 2) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  if (!Number.isFinite(number)) return null;
  const factor = 10 ** digits;
  return Math.round(number * factor) / factor;
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

function codesLabel(codes) {
  const values = Array.isArray(codes) ? codes : [];
  const has161130 = values.includes("161130");
  const has159509 = values.includes("159509");
  const fullLike = values.includes("159941") && values.includes("513870") && values.includes("159513");
  if (values.length <= 4) return values.join(",");
  if (fullLike && has161130) return `全纳指ETF+161130(${values.length})`;
  if (fullLike) return `全纳指ETF(${values.length})`;
  if (has159509) return `含159509科技池(${values.length})`;
  return `${values.slice(0, 4).join(",")}...(${values.length})`;
}

function familyOf(row) {
  const codes = row.codes || [];
  const has161130 = codes.includes("161130");
  const isPair161130 = has161130 && codes.length === 2;
  if (row.mode === "dynamic-anchor-cycle") return "动态锚点";
  if (isPair161130) return `${row.benchmarkCode}↔161130二元池`;
  if (has161130) return "全池+161130替代仓";
  if (row.benchmarkCode === "513100") return "513100固定锚点";
  if (row.benchmarkCode === "513390") {
    if (codes.length <= 4) return "513390小池/清洁池";
    return "513390固定锚点";
  }
  return `${row.benchmarkCode || "-"}其他策略`;
}

function rowFromResult(run, result, rank, source) {
  const config = run.manifest.config || {};
  const codes = result.codes || config.codes || [];
  const startDate = result.start_date || config.start || "";
  const row = {
    runId: run.runId,
    createdAt: run.manifest.created_at || "",
    source,
    rank,
    mode: result.mode || config.mode || "",
    benchmarkCode: result.benchmark_code || config.benchmark || "",
    codes,
    codesLabel: codesLabel(codes),
    startDate,
    startYear: startDate.slice(0, 4),
    endDate: result.end_date || config.end || "",
    thresholdPct: round(result.threshold_pct),
    returnThresholdPct: round(result.return_threshold_pct),
    minHoldDays: result.min_hold_days ?? "",
    costBps: round(result.cost_bps),
    maxBuyPremiumPct: round(result.max_buy_premium_pct),
    alphaPct: round(result.alpha_pct),
    alphaValue: round(result.alpha_value),
    totalReturnPct: round(result.total_return_pct),
    benchmarkReturnPct: round(result.benchmark_return_pct),
    finalValue: round(result.final_value),
    benchmarkValue: round(result.benchmark_value),
    annualAlphaPct: round(result.annual_alpha_pct),
    maxDrawdownPct: round(result.max_drawdown_pct),
    switches: result.switches ?? result.trade_count ?? "",
    finalCode: result.final_code || "",
    runDir: run.runDir,
  };
  row.family = familyOf(row);
  row.paramKey = `${row.thresholdPct}/${row.returnThresholdPct}/${row.minHoldDays}`;
  return row;
}

async function listRuns() {
  const entries = await fs.readdir(runsDir, { withFileTypes: true });
  const runs = [];
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const runDir = path.join(runsDir, entry.name);
    const manifestPath = path.join(runDir, "manifest.json");
    if (!(await exists(manifestPath))) continue;
    const manifest = await readJson(manifestPath);
    const stat = await fs.stat(manifestPath);
    runs.push({ runId: entry.name, runDir, manifestPath, manifest, mtimeMs: stat.mtimeMs });
  }
  runs.sort((a, b) => b.mtimeMs - a.mtimeMs);
  return runs;
}

async function collectRows() {
  const runs = await listRuns();
  const bestRows = [];
  const topRows = [];
  for (const run of runs) {
    const best = run.manifest.best || {};
    bestRows.push(rowFromResult(run, best, 1, "manifest_best"));
    const topPath = path.join(run.runDir, "top_results.json");
    if (!(await exists(topPath))) continue;
    const top = await readJson(topPath);
    top.forEach((result, index) => {
      topRows.push(rowFromResult(run, result, index + 1, "top_results"));
    });
  }
  return { bestRows, topRows, allRows: [...bestRows, ...topRows] };
}

function bestBy(rows, predicate) {
  const candidates = rows.filter(predicate).filter((row) => row.alphaPct !== null);
  candidates.sort((a, b) => b.alphaPct - a.alphaPct || b.runId.localeCompare(a.runId));
  return candidates[0] || null;
}

function latestBy(rows, predicate) {
  return rows.find(predicate) || null;
}

function yearlyMainRows(rows, year) {
  const pool = rows.filter((row) => row.startYear === year);
  const selectors = [
    {
      view: "最佳参数",
      family: "513390固定锚点",
      note: "不含161130，在原纳指ETF池/消融池中选alpha最高。",
      pick: () => bestBy(pool, (row) => row.family === "513390固定锚点"),
    },
    {
      view: "稳健参数",
      family: "513390固定锚点",
      note: "固定 2.5/1.0/10，便于和前期结论对照。",
      pick: () => latestBy(pool, (row) => row.family === "513390固定锚点" && row.thresholdPct === 2.5 && row.returnThresholdPct === 1 && row.minHoldDays === 10),
    },
    {
      view: "最佳参数",
      family: "513100固定锚点",
      note: "以513100做锚点，在原纳指ETF池中选alpha最高。",
      pick: () => bestBy(pool, (row) => row.family === "513100固定锚点"),
    },
    {
      view: "稳健参数",
      family: "513100固定锚点",
      note: "固定 2.5/1.0/10。",
      pick: () => latestBy(pool, (row) => row.family === "513100固定锚点" && row.thresholdPct === 2.5 && row.returnThresholdPct === 1 && row.minHoldDays === 10),
    },
    {
      view: "当前参数",
      family: "动态锚点",
      note: "动态锚点模式；若该年份没跑则为空。",
      pick: () => bestBy(pool, (row) => row.family === "动态锚点"),
    },
    {
      view: "最佳参数",
      family: "全池+161130替代仓",
      note: "全纳指ETF池加入161130，在该家族中选alpha最高。",
      pick: () => bestBy(pool, (row) => row.family === "全池+161130替代仓"),
    },
    {
      view: "稳健参数",
      family: "全池+161130替代仓",
      note: "固定 5.0/1.0/10，对应高出5个点再切到低溢价替代仓。",
      pick: () => latestBy(pool, (row) => row.family === "全池+161130替代仓" && row.thresholdPct === 5 && row.returnThresholdPct === 1 && row.minHoldDays === 10),
    },
    {
      view: "最佳参数",
      family: "513390↔161130二元池",
      note: "只在513390和161130之间换仓。",
      pick: () => bestBy(pool, (row) => row.family === "513390↔161130二元池"),
    },
    {
      view: "最佳参数",
      family: "513100↔161130二元池",
      note: "只在513100和161130之间换仓；若没跑则为空。",
      pick: () => bestBy(pool, (row) => row.family === "513100↔161130二元池"),
    },
  ];
  return selectors.map((selector) => {
    const row = selector.pick();
    if (!row) {
      return {
        year,
        view: selector.view,
        family: selector.family,
        note: `${selector.note}（未找到匹配run）`,
      };
    }
    return { year, view: selector.view, note: selector.note, ...row };
  });
}

function matrixRows(yearRows) {
  const keys = [
    ["513390固定锚点", "最佳参数"],
    ["513390固定锚点", "稳健参数"],
    ["513100固定锚点", "最佳参数"],
    ["513100固定锚点", "稳健参数"],
    ["动态锚点", "当前参数"],
    ["全池+161130替代仓", "最佳参数"],
    ["全池+161130替代仓", "稳健参数"],
    ["513390↔161130二元池", "最佳参数"],
    ["513100↔161130二元池", "最佳参数"],
  ];
  return keys.map(([family, view]) => {
    const item = { family, view };
    for (const year of years) {
      const row = yearRows[year].find((entry) => entry.family === family && entry.view === view);
      item[`${year}Alpha`] = row?.alphaPct ?? "";
      item[`${year}Param`] = row?.paramKey ?? "";
      item[`${year}Switches`] = row?.switches ?? "";
      item[`${year}Run`] = row?.runId ?? "";
    }
    return item;
  });
}

function toRows(objects, headers) {
  return [headers.map((header) => header.title), ...objects.map((item) => headers.map((header) => item[header.key] ?? ""))];
}

function writeSheet(sheet, rows, widths) {
  sheet.showGridLines = false;
  if (!rows.length) return;
  sheet.getRangeByIndexes(0, 0, rows.length, rows[0].length).values = rows;
  sheet.freezePanes.freezeRows(1);
  const header = sheet.getRangeByIndexes(0, 0, 1, rows[0].length);
  header.format.fill.color = "#0f766e";
  header.format.font.color = "#ffffff";
  header.format.font.bold = true;
  widths.forEach((width, col) => {
    sheet.getRangeByIndexes(0, col, rows.length, 1).format.columnWidth = width;
  });
}

async function collectTrades(rows) {
  const selected = rows.filter((row) => row.runDir && row.runId).slice(0, 40);
  const trades = [];
  const seenRuns = new Set();
  for (const row of selected) {
    if (seenRuns.has(row.runId)) continue;
    seenRuns.add(row.runId);
    const tradePath = path.join(row.runDir, "best_trades.json");
    if (!(await exists(tradePath))) continue;
    const items = await readJson(tradePath);
    items.forEach((trade, index) => {
      trades.push({
        year: row.startYear,
        family: row.family,
        runId: row.runId,
        tradeNo: index + 1,
        tradeDate: trade.trade_date,
        fromCode: trade.from_code,
        toCode: trade.to_code,
        anchorCode: trade.anchor_code || row.benchmarkCode,
        spreadPct: round(Number(trade.spread) * 100),
        fromPremiumPct: round(Number(trade.from_premium) * 100),
        toPremiumPct: round(Number(trade.to_premium) * 100),
        valueAfterCost: round(trade.value_after_cost),
      });
    });
  }
  return trades;
}

async function main() {
  const { bestRows, topRows, allRows } = await collectRows();
  const yearRows = Object.fromEntries(years.map((year) => [year, yearlyMainRows(allRows, year)]));
  const workbook = Workbook.create();
  const overview = workbook.worksheets.add("年度矩阵");
  const sheet2024 = workbook.worksheets.add("2024主要策略");
  const sheet2025 = workbook.worksheets.add("2025主要策略");
  const sheet2026 = workbook.worksheets.add("2026主要策略");
  const allBest = workbook.worksheets.add("全部运行记录");
  const topDetail = workbook.worksheets.add("Top结果明细");
  const trades = workbook.worksheets.add("交易明细");
  const notes = workbook.worksheets.add("说明");

  const matrixHeaders = [
    { key: "family", title: "策略家族" },
    { key: "view", title: "视角" },
    { key: "2024Alpha", title: "2024 Alpha%" },
    { key: "2024Param", title: "2024 参数" },
    { key: "2024Switches", title: "2024 切仓" },
    { key: "2025Alpha", title: "2025 Alpha%" },
    { key: "2025Param", title: "2025 参数" },
    { key: "2025Switches", title: "2025 切仓" },
    { key: "2026Alpha", title: "2026 Alpha%" },
    { key: "2026Param", title: "2026 参数" },
    { key: "2026Switches", title: "2026 切仓" },
    { key: "2024Run", title: "2024 Run" },
    { key: "2025Run", title: "2025 Run" },
    { key: "2026Run", title: "2026 Run" },
  ];
  writeSheet(overview, toRows(matrixRows(yearRows), matrixHeaders), [180, 90, 90, 90, 75, 90, 90, 75, 90, 90, 75, 125, 125, 125]);

  const mainHeaders = [
    { key: "year", title: "年份维度" },
    { key: "view", title: "视角" },
    { key: "family", title: "策略家族" },
    { key: "note", title: "说明" },
    { key: "benchmarkCode", title: "锚点" },
    { key: "codesLabel", title: "标的池" },
    { key: "startDate", title: "开始" },
    { key: "endDate", title: "结束" },
    { key: "thresholdPct", title: "大阈值%" },
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
    { key: "source", title: "来源" },
  ];
  const widths = [80, 85, 170, 360, 80, 160, 90, 90, 85, 95, 85, 85, 100, 95, 115, 100, 85, 85, 125, 105];
  writeSheet(sheet2024, toRows(yearRows["2024"], mainHeaders), widths);
  writeSheet(sheet2025, toRows(yearRows["2025"], mainHeaders), widths);
  writeSheet(sheet2026, toRows(yearRows["2026"], mainHeaders), widths);

  const detailHeaders = [
    { key: "runId", title: "Run ID" },
    { key: "createdAt", title: "创建时间" },
    { key: "startYear", title: "年份" },
    { key: "source", title: "来源" },
    { key: "rank", title: "排名" },
    { key: "family", title: "策略家族" },
    { key: "mode", title: "模式" },
    { key: "benchmarkCode", title: "锚点" },
    { key: "codesLabel", title: "标的池" },
    { key: "thresholdPct", title: "大阈值%" },
    { key: "returnThresholdPct", title: "回归阈值%" },
    { key: "minHoldDays", title: "最短持有" },
    { key: "alphaPct", title: "Alpha%" },
    { key: "totalReturnPct", title: "策略收益%" },
    { key: "benchmarkReturnPct", title: "Benchmark收益%" },
    { key: "switches", title: "切仓次数" },
    { key: "finalCode", title: "最终持仓" },
    { key: "runDir", title: "目录" },
  ];
  writeSheet(allBest, toRows(bestRows, detailHeaders), [125, 140, 65, 110, 60, 170, 150, 80, 160, 85, 95, 85, 85, 95, 115, 85, 85, 420]);
  writeSheet(topDetail, toRows(topRows, detailHeaders), [125, 140, 65, 110, 60, 170, 150, 80, 160, 85, 95, 85, 85, 95, 115, 85, 85, 420]);

  const tradeHeaders = [
    { key: "year", title: "年份" },
    { key: "family", title: "策略家族" },
    { key: "runId", title: "Run ID" },
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
  const tradeRows = await collectTrades([...yearRows["2024"], ...yearRows["2025"], ...yearRows["2026"]]);
  writeSheet(trades, toRows(tradeRows, tradeHeaders), [65, 170, 125, 55, 90, 70, 70, 70, 75, 80, 80, 110]);

  const noteRows = [
    ["项目", "说明"],
    ["年度矩阵", "把 2024/2025/2026 三个起始时间维度并排展示；单元格是该策略家族在该年份下的 Alpha%。"],
    ["主要策略", "每个年份页只保留主要策略家族：原池固定锚点、动态锚点、161130替代仓、161130二元池等。"],
    ["最佳参数", "从全部 top_results.json 中按策略家族选 Alpha 最高的参数。"],
    ["稳健参数", "保留当前讨论中更接近实盘观察的固定参数，如 2.5/1.0/10 或 5.0/1.0/10。"],
    ["全部运行记录", "每个 run 的 manifest best 结果。"],
    ["Top结果明细", "每个 run 的 top_results.json 全部行，用于后续模型深挖。"],
    ["数据覆盖", `manifest run 数：${bestRows.length}；top result 行数：${topRows.length}。`],
  ];
  writeSheet(notes, noteRows, [160, 760]);

  await fs.mkdir(outputDir, { recursive: true });
  const inspect = await workbook.inspect({
    kind: "table",
    range: "年度矩阵!A1:N10",
    include: "values",
    tableMaxRows: 10,
    tableMaxCols: 14,
  });
  console.log(inspect.ndjson);
  const errors = await workbook.inspect({
    kind: "match",
    searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
    options: { useRegex: true, maxResults: 100 },
    summary: "formula error scan",
  });
  console.log(errors.ndjson);
  for (const check of [
    { sheetName: "年度矩阵", range: "A1:N12" },
    { sheetName: "2024主要策略", range: "A1:T12" },
    { sheetName: "2025主要策略", range: "A1:T12" },
    { sheetName: "2026主要策略", range: "A1:T12" },
    { sheetName: "全部运行记录", range: "A1:R18" },
    { sheetName: "Top结果明细", range: "A1:R18" },
    { sheetName: "交易明细", range: "A1:L18" },
    { sheetName: "说明", range: "A1:B10" },
  ]) {
    await workbook.render({ ...check, scale: 1, format: "png" });
  }
  const output = await SpreadsheetFile.exportXlsx(workbook);
  await output.save(outputPath);
  console.log(outputPath);
}

await main();
