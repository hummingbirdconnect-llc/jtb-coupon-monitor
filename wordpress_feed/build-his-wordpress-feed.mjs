import { createHash } from "node:crypto";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { formatHisCouponForCard } from "./his-card-formatter.mjs";

const scriptPath = fileURLToPath(import.meta.url);
const scriptDir = dirname(scriptPath);
const DEFAULT_MIN_COUPONS = 10;
const DEFAULT_MAX_AGE_HOURS = 3;
const MAX_FUTURE_SKEW_MINUTES = 5;
const MAX_DROP_RATIO = 0.4;

function parseArgs(argv) {
  const args = {};
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index];
    if (!key.startsWith("--")) {
      throw new Error(`引数の形式が不正です: ${key}`);
    }
    const name = key.slice(2);
    if (name === "allow-stale") {
      args[name] = true;
      continue;
    }
    const value = argv[index + 1];
    if (value === undefined || value.startsWith("--")) {
      throw new Error(`${key} の値がありません。`);
    }
    args[name] = value;
    index += 1;
  }
  return args;
}

function readJson(path, label) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    throw new Error(`${label}を読み取れません: ${error.message}`);
  }
}

function sha256(text) {
  return createHash("sha256").update(text).digest("hex");
}

function decodeHtml(value) {
  return String(value || "")
    .replace(/&#x([0-9a-f]+);/gi, (_, hex) =>
      String.fromCodePoint(Number.parseInt(hex, 16)),
    )
    .replace(/&#([0-9]+);/g, (_, decimal) =>
      String.fromCodePoint(Number.parseInt(decimal, 10)),
    )
    .replace(/&amp;/g, "&")
    .replace(/&quot;/g, '"')
    .replace(/&#039;|&apos;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">");
}

function extractAffiliateLinks(html) {
  const links = new Map();
  const pattern = /<a\s+href="([^"]+)"[^>]*>([^<]+)<\/a>/g;
  for (const match of html.matchAll(pattern)) {
    const title = decodeHtml(match[2]).trim();
    const url = decodeHtml(match[1]).trim();
    if (!title || !url) {
      continue;
    }
    if (links.has(title) && links.get(title) !== url) {
      throw new Error(`同じ表示タイトルに異なる遷移リンクがあります: ${title}`);
    }
    links.set(title, url);
  }
  return links;
}

function parseIsoDate(value, label) {
  const timestamp = Date.parse(String(value || ""));
  if (Number.isNaN(timestamp)) {
    throw new Error(`${label}の日時を解釈できません。`);
  }
  return timestamp;
}

function assertFreshRun(latest, allowStale, maxAgeHours, nowMs) {
  const fetchedAt = String(
    latest.official_fetched_at || latest.completed_at || "",
  );
  const fetchedMs = parseIsoDate(fetchedAt, "HIS公式確認");
  const startedMs = parseIsoDate(latest.started_at, "HIS監視開始");
  const completedMs = parseIsoDate(latest.completed_at, "HIS監視完了");

  if (completedMs < startedMs || fetchedMs < startedMs) {
    throw new Error("HIS監視の開始・確認・完了時刻の順序が不正です。");
  }
  if (fetchedMs > nowMs + MAX_FUTURE_SKEW_MINUTES * 60 * 1000) {
    throw new Error("HIS公式確認時刻が未来になっています。");
  }
  if (!allowStale && nowMs - fetchedMs > maxAgeHours * 60 * 60 * 1000) {
    throw new Error(
      `HIS公式確認から${maxAgeHours}時間を超えているため同期しません。`,
    );
  }
  return fetchedAt;
}

function previousSnapshotCount(dataDir, latestFile) {
  const files = readdirSync(dataDir)
    .filter(
      (name) =>
        /^coupons_\d{4}-\d{2}-\d{2}\.json$/.test(name) && name !== latestFile,
    )
    .sort()
    .reverse();
  if (files.length === 0) {
    return null;
  }
  const previous = readJson(join(dataDir, files[0]), "直前のHIS監視JSON");
  return Array.isArray(previous) ? previous.length : null;
}

