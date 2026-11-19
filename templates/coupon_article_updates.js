function renderCouponArticleUpdates(container) {
  const sites = DATA.coupon_articles?.sites || [];
  const esc = escapeHtml;
  const day = value => (value || '').slice(0, 10);
  const stamp = value => value ? value.slice(0, 16).replace('T', ' ') : '—';
  const today = new Intl.DateTimeFormat('sv-SE', {timeZone:'Asia/Tokyo'}).format(new Date());
  const firstDay = new Date(Date.parse(today + 'T00:00:00Z') - 6 * 86400000).toISOString().slice(0,10);
  const records = sites.flatMap(s=>s.articles.map(a=>({...a, site:s.site, name:s.name,
    draftsChecked:s.drafts_checked, stale:s.status!=='取得済み'})));
  const recent = a => day(a.history.changed_at);
  const latest = a => [a.modified,a.draft_at || ''].sort().at(-1);
  function draftSortDate(a) {
    if (a.draft_at) return a.draft_at;
    // 時刻がない作業記録は、並べ替えのときだけ同じ日の午前・午後の先頭に置く。
    return (a.job?.draft_run || '').replace(' 朝', 'T00:00').replace(' 夕', 'T12:00');
  }
  const draftToday = a => day(a.draft_at) === today || day(a.job?.draft_run) === today;
  const publishedToday = a => day(a.job?.published_run) === today;
  let sortField = '', sortDirection = 'desc';
  function sortHeader(field, label) {
    return `<th scope="col"><button type="button" data-sort-field="${field}">${label} <span class="sort-arrow" aria-hidden="true">↕</span></button></th>`;
  }
  const safeLink = a => /^https?:\/\//i.test(a.url) ? `<a href="${esc(a.url)}" target="_blank" rel="noopener noreferrer">${esc(a.title)}</a>` : esc(a.title);
  function siteCard(s) {
    const rows = s.articles;
    const confirmed = rows.filter(a=>recent(a)===today).length;
    const current = s.status==='取得済み' && day(s.checked_at)===today;
    const drafts = s.drafts_checked && current ? rows.filter(draftToday).length+'本' : '未確認';
    const job = rows.filter(publishedToday).length;
    const ran = rows.some(a=>day(a.job?.last_run)===today);

    return `<div class="update-site"><h3>${esc(s.name)}</h3>
      <p><b>${current?confirmed+'本':'—'}</b> 今日クーポン条件の変更を確認</p>
      <p>朝夕の作業で公開更新　${current && ran?job+'本':'未確認'} ／ 今日の下書き保存　${drafts}</p>
      <small>${s.checked_at?`${rows.length}記事 · ${esc(stamp(s.checked_at))} 確認`:'未取得'}${s.status!=='取得済み'?' · 再取得できず':''}</small></div>`;
  }
  container.innerHTML = `<section class="deal-board update-board"><div class="deal-heading"><h2>クーポン記事の更新</h2></div>
    <div class="update-sites">${sites.map(siteCard).join('')}</div>
    <p class="deal-hint">公開本文と直近8版までの保存履歴で、割引・利用金額、予約・利用期間、配布枚数、コードを比べています。日時は日本時間です。</p>
    <div class="deal-filters"><label>サイト<select aria-label="サイト" data-site><option value="">両サイト</option>${sites.map(s=>`<option value="${esc(s.site)}">${esc(s.name)}</option>`).join('')}</select></label>
    <label>表示する記事<select aria-label="表示する記事" data-state><option value="">すべて</option><option value="today">今日クーポン条件を変更</option>
    <option value="week">直近7日にクーポン条件を変更</option><option value="run">今日の朝夕の作業で公開更新</option><option value="draft">今日更新用の下書きを保存</option>
    <option value="newer">公開記事より新しい下書きあり</option><option value="unknown">条件の変更日が未確認</option></select></label>
    <label>記事名<input type="search" aria-label="記事名" placeholder="旅行会社名・記事名" data-query></label>
    <button type="button" data-reset>条件を戻す</button></div>
    <div class="deal-list"><div class="deal-results"><strong aria-live="polite"></strong></div>
    <table class="update-table" aria-label="クーポン記事の更新"><thead><tr><th scope="col">記事名</th>
      ${sortHeader('changed','クーポン条件の変更')}${sortHeader('modified','公開記事の最終編集')}${sortHeader('draft','下書きの最終保存')}
    </tr></thead><tbody class="update-list"></tbody></table></div>
    <p class="deal-hint">対象は朝夕の更新対象、登録記事と、題名に「クーポン」またはURL末尾の名前に「coupon」を含む公開記事です。下書きは元記事を特定できたものを表示します。</p></section>`;
  const $ = selector => container.querySelector(selector);
  const normalize = v => v.normalize('NFKC').toLowerCase();
  function show(a) {
    const change = a.history;
    const note = change.changed_at ? [change.fields.join('・'), change.latest_check].filter(Boolean).join(' ／ ') : change.result;
    const draft = a.draft_at ? stamp(a.draft_at) : (a.job?.draft_run || (a.draftsChecked?'対応する記録なし':'未確認'));
    const job = a.job || {};
    return `<tr class="update-row"><td><span class="deal-meta">${esc(a.name)}${a.stale?' · 前回の記録':''}</span>${safeLink(a)}</td>
      <td><small>クーポン条件の変更</small><b>${change.changed_at?esc(stamp(change.changed_at)):'未確認'}</b><span class="deal-meta">${esc(note)}</span></td>
      <td><small>公開記事の最終編集</small>${esc(stamp(a.modified))}</td>
      <td><small>下書きの最終保存</small>${esc(draft)}</td></tr>
      ${job.last_run?`<tr><td colspan="4" class="update-run">朝夕の作業　${esc(job.last_run)} · ${esc(job.run_result)}${job.published_run && job.published_run !== job.last_run?'　／　直近の公開更新 '+esc(job.published_run):''}${job.draft_run?'　／　下書き保存 '+esc(job.draft_run):''}</td></tr>`:''}`;
  }
  function draw() {
    const site=$('[data-site]').value, filter=$('[data-state]').value, q=normalize($('[data-query]').value.trim());
    const field = sortField || (['today', 'week'].includes(filter) ? 'changed' : '');
    const byDate = {
      modified: a => a.modified,
      draft: draftSortDate,
      changed: a => a.history.changed_at
    };
    const sortDate = byDate[field] || latest;
    const matches=records.filter(a=>{
      if ((site && a.site!==site) || (q && !normalize(a.title).includes(q))) return false;
      switch(filter) {
        case 'today': return recent(a)===today;
        case 'week': return recent(a)>=firstDay && recent(a)<=today;
        case 'draft': return draftToday(a);
        case 'run': return publishedToday(a);
        case 'newer': return a.draft_at && a.draft_at>a.modified;
        case 'unknown': return !a.history.changed_at;
        default: return true;
      }
    }).sort((a, b) => {
      const left = sortDate(a) || '', right = sortDate(b) || '';
      if (Boolean(left) !== Boolean(right)) return left ? -1 : 1;
      const order = sortDirection === 'asc' ? left.localeCompare(right) : right.localeCompare(left);
      return order || a.title.localeCompare(b.title, 'ja');
    });
    $('.deal-results strong').textContent=matches.length+'記事';
    $('.update-list').innerHTML=matches.map(show).join('') || '<tr><td colspan="4" class="empty">条件に合う記事はありません。</td></tr>';
    for (const button of sortButtons) {
      const active = button.dataset.sortField === field;
      const newest = active && sortDirection === 'desc';
      if (active) button.parentElement.setAttribute('aria-sort', newest ? 'descending' : 'ascending');
      else button.parentElement.removeAttribute('aria-sort');
      button.querySelector('.sort-arrow').textContent = active ? (newest ? '↓' : '↑') : '↕';
      button.title = newest ? '古い順に並べ替え' : '新しい順に並べ替え';
    }
  }
  const sortButtons = container.querySelectorAll('[data-sort-field]');
  for (const button of sortButtons) {
    button.onclick = () => {
      sortField = button.dataset.sortField;
      sortDirection = button.parentElement.getAttribute('aria-sort') === 'descending' ? 'asc' : 'desc';
      draw();
    };
  }
  container.querySelectorAll('select').forEach(el=>el.onchange=draw);
  $('[data-query]').oninput=draw;
  $('[data-reset]').onclick=()=>{
    container.querySelectorAll('select,input').forEach(el=>el.value='');
    sortField = ''; sortDirection = 'desc';
    draw();
  };
  draw();
}
