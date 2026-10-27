"""保存日と確認日時を分ける鮮度表示の受入テスト。外部取得は行わない。"""
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import codex_audit_runner as audit
import generate_dashboard as dashboard
import official_deal_monitor as monitor
import provider_check_runner as runner

NOW = datetime(2026, 10, 27, 21, 0, tzinfo=monitor.JST)
OLD = '2026-09-29'
TEXT = '公式のクーポン条件を記載した本文。' * 30
PROVIDER = {
    'id': 'sample', 'label': 'サンプル', 'data_dir': 'official_coupon_data/sample',
    'official_sources': [{'url': 'https://example.com/deals'}],
    'coverage_status': 'official_codex', 'cadence_days': 1,
}
SOURCE = {'url': 'https://example.com/deals', 'text': TEXT, 'ok': True, 'error': ''}
HASH = hashlib.sha256(monitor.normalize_text(f"SOURCE {SOURCE['url']} {TEXT}").encode()).hexdigest()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False))


@pytest.fixture
def sandbox(tmp_path):
    previous_monitor, previous_audit = monitor.ROOT, audit.ROOT
    monitor.configure_root(tmp_path)
    audit.configure_root(tmp_path)
    write_json(tmp_path / PROVIDER['data_dir'] / f'coupons_{OLD}.json', [
        {'id': 'one', 'title': '保存したクーポン', 'source_type': 'official_codex_audit', 'status': '要確認'}])
    monitor.save_state('sample', {'processed_hash': HASH, 'last_audit_status': 'processed',
                                  'confirmed_at': '2026-10-20T07:00:00+09:00'})
    with patch.multiple(dashboard, ROOT=tmp_path, CHECK_STATUS_ROOT=tmp_path / 'provider_check_data',
                        AUDIT_QUEUE_ROOT=tmp_path / 'codex_audit_queue',
                        AUDIT_RESULT_ROOT=tmp_path / 'codex_audit_results',
                        AUDIT_LEDGER=tmp_path / 'codex_audit_data/applied-candidates.json'), \
         patch.object(runner, 'STATUS_ROOT', tmp_path / 'provider_check_data'), \
         patch.object(monitor, 'now_jst', return_value=NOW), \
         patch.object(runner, 'now_jst', return_value=NOW), \
         patch.object(dashboard, 'datetime', wraps=datetime) as clock:
        clock.now.return_value = NOW
        yield tmp_path
    monitor.configure_root(previous_monitor)
    audit.configure_root(previous_audit)


def run_check(results):
    with patch.object(monitor, 'fetch_official_source', side_effect=results), \
         patch.object(runner, 'deep_dive_allowed', return_value=True):
        return runner.run_provider(PROVIDER, timeout=10, max_urls=0)


def test_today_unchanged_keeps_saved_file_and_displays_confirmation(sandbox):
    before = list((sandbox / PROVIDER['data_dir']).glob('*.json'))
    payload = run_check([SOURCE])
    assert payload['confirmed_at'] == NOW.isoformat()
    assert payload['fetch_status'] == 'success'
    assert payload['freshness_status'] == 'fresh'
    assert payload['data_date'] == OLD
    assert list((sandbox / PROVIDER['data_dir']).glob('*.json')) == before
    display = dashboard.build_provider_payload(PROVIDER)
    chip = dashboard.freshness_chip(display, NOW.date())
    assert chip['text'].startswith('本日確認・内容変更なし')
    assert '要確認 1件' in chip['text']
    assert display['summary']['内容を保存した日'] == OLD


@pytest.mark.parametrize('case', ['partial', 'zero', 'short', 'pending', 'held', 'no_coupons', 'pending_same', 'held_same'])
def test_incomplete_checks_never_confirm(sandbox, case):
    state = monitor.load_state('sample')
    if case in {'pending', 'held'}:
        state.update(processed_hash='different', queued_hash=HASH,
                     queued_candidate_id=f'sample-{HASH[:16]}',
                     last_audit_status='held' if case == 'held' else 'pending')
        write_json(monitor._candidate_path('sample', f'sample-{HASH[:16]}'), {})
    elif case.endswith('_same'):
        state['last_audit_status'] = case.split('_')[0]
    monitor.save_state('sample', state)
    results = [SOURCE]
    if case == 'partial':
        # 点検対象外の入口が失敗しても、全対象成功とはしない。
        results = [SOURCE, {'url': 'https://example.com/hub', 'ok': False,
                            'text': '', 'include_in_audit': False, 'error': 'timeout'}]
    if case == 'zero':
        results = []
    if case == 'short':
        results = [{'url': SOURCE['url'], 'ok': False, 'text': '', 'error': 'page text too short'}]
    if case == 'no_coupons':
        write_json(sandbox / PROVIDER['data_dir'] / f'coupons_{OLD}.json', [])
    with patch.dict(PROVIDER, {'official_sources': [{'url': r['url']} for r in results]}):
        payload = run_check(results)
    assert not payload['confirmed_at']
    assert payload['freshness_status'] != 'fresh'
    assert monitor.load_state('sample')['confirmed_at'] == state['confirmed_at']
    display = dashboard.build_provider_payload(PROVIDER)
    chip = dashboard.freshness_chip(display, NOW.date())
    assert '内容変更なし' not in chip['text']
    assert chip['state'] != 'fresh'
    if case == 'partial':
        assert payload['fetch_status'] == 'partial_failure'
        assert '一部失敗 1件' in chip['text']


