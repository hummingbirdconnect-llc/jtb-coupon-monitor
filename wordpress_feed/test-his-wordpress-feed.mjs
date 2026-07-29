import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { buildHisWordPressFeed } from "./build-his-wordpress-feed.mjs";

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
const staleNowMs = Date.parse(latest.official_fetched_at) + 4 * 60 * 60 * 1000;

try {
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

  assert.equal(payload.items.length, 21, "最新のHIS表示件数が想定と異なります。");
  assert.equal(payload.source.officialComparison.liveCount, 21);
  assert.equal(payload.source.officialComparison.matchedCount, 21);
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

  console.log("✅ HIS WordPressフィード生成テスト: 21件と対象2件を確認");
} finally {
  rmSync(outputDir, { recursive: true, force: true });
}
