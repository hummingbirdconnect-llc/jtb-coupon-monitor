function renderDealCatalog(container) {
  const catalog = DATA.deals || {rows: [], checks: []};
  const companies = new Map(DATA.providers.map(p => [p.id, p.label]));
  const checks = new Map();
  catalog.checks.forEach(c => {
    if (!checks.has(c.provider)) checks.set(c.provider, []);
    checks.get(c.provider).push(c);
  });
  const updates = new Map((DATA.coupon_articles?.sites || []).flatMap(s=>s.articles.map(a=>[a.url,a.history.changed_at?.slice(0,10)])));
  const esc = escapeHtml;
  function link(url, label) {
    return /^https?:\/\//i.test(url || '') ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>` : esc(label);
  }
  const options = values => values.map(([value,label]) => `<option value="${esc(value)}">${esc(label)}</option>`).join('');
  const select = (key,label,all,items) => `<label class="${['kind','source','stock'].includes(key) ? 'deal-extra' : ''}">${label}<select aria-label="${esc(label)}" data-field="${key}">${options([['',all], ...items])}</select></label>`;
  const distinct = values => [...new Set(values)].sort().map(v => [v,v]);
  const rows = [...catalog.rows].sort((a,b) => (b.date || '').localeCompare(a.date || '') || a.provider.localeCompare(b.provider) || a.title.localeCompare(b.title,'ja'));
  container.innerHTML = `<section class="deal-board">
    <div class="deal-heading"><h2>お得情報と掲載状況</h2><a href="#changes">新着・終了の速報を見る</a></div>
    <div class="deal-metrics"></div>
    <div class="deal-filters">
    <label>案件を検索<input type="search" data-field="query" placeholder="特典名・割引額・会社名"></label>
    ${select('company','旅行会社','すべての会社',[...companies])}
    ${select('kind','特典の種類','すべての種類',distinct(rows.map(r=>r.kind)))}
    ${select('source','情報源','すべての情報源',distinct(rows.flatMap(r=>r.sources)))}
    ${select('x','X投稿','すべての状況',[['posted','投稿済み'],['draft','下書きのみ'],['unknown','記録なし']])}
    ${select('article','公開記事','すべての状況',[['found','掲載を確認'],['missing','本文に一致なし'],['unknown','未照合・一部未取得']])}
    ${select('stock','配布状況','すべての状況',distinct(rows.map(r=>r.stock)))}
    <button type="button" data-more aria-expanded="false">ほかの条件を表示</button><button type="button" data-reset>条件を戻す</button></div>
    <div class="deal-list"><div class="deal-results"><strong aria-live="polite"></strong><div class="deal-pages"><button data-prev>前へ</button><span></span><button data-next>次へ</button></div></div><div class="deal-body"></div></div>
    <p class="deal-hint">案件を開くと、出典と確認日を見られます。記事の掲載は登録した公開記事の名称・コードで照合しています。</p></section>`;
  const $ = s => container.querySelector(s);
  const fields = [...container.querySelectorAll('[data-field]')];
  function xState(r) {
    if (r.x.some(x=>x.stage === 'posted')) return 'posted';
    return r.x.length ? 'draft' : 'unknown';
  }
  function articleState(r) {
    if (r.articles.length) return 'found';
    const list = checks.get(r.provider) || [];
    return list.length && list.every(c=>c.status === '照合済み' && c.date >= r.date) ? 'missing' : 'unknown';
  }
  const xLabels = {posted:'投稿済み',draft:'下書きのみ',unknown:'記録なし'};
  const artLabels = {found:'掲載を確認',missing:'本文に一致なし',unknown:'照合未完了'};
  const pill = (value,label,done) => `<span class="deal-pill ${value===done?'done':value==='draft'?'draft':''}">${label}</span>`;
  function detail(r) {
    const links = (checks.get(r.provider) || []).map(c => link(c.url,c.site) + '（' + esc(c.date) + ' ' + esc(c.status) + '）');
    const articles = r.articles.map(a=>link(a.url,a.site) + '（' + esc(a.date) + ' ' + esc(a.basis) + '）' + (updates.get(a.url)?' · 記事のクーポン条件変更 '+esc(updates.get(a.url)):''));
    const x = r.x.map(x=>(x.stage==='posted'?link(x.url,x.site+'の投稿'):esc(x.site+' 下書き'))+'（'+esc(x.date)+'）');
    return `<p class="deal-meta">案件ID　<code>${esc(r.key)}</code></p><p><b>出典</b>　${link(r.url, r.url?'特典の掲載元':'URL未登録')}　${esc(r.sources.join('・'))}</p>
      ${r.ai_date?`<p><b>AIの発見</b>　${esc(r.ai_date)}　${link(r.ai_url,'発見元')}　${esc(r.ai_evidence)}</p>`:''}
      ${r.period?`<p><b>対象期間</b>　${esc(r.period)}</p>`:''}
      <p><b>X投稿</b>　${x.join(' ／ ') || 'この案件に結び付く投稿・下書きの記録はありません。'}</p>
      <p><b>公開記事</b>　${articles.join(' ／ ') || (links.length?'一致を確認できた記事はありません。':'照合対象の記事が登録されていません。')}</p>
      ${links.length?`<p class="deal-meta">照合対象　${links.join(' ／ ')}</p>`:''}`;
  }
  function card(r,i) {
    const xs=xState(r), as=articleState(r);
    return `<details class="deal-row" data-index="${i}"><summary>
      <span class="deal-who">${esc(companies.get(r.provider))}<small class="deal-sub">${esc(r.kind)}</small></span>
      <span class="deal-name"><span class="deal-title">${esc(r.title)}</span><small class="deal-sub">${esc([r.discount,r.scope,r.stock && '取得時：'+r.stock,r.sources.join('・'),r.date].filter(Boolean).join('　·　'))}</small></span>
      <span class="deal-channel">X　${pill(xs,xLabels[xs],'posted')}</span>
      <span class="deal-channel">記事　${pill(as,artLabels[as],'found')}</span>
      </summary><div class="deal-details"></div></details>`;
  }
  let page=0;
  const size=25;
  function draw() {
    const f=Object.fromEntries(fields.map(el=>[el.dataset.field,el.value]));
    const query=f.query.normalize('NFKC').trim().toLowerCase();
    const filtered=rows.filter(r=>(!f.company || r.provider===f.company) && (!f.kind || r.kind===f.kind)
      && (!f.source || r.sources.includes(f.source)) && (!f.x || xState(r)===f.x)
      && (!f.article || articleState(r)===f.article) && (!f.stock || r.stock===f.stock)
      && (!query || [r.title,r.discount,companies.get(r.provider)].join(' ').normalize('NFKC').toLowerCase().includes(query)));
    const metrics=[['収集した案件',filtered.length],['AI収集を含む',filtered.filter(r=>r.sources.includes('AI収集')).length],
      ['X投稿済み',filtered.filter(r=>xState(r)==='posted').length],['記事掲載を確認',filtered.filter(r=>articleState(r)==='found').length]];
    $('.deal-metrics').innerHTML=metrics.map(([l,n])=>`<div class="deal-metric"><span>${l}</span><strong>${n.toLocaleString()}</strong></div>`).join('');
    const pages=Math.ceil(filtered.length/size);
    $('.deal-results strong').textContent=filtered.length.toLocaleString()+'件';
    $('.deal-pages span').textContent=pages?(page+1)+' / '+pages:'0 / 0';
    $('[data-prev]').disabled=!page;
    $('[data-next]').disabled=page+1>=pages;
    const visible=filtered.slice(page*size,(page+1)*size);
    $('.deal-body').innerHTML=visible.map(card).join('') || '<p class="empty">条件に合う案件はありません。</p>';
    container.querySelectorAll('.deal-row').forEach(el=>el.addEventListener('toggle',()=>{
      if(el.open) el.lastElementChild.innerHTML=detail(visible[Number(el.dataset.index)]);
    }));
  }
  fields.forEach(el=>el.addEventListener(el.type==='search'?'input':'change',()=>{page=0;draw();}));
  $('[data-more]').onclick=()=>{
    const open=$('.deal-filters').classList.toggle('expanded');
    $('[data-more]').setAttribute('aria-expanded', String(open));
    $('[data-more]').textContent=open?'ほかの条件を閉じる':'ほかの条件を表示';
  };
  $('[data-reset]').onclick=()=>{fields.forEach(el=>el.value='');page=0;draw();};
  $('[data-prev]').onclick=()=>{page--;draw();};
  $('[data-next]').onclick=()=>{page++;draw();};
  draw();
}
