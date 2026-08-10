import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  buildHisWordPressFeed,
  selectRegionCoupons,
  validateOfficialCoupons,
} from "./build-his-wordpress-feed.mjs";

const testDir = dirname(fileURLToPath(import.meta.url));
const monitorRepo = resolve(testDir, "..");
const outputDir = mkdtempSync(join(tmpdir(), "his-wordpress-feed-"));
const outputPath = join(outputDir, "feed.json");
const latest = JSON.parse(
  readFileSync(
    join(monitorRepo, "provider_check_data", "his", "latest.json"),
    "utf8",
  ),
);
const sourceCoupons = JSON.parse(
  readFileSync(
    join(monitorRepo, "his_coupon_data", latest.latest_file),
    "utf8",
  ),
);
const expectedPrimaryCoupons = sourceCoupons.filter((coupon) => {
  const regionCodes = Array.isArray(coupon.region_codes)
    ? coupon.region_codes
    : ["kanto"];
  return regionCodes.includes("kanto");
});
const staleNowMs = Date.parse(latest.official_fetched_at) + 4 * 60 * 60 * 1000;

try {
  assert.deepEqual(
    selectRegionCoupons([
      { id: "legacy" },
      { id: "common", region_codes: ["kanto", "hokkaido"] },
      { id: "hokkaido-only", region_codes: ["hokkaido"] },
    ]).map((coupon) => coupon.id),
    ["legacy", "common"],
    "WordPress向け首都圏版選択に他地域限定クーポンが混入しています。",
  );

  const regionalWithoutFeedFields = {
    ...sourceCoupons[0],
    id: "regional-without-feed-fields",
    title: "北海道版の監視専用カード",
    region_codes: ["hokkaido"],
    booking_period: "",
    coupon_codes: [],
  };
  assert.doesNotThrow(
    () =>
      validateOfficialCoupons(
        [regionalWithoutFeedFields],
        { ...latest, coupon_count: 1 },
        1,
        null,
        false,
      ),
    "他地域の監視専用カードにWordPress用フィールドを強制しています。",
  );
  assert.throws(
    () =>
      validateOfficialCoupons(
        [regionalWithoutFeedFields],
        { ...latest, coupon_count: 1 },
        1,
        null,
      ),
    /予約期間がありません/,
    "首都圏版のWordPress候補に必須フィールド不足を許しています。",
  );

  assert.throws(
    () =>
      buildHisWordPressFeed({
        monitorRepo,
        outputPath,
        nowMs: staleNowMs,
      }),
    /3時間を超えているため同期しません/,
    "古い監視結果を通常の自動同期用に生成できてしまいます。",
  );

  const { payload } = buildHisWordPressFeed({
    monitorRepo,
    outputPath,
    allowStale: true,
  });

  assert.equal(
    payload.items.length,
    expectedPrimaryCoupons.length,
    "最新のHIS首都圏版表示件数が想定と異なります。",
  );
  assert.equal(
    payload.source.officialComparison.liveCount,
    expectedPrimaryCoupons.length,
  );
  assert.equal(
    payload.source.officialComparison.matchedCount,
    expectedPrimaryCoupons.length,
  );
  assert.equal(payload.source.monitorRawCount, sourceCoupons.length);
  assert.equal(
    payload.source.monitorPrimaryRegionCount,
    expectedPrimaryCoupons.length,
  );
  assert.equal(payload.source.officialComparison.codeMismatchCount, 0);
  assert.equal(payload.source.safetyChecks.currentRunFresh, false);

  const domestic = payload.items.find(
    (item) => item.id === "his:d3000a952db4",
  );
  const urayasu = payload.items.find(
    (item) => item.id === "his:e7b7aa037781",
  );
  assert.ok(domestic, "国内ホテル宿泊クーポンがフィードにありません。");
  assert.ok(urayasu, "舞浜・浦安クーポンがフィードにありません。");
  assert.deepEqual(
    domestic.codes.map((entry) => entry.code),
    ["37Y6V9DA7GTB84QB"],
    "国内ホテル宿泊クーポンのコードが変わっています。",
  );
  assert.deepEqual(
    urayasu.codes.map((entry) => entry.code),
    ["BZAWUDBND5BMW6DD"],
    "舞浜・浦安クーポンのコードが変わっています。",
  );

  console.log(
    `✅ HIS WordPressフィード生成テスト: 首都圏版${expectedPrimaryCoupons.length}件と対象2件を確認`,
  );
} finally {
  rmSync(outputDir, { recursive: true, force: true });
}