def test_all_providers_stopped_are_stale_even_if_saved_status_is_fresh(sandbox):
    run_check([SOURCE])
    future = NOW + timedelta(days=4)
    with patch.object(dashboard, 'datetime', wraps=datetime) as clock:
        clock.now.return_value = future
        providers = [dashboard.build_provider_payload(PROVIDER) for _ in range(4)]
        for provider in providers:
            assert provider['freshness_status'] == 'stale'
            chip = dashboard.freshness_chip(provider, datetime.fromisoformat(OLD).date())
            assert chip['state'] == 'stale'
            assert '30時間超過' in chip['text']


@pytest.mark.parametrize('hours,expected', [(29.999, 'fresh'), (30, 'fresh'), (30.001, 'stale'), (-1, 'unknown')])
def test_thirty_hour_boundary(hours, expected):
    assert runner.confirmed_freshness((NOW - timedelta(hours=hours)).isoformat(), now=NOW) == expected


@pytest.mark.parametrize('value', ['', 'invalid', '2026-10-27T20:00:00'])
def test_invalid_confirmation(value):
    assert runner.confirmed_freshness(value, now=NOW) == 'unknown'


def test_jst_date_boundary(sandbox):
    provider = {'id': 'sample', 'label': 'サンプル', 'confirmed_at': '2026-10-27T15:01:00+00:00'}
    with patch.object(dashboard, 'datetime', wraps=datetime) as clock:
        clock.now.return_value = datetime(2026, 10, 28, 0, 2, tzinfo=monitor.JST)
        assert dashboard.freshness_chip(provider, None)['text'] == '本日確認・内容変更なし'
        provider['confirmed_at'] = '2026-10-27T14:59:00+00:00'
        assert dashboard.freshness_chip(provider, None)['text'] == '10/27 23:59確認・内容変更なし'


def test_same_completed_candidate_clears_old_pending_without_changing_history(sandbox):
    monitor.save_state('sample', {})
    payload = run_check([SOURCE])
    candidate = json.loads((sandbox / payload['audit_candidate_path']).read_text())
    check_file = sandbox / 'provider_check_data/sample/latest.json'
    before = check_file.read_bytes()
    assert dashboard.load_pending_audit_counts() == {'sample': 1}
    audit._record_state(candidate, status='processed')
    state = monitor.load_state('sample')
    assert state['audit_completed_at']
    assert state['audit_completed_candidate_id'] == candidate['candidate_id']
    assert dashboard.load_pending_audit_counts() == {}
    display = dashboard.build_provider_payload(PROVIDER, dashboard.load_pending_audit_counts())
    assert display['check_status']['status'] == 'audit_processed'
    assert display['pending_audit_count'] == 0
    assert display['ai_label'] == '監査済み'
    assert check_file.read_bytes() == before
    assert not dashboard.audit_completed_for_check({'audit_candidate_id': 'another'}, state)
    different = {**payload, 'audit_candidate_id': 'another'}
    write_json(check_file, different)
    assert dashboard.build_provider_payload(PROVIDER)['pending_audit_count'] == 1


def test_legacy_confirmation_absent_keeps_old_display(sandbox):
    write_json(sandbox / 'provider_check_data/sample/latest.json', {
        'data_date': OLD, 'check_type': 'official_page_candidate', 'status': 'unchanged'})
    provider = dashboard.build_provider_payload(PROVIDER)
    assert dashboard.freshness_chip(provider, NOW.date())['text'].startswith('9/29 の情報のまま')


def test_real_short_body_is_not_success():
    with patch.object(monitor, '_requests_fetch', return_value=('<p>短い本文</p>', 200)):
        result = monitor.fetch_official_source({'url': SOURCE['url'], 'fetch_method': 'html'})
    assert not result['ok']
    assert 'too short' in result['error']


def test_old_audit_completion_fields_also_resolve_same_candidate(sandbox):
    old_state = {'last_audit_status': 'processed', 'last_audit_candidate_id': 'same',
                 'last_processed_at': NOW.isoformat()}
    assert dashboard.audit_completed_for_check({'audit_candidate_id': 'same'}, old_state)
    assert not dashboard.audit_completed_for_check({'audit_candidate_id': 'different'}, old_state)
    assert not dashboard.audit_completed_for_check({'audit_candidate_id': 'same'},
                                                   {**old_state, 'queued_candidate_id': 'new'})
