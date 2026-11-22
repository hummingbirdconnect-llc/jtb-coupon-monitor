#!/bin/bash
# 毎朝06:45・15:30（launchd: com.hbconnect.provider-fetch-jp）。日本のIPでしか取れない公式ページ（Booking.com）を取得し、
# 中身が変わったときは provider_check_data / official_source_data / codex_audit_queue をcommitしてpushする。
# 中身が変わらなくても、取得に成功した会社はチェック時刻の記録（provider_check_data/<id>/latest.json・
# その回の check_*.json・official_source_data/<id>/state.json）だけをcommitしてpushする（2026-11-11 追加）。
# ダッシュボードが「M/D の情報のまま（M/D H:MM チェック済み）」を出せるようにするため。
# 本番は ~/agent-tools/provider-fetch-jp/run.sh。このファイルはレビュー用の版で、マージ後に本番へ同じ変更を入れる。
# 一休・Yahoo!トラベルは 2026-11-10 に対象から外した。11/02（Yahoo!）・11/05（一休）から自動取得に403が返るため。
# 2社は本人が開いたタブから取り込む（save_tab.js → import_saved.sh、毎朝9:00の定期タスク）。
# 対象外（2026-10-20 本人決定）: KKday・サプライス・ゆめやど・Expedia・Hotels.com。保護の回避はしない。
set -uo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export LANG=ja_JP.UTF-8
REPO="$HOME/projects/jtb-coupon-monitor-daily"
BASE="$HOME/agent-tools/provider-fetch-jp"
PY=/opt/homebrew/bin/python3
VENV_PY="$HOME/agent-tools/coupon-daily/.venv/bin/python"
DATE=$(date +%Y-%m-%d)
LOG="${BASE}/logs/run-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "${LOG}") 2>&1
NO_PUSH=0; [ "${1:-}" = "--no-push" ] && NO_PUSH=1
DIRS="provider_check_data official_source_data codex_audit_queue ikkyu_coupon_data yahoo_travel_coupon_data"
discard() { git checkout -q -- ${DIRS} 2>/dev/null; git clean -fdq -- ${DIRS} 2>/dev/null; }
log() { echo "[$(date +%T)] $*"; }
push_or_exit() {
  if [ "${NO_PUSH}" = 1 ]; then log "--no-push: pushしない（手元に1コミット残る）"; exit 0; fi
  for i in 1 2; do
    if git push -q origin main; then log "成功: push $(git rev-parse --short HEAD)"; exit 0; fi
    if [ "${i}" = 1 ]; then git pull -q --rebase origin main || { log "失敗: rebase"; git rebase --abort 2>/dev/null; exit 1; }; fi
  done
  log "失敗: pushを2回試して失敗（手元にcommitが残っている。次の実行のpushで一緒に送られる）"; exit 1
}

cd "${REPO}" || { log "失敗: リポジトリなし"; exit 1; }
if [ -n "$(git status --porcelain)" ]; then log "停止: ${REPO} に未コミットの変更がある"; exit 1; fi
git pull -q --ff-only origin main || { log "停止: git pull --ff-only 失敗"; exit 1; }

# 前回の観測ハッシュ（変化判定用）
snap() { "${PY}" - <<'PY'
import json,glob
out={}
for f in sorted(glob.glob('official_source_data/*/state.json')):
    try: out[f]=json.load(open(f)).get('observed_hash')
    except Exception: out[f]=None
print(json.dumps(out))
PY
}
BEFORE=$(snap)

# 一休・Yahoo!トラベルは取得しない（2026-11-10。保護は回避しない）。戻すときは run.sh.bak-20261110 を見る
TARGETS="booking"
for id in ${TARGETS}; do
  log "取得: ${id}"
  "${PY}" provider_check_runner.py --provider-id "${id}" --deep 2>&1 | tail -5
  "${PY}" - "${id}" <<'PY'
import json,glob,sys
f=sorted(glob.glob(f'provider_check_data/{sys.argv[1]}/check_*.json'))[-1]; d=json.load(open(f))
print(f"  status={d.get('status')} change={d.get('change_kind')} failed={d.get('failed_source_count')}")
for r in d.get('source_results',[]): print(f"  {r.get('url')} ok={r.get('ok')} http={r.get('status_code')} via={r.get('fetch_method')} chars={r.get('text_chars')} {r.get('error') or ''}")
PY
done