function validateOfficialCoupons(coupons, latest, minCoupons, previousCount) {
  if (!Array.isArray(coupons)) {
    throw new Error("HIS監視JSONが配列ではありません。");
  }
  if (coupons.length !== Number(latest.coupon_count)) {
    throw new Error("HIS監視JSONと最新実行メタデータの件数が一致しません。");
  }
  if (coupons.length < minCoupons) {
    throw new Error(
      `HIS公式表示カードが${coupons.length}件しかないため同期しません。`,
    );
  }
  if (
    Number.isInteger(previousCount) &&
    previousCount >= minCoupons &&
    coupons.length < previousCount * (1 - MAX_DROP_RATIO)
  ) {
    throw new Error(
      `HIS公式表示カードが直前の${previousCount}件から${coupons.length}件へ急減したため同期しません。`,
    );
  }

  const ids = new Set();
  for (const coupon of coupons) {
    const id = String(coupon?.id || "").trim();
    if (!id) {
      throw new Error("監視元IDがないHISクーポンがあります。");
    }
    if (ids.has(id)) {
      throw new Error(`監視元IDが重複しています: ${id}`);
    }
    ids.add(id);

    if (
      coupon.official_visibility !== "visible" ||
      coupon.source_type !== "official_visible_dom" ||
      !String(coupon.fetch_method || "").includes("playwright_visible_dom") ||
      coupon.confidence !== "公式画面表示確認済み"
    ) {
      throw new Error(`公式画面の表示確認を満たさない候補があります: ${id}`);
    }
    if (!/^https:\/\/www\.his-j\.com\//.test(String(coupon.source_url || ""))) {
      throw new Error(`HIS公式ドメイン以外の取得元があります: ${id}`);
    }
    const checkedMs = parseIsoDate(coupon.official_checked_at, `公式確認 ${id}`);
    const runMs = parseIsoDate(
      latest.official_fetched_at || latest.completed_at,
      "HIS監視完了",
    );
    if (Math.abs(runMs - checkedMs) > 2 * 60 * 60 * 1000) {
      throw new Error(`候補の確認時刻が監視実行と離れています: ${id}`);
    }
    if (!String(coupon.title || "").trim()) {
      throw new Error(`クーポン名がありません: ${id}`);
    }
    if (!String(coupon.booking_period || "").trim()) {
      throw new Error(`予約期間がありません: ${id}`);
    }
    const codes = Array.isArray(coupon.coupon_codes)
      ? coupon.coupon_codes
      : [];
    if (codes.length === 0 || codes.some((entry) => !String(entry?.code || "").trim())) {
      throw new Error(`クーポンコードがありません: ${id}`);
    }
  }
}

function normalizeStatus(value) {
  if (value === "配布中") {
    return "active";
  }
  if (value === "配布終了") {
    return "ended";
  }
  return "invalid";
}

function buildItem(coupon, links, monitorFetchedAt) {
  const rawId = String(coupon.id || "").trim();
  const title = String(coupon.title || "").trim();
  const status = normalizeStatus(coupon.stock_status);
  const bookingPeriod = String(coupon.booking_period || "").trim();
  const travelPeriod = String(coupon.travel_period || "").trim();
  const destinationUrl = status === "ended" ? "" : links.get(title) || "";
  const presentation = formatHisCouponForCard(coupon);
  const warnings = [];

  if (status === "invalid") {
    warnings.push("配布状態を判定できません。");
  }
  if (!presentation.discountLabel) {
    warnings.push("割引額・特典を監視元で確認できません。");
  }
  if (!travelPeriod) {
    warnings.push("対象期間を監視元で確認できません。");
  }
  const target = String(coupon.target || "").trim();
  const targetTruncated =
    typeof coupon.target_truncated === "boolean"
      ? coupon.target_truncated
      : Array.from(target).length >= 200;
  if (targetTruncated) {
    warnings.push(
      "対象条件が監視処理の文字数上限に達しているため、全文の再確認が必要です。",
    );
  }
  if (/[本翌]日|翌\d/.test(bookingPeriod) || /[本翌]日|翌\d/.test(travelPeriod)) {
    warnings.push("相対日付を含むため、絶対日付への再確認が必要です。");
  }
  if ((status === "active" || status === "upcoming") && !destinationUrl) {
    warnings.push("同じ監視実行の遷移リンクを確認できません。");
  }

  const rawCodes = coupon.coupon_codes.map((entry) =>
    String(entry.code || "").trim(),
  );
  const formattedCodes = presentation.codes.map((entry) =>
    String(entry.code || "").trim(),
  );
  if (JSON.stringify(rawCodes) !== JSON.stringify(formattedCodes)) {
    throw new Error(`表示整形でクーポンコードが変わりました: ${rawId}`);
  }

  return {
    id: `his:${rawId}`,
    monitorSourceId: "his-official-monitor",
    providerSlug: "his",
    providerName: "HIS",
    status: status === "invalid" ? "ended" : status,
    couponName: presentation.couponName,
    discountLabel: presentation.discountLabel,
    bookingPeriod,
    travelPeriod,
    displayTitle: presentation.displayTitle,
    codes: presentation.codes,
    note: presentation.note,
    destinationUrl,
    buttonText: presentation.buttonText,
    sourceUpdatedAt: monitorFetchedAt,
    sourceCategory: presentation.sourceCategory,
    sourceTitle: presentation.sourceTitle,
    presentationVersion: presentation.presentationVersion,
    selectable: warnings.length === 0,
    warnings,
  };
}