# provider_check_data/run-latest.json は Actions の全社実行の要約なので、1〜2社だけの結果で上書きしない
git checkout -q -- provider_check_data/run-latest.json 2>/dev/null || true
OUTSIDE=$(git status --porcelain | grep -v -E " (provider_check_data|official_source_data|codex_audit_queue|ikkyu_coupon_data|yahoo_travel_coupon_data)/" || true)
if [ -n "${OUTSIDE}" ]; then log "停止: 対象外のファイルが変わった → ${OUTSIDE}"; discard; git checkout -q -- . ; exit 1; fi

AFTER=$(snap)
NEWQ=$(git status --porcelain -- codex_audit_queue | grep -c '^??' || true)
# 一休・Yahoo!トラベルは monitor が保存先へ直接書くので、保存先の変化も「中身の変化」に数える
DATA_CHANGED=$(git status --porcelain -- ikkyu_coupon_data yahoo_travel_coupon_data | wc -l | tr -d ' ')
if [ "${BEFORE}" = "${AFTER}" ] && [ "${NEWQ}" = "0" ] && [ "${DATA_CHANGED}" = "0" ]; then
  # 中身は同じ。取得に成功した会社のチェック時刻の記録だけを残し、ほかは元に戻す。
  # 残すもの: provider_check_data/<id>/latest.json、その回の check_*.json、official_source_data/<id>/state.json
  # 残さないもの: run-latest.json（上で戻し済み）、codex_audit_queue、一休・Yahoo!の保存先、そのほかの差分
  # 中身が同じなので pytest は通さない（記録するのはチェック時刻のJSONだけ）。JSONとして読めるかだけ見る。
  KEEP=""
  for id in ${TARGETS}; do
    if "${PY}" - "${id}" <<'PY'
import json,sys
d=json.load(open(f'provider_check_data/{sys.argv[1]}/latest.json'))
sys.exit(0 if d.get('official_fetched_at') and d.get('status') != 'error' else 1)
PY
    then
      KEEP="${KEEP} provider_check_data/${id}/latest.json official_source_data/${id}/state.json"
      KEEP="${KEEP} $(git ls-files --others --exclude-standard -- "provider_check_data/${id}/check_*.json" | tr '\n' ' ')"
    fi
  done
  if [ -z "${KEEP// /}" ]; then
    log "中身の変化なし・取得成功なし。commitせず元に戻す"; discard; exit 0
  fi
  for f in ${KEEP}; do
    [ -e "${f}" ] || continue
    "${PY}" -c 'import json,sys; json.load(open(sys.argv[1]))' "${f}" || { log "失敗: JSONが読めない ${f}"; git reset -q; discard; exit 1; }
    git add -- "${f}"
  done
  discard   # index に入れた記録は残り、それ以外の差分だけ元に戻る
  if git diff --cached --quiet; then log "中身の変化なし・チェック時刻の差分もなし"; exit 0; fi
  git -c user.name="provider-fetch-jp" -c user.email="provider-fetch-jp@local" commit -q -m "🇯🇵 公式ページのチェック時刻を記録 ${DATE}（${TARGETS}・中身の変化なし）" || { log "失敗: commit"; git reset -q; discard; exit 1; }
  log "中身の変化なし。チェック時刻だけcommit $(git rev-parse --short HEAD)"
  push_or_exit
fi
if ! "${VENV_PY}" -m pytest -q -p no:cacheprovider > "${BASE}/logs/pytest-last.log" 2>&1; then
  log "失敗: pytest（logs/pytest-last.log）。commitしない"; discard; exit 1
fi
git add -- ${DIRS}
if git diff --cached --quiet; then log "変更なし"; exit 0; fi
git -c user.name="provider-fetch-jp" -c user.email="provider-fetch-jp@local" commit -q -m "🇯🇵 日本から取得した公式ページを更新 ${DATE}（${TARGETS}）" || { log "失敗: commit"; git reset -q; discard; exit 1; }
log "commit $(git rev-parse --short HEAD)"
push_or_exit