export function buildHisWordPressFeed(options = {}) {
  const monitorRepo = resolve(options.monitorRepo || join(scriptDir, ".."));
  const outputPath = resolve(
    options.outputPath ||
      join(monitorRepo, "wordpress_feed", "his-monitor-feed.json"),
  );
  const allowStale = options.allowStale === true;
  const maxAgeHours = Number(options.maxAgeHours || DEFAULT_MAX_AGE_HOURS);
  const minCoupons = Number(options.minCoupons || DEFAULT_MIN_COUPONS);
  const nowMs = Number(options.nowMs || Date.now());
  if (!Number.isFinite(maxAgeHours) || maxAgeHours <= 0) {
    throw new Error("最大経過時間が不正です。");
  }
  if (!Number.isInteger(minCoupons) || minCoupons < 1) {
    throw new Error("最低件数が不正です。");
  }

  const latestPath = join(
    monitorRepo,
    "provider_check_data",
    "his",
    "latest.json",
  );
  const latest = readJson(latestPath, "HIS監視ステータス");
  if (
    latest.status !== "success" ||
    latest.check_type !== "official_monitor" ||
    !latest.latest_file
  ) {
    throw new Error("HIS公式監視の最新実行が成功状態ではありません。");
  }
  if (latest.ai_used === true) {
    throw new Error("AI生成値を含む監視結果は自動同期しません。");
  }
  const verifiedAt = assertFreshRun(
    latest,
    allowStale,
    maxAgeHours,
    nowMs,
  );

  const dataDir = join(monitorRepo, "his_coupon_data");
  const sourcePath = join(dataDir, latest.latest_file);
  const htmlPath = join(monitorRepo, "html_output", "his_coupons.html");
  if (!existsSync(sourcePath) || !existsSync(htmlPath)) {
    throw new Error("HIS監視JSONまたは同じ実行の遷移リンクHTMLがありません。");
  }

  const sourceText = readFileSync(sourcePath, "utf8");
  const htmlText = readFileSync(htmlPath, "utf8");
  const coupons = JSON.parse(sourceText);
  const previousCount = previousSnapshotCount(dataDir, latest.latest_file);
  validateOfficialCoupons(coupons, latest, minCoupons, previousCount);
  if (!htmlText.includes(`データ: ${latest.latest_file} /`)) {
    throw new Error("遷移リンクHTMLが最新のHIS監視JSONから生成されていません。");
  }

  const links = extractAffiliateLinks(htmlText);
  const items = coupons.map((coupon) =>
    buildItem(coupon, links, verifiedAt),
  );
  const itemIds = new Set(items.map((item) => item.id));
  if (itemIds.size !== items.length) {
    throw new Error("WordPress用フィードの識別子が重複しています。");
  }
  const selectableCount = items.filter((item) => item.selectable).length;

  const payload = {
    schemaVersion: 1,
    generatedAt: String(latest.completed_at || verifiedAt),
    source: {
      monitorSourceId: "his-official-monitor",
      providerSlug: "his",
      providerName: "HIS",
      monitorRepository: "jtb-coupon-monitor",
      monitorFile: `his_coupon_data/${latest.latest_file}`,
      monitorFileSha256: sha256(sourceText),
      monitorHtmlFile: "html_output/his_coupons.html",
      monitorHtmlSha256: sha256(htmlText),
      monitorStatusFile: "provider_check_data/his/latest.json",
      monitorFetchedAt: verifiedAt,
      verifiedAt,
      freshnessSlaHours: 30,
      monitorRawCount: coupons.length,
      officialComparison: {
        liveCount: coupons.length,
        sourceCount: coupons.length,
        matchedCount: coupons.length,
        codeMismatchCount: 0,
        periodMismatchCount: 0,
      },
      safetyChecks: {
        currentRunFresh: !allowStale,
        allItemsOfficialVisible: true,
        uniqueIds: true,
        previousCount,
        maximumAllowedDropRatio: MAX_DROP_RATIO,
      },
    },
    counts: {
      total: items.length,
      selectable: selectableCount,
      needsReview: items.length - selectableCount,
    },
    items,
  };

  mkdirSync(dirname(outputPath), { recursive: true });
  writeFileSync(outputPath, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
  return { outputPath, payload };
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const result = buildHisWordPressFeed({
    monitorRepo: args["monitor-repo"],
    outputPath: args.output,
    allowStale: args["allow-stale"] === true,
    maxAgeHours: args["max-age-hours"],
    minCoupons: args["min-coupons"],
  });
  console.log(
    `WordPress用HIS監視フィードを生成しました: ${result.payload.items.length}件` +
      `（通常${result.payload.counts.selectable}件、要確認${result.payload.counts.needsReview}件）`,
  );
}

if (resolve(process.argv[1] || "") === resolve(scriptPath)) {
  try {
    main();
  } catch (error) {
    console.error(`❌ ${error.message}`);
    process.exit(1);
  }
}
