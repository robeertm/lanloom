/* Lanloom — nightly reports: every morning a short report about the night, with an archive and points to tick off.

   Data: GET /api/reports (list), GET /api/reports/<day> (one report with tick marks), GET /api/reports/preview (not saved),
   POST /api/reports/<day>/read, PUT /api/reports/<day>/items {item, done}. Settings: GET/PUT /api/reports/settings,
   POST /api/reports/test {channel}. The report is language-neutral (keys + variables); this file renders it in the page
   language and the report's time zone. Variables: *_at time, *_dur seconds, *_bytes bytes, *_cc country codes, *_key text keys. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const CHANNELS = ['email', 'webhook', 'ntfy', 'chatbot'];
  const SUFFIX = /_(at|dur|bytes|cc|key)$/;
  const ICONS = {
    internet: '<path d="M3 12h18M12 3c2.6 2.8 3.9 5.8 3.9 9s-1.3 6.2-3.9 9c-2.6-2.8-3.9-5.8-3.9-9S9.4 5.8 12 3z"/><circle cx="12" cy="12" r="9"/>',
    devices: '<rect x="3" y="4" width="13" height="10" rx="1.5"/><rect x="15" y="9" width="6" height="11" rx="1.5"/><path d="M7 18h6"/>',
    connections: '<path d="M8 12h8"/><rect x="2.5" y="8" width="6" height="8" rx="2"/><rect x="15.5" y="8" width="6" height="8" rx="2"/>',
    logins: '<rect x="4" y="10" width="16" height="10" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
    paths: '<circle cx="6" cy="18" r="2.5"/><circle cx="18" cy="6" r="2.5"/><path d="M8 16.5C11 15 9.5 9 16 7.5"/>',
    controller: '<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2.2"/><circle cx="10" cy="17" r="2.2"/>',
    certificates: '<path d="M12 3l7 3v5c0 4.4-3 8.3-7 10-4-1.7-7-5.6-7-10V6z"/><path d="M9 12l2 2 4-4"/>',
    family: '<rect x="3" y="4" width="18" height="6" rx="1.5"/><rect x="3" y="14" width="18" height="6" rx="1.5"/><path d="M7 7h.01M7 17h.01"/>',
    lanloom: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/><path d="M10 6.5h4.5v7.5"/>',
    other: '<circle cx="12" cy="12" r="8"/><path d="M12 8v5M12 16h.01"/>'
  };
  const svg = (inner, size) => '<svg width="' + (size || 18) + '" height="' + (size || 18) + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' + inner + '</svg>';
  LL.NAV_REPORTS_ICON = svg('<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/><path d="M15 3.5l.6 1.4 1.4.6-1.4.6-.6 1.4-.6-1.4-1.4-.6 1.4-.6z"/>');

  // ------------------------------------------------------------------ formatting in the report's time zone
  const locale = () => LL.locale();
  function parts(ts, tz, opt) {
    const out = {};
    try { new Intl.DateTimeFormat(locale(), Object.assign({ timeZone: tz }, opt)).formatToParts(new Date(ts * 1000)).forEach(p => { out[p.type] = p.value; }); }
    catch (e) { new Intl.DateTimeFormat(locale(), opt).formatToParts(new Date(ts * 1000)).forEach(p => { out[p.type] = p.value; }); }
    return out;
  }
  function ymd(ts, tz) {
    const p = parts(ts, tz, { year: 'numeric', month: 'numeric', day: 'numeric' });
    return p.year + '-' + String(p.month).padStart(2, '0') + '-' + String(p.day).padStart(2, '0');
  }
  function fmtTime(ts, tz, day) {
    if (!ts) return '–';
    const p = parts(ts, tz, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23', weekday: 'short', day: 'numeric', month: 'short' });
    const hm = String(p.hour).replace(/^24$/, '00') + ':' + p.minute;
    if (!day || ymd(ts, tz) === day) return hm;
    const wd = String(p.weekday || '').replace(/\.$/, '');
    return LL.lang === 'de' ? wd + ' ' + p.day + '. ' + String(p.month || '').replace(/\.$/, '') + ' ' + hm : wd + ' ' + p.day + ' ' + p.month + ' ' + hm;
  }
  function fmtDay(day, long) {
    const d = new Date(day + 'T12:00:00Z');
    const opt = long ? { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC' } : { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' };
    try { return new Intl.DateTimeFormat(locale(), opt).format(d).replace(/,(?= \d)/, ''); } catch (e) { return day; }
  }
  function fmtDur(s) {
    s = Math.max(0, Math.round(Number(s) || 0));
    if (s < 60) return t('report.dur.s', { n: s });
    if (s < 3600) return t('report.dur.m', { n: Math.round(s / 60) });
    if (s < 86400) { const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60); return mm ? t('report.dur.hm', { h: hh, m: mm }) : t('report.dur.h', { h: hh }); }
    return t('report.dur.dh', { d: Math.floor(s / 86400), h: Math.floor((s % 86400) / 3600) });
  }
  function values(vars, rep) {
    const out = {};
    Object.entries(vars || {}).forEach(([k, v]) => {
      const m = SUFFIX.exec(k), base = m ? k.slice(0, -m[0].length) : k;
      if (!m) out[base] = typeof v === 'number' && !Number.isInteger(v) ? LL.fmtNum(v) : v;
      else if (m[1] === 'at') out[base] = fmtTime(v, rep.tz, rep.day);
      else if (m[1] === 'dur') out[base] = fmtDur(v);
      else if (m[1] === 'bytes') out[base] = LL.fmtBytes(v);
      else if (m[1] === 'cc') out[base] = String(v || '').split(',').filter(Boolean).map(cc => (LL.countryName ? LL.countryName(cc) : cc)).join(', ') || '–';
      else out[base] = t(String(v));
    });
    return out;
  }
  function line(entry, rep) {
    const main = t(entry.key, values(entry.vars, rep));
    const parts = (entry.parts || []).map(p => line(p, rep));
    return [main].concat(parts).join(' · ');
  }
  function headline(rep) {
    const n = rep.points || 0;
    return !n ? t('report.head.calm') : t('report.head', { n });
  }
  LL.reportHeadline = headline;

  // ------------------------------------------------------------------ unread mark (tab badge)
  LL.setReportsUnread = function (n) {
    if (LL.state) LL.state.reports_unread = n;
    document.querySelectorAll('.tab--reports').forEach(tab => {
      let b = tab.querySelector('.tab__badge');
      if (!n) { if (b) b.remove(); tab.removeAttribute('title'); return; }
      if (!b) { b = h('span', { class: 'tab__badge' }); tab.append(b); }
      b.textContent = n > 9 ? '9+' : String(n);
      tab.title = t('reports.badge', { n });
    });
  };

  // ------------------------------------------------------------------ view state
  const S = { list: null, rep: null, day: null, preview: false, readTimer: 0, alive: false };

  async function reportsView(app, rest) {
    const fr = LL.frame(app, 'reports');
    S.alive = true;
    S.preview = false;
    const page = h('div', { class: 'page rp-page' });
    fr.main.append(page);
    page.append(h('div', { class: 'rp-loading' }, h('div', { class: 'skeleton' })));
    try { S.list = await api('/api/reports'); }
    catch (e) { page.replaceChildren(h('div', { class: 'msg msg--error' }, LL.errText(e) || t('reports.err.load'))); return fr; }
    const want = rest && rest[0] && /^\d{4}-\d{2}-\d{2}$/.test(rest[0]) ? rest[0] : null;
    S.day = want && S.list.reports.some(r => r.day === want) ? want : (S.list.reports[0] && S.list.reports[0].day) || null;
    S.rep = null;
    if (S.day) { try { S.rep = await api('/api/reports/' + S.day); } catch (e) { S.rep = null; } }
    draw(page);
    return { destroy: () => { S.alive = false; clearTimeout(S.readTimer); fr.destroy(); } };
  }

  function draw(page) {
    const l = S.list;
    const next = l.next_at ? fmtTime(l.next_at, l.tz, ymd(Date.now() / 1000, l.tz)) : '';
    const previewBtn = h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => showPreview(page) }, t('reports.preview'));
    const head = h('div', { class: 'page__head rp-head' },
      h('div', { class: 'rp-head__titles' }, h('h1', {}, t('reports.title')), next ? h('span', { class: 'muted rp-head__next' }, t('reports.next', { when: next })) : null),
      h('div', { class: 'rp-head__tools' }, previewBtn, LL.can('admin') ? h('a', { class: 'btn btn--ghost btn--sm', href: '#/settings/reports' }, t('reports.settings')) : null));
    const body = h('div', { class: 'rp-layout' });
    if (!l.reports.length && !S.preview) {
      body.classList.add('rp-layout--empty');
      body.append(h('div', { class: 'card empty-state rp-empty' }, h('div', { class: 'rp-empty__art' }), h('h2', {}, t('reports.empty.title')),
        h('p', { class: 'muted' }, t('reports.empty.text', { when: next || '—' })), previewBtn.cloneNode(true)));
      body.querySelector('.rp-empty .btn').addEventListener('click', () => showPreview(page));
      body.querySelector('.rp-empty__art').innerHTML = svg('<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/>', 40);
    } else {
      body.append(S.rep ? reportCard(S.rep, page) : h('div', { class: 'card rp-report' }, h('p', { class: 'msg msg--error' }, t('report.err.missing'))));
      body.append(archive(l, page));
    }
    page.replaceChildren(head, body);
    if (S.rep && !S.rep.read && !S.rep.preview && LL.can('operate')) {
      clearTimeout(S.readTimer);
      const day = S.rep.day;
      S.readTimer = setTimeout(async () => {
        if (!S.alive || !S.rep || S.rep.day !== day) return;
        try {
          const r = await api('/api/reports/' + day + '/read', { method: 'POST', body: { read: true } });
          S.rep.read = true;
          const row = S.list.reports.find(x => x.day === day); if (row) row.read = true;
          page.querySelectorAll('.rp-row[data-day="' + day + '"]').forEach(el => el.classList.remove('is-unread'));
          LL.setReportsUnread(r.unread || 0);
        } catch (e) { /* stays unread */ }
      }, 1200);
    }
  }

  async function showPreview(page) {
    let rep;
    try { rep = await api('/api/reports/preview'); } catch (e) { LL.toast(LL.errText(e), 'error'); return; }
    S.preview = true;
    S.rep = rep;
    draw(page);
    page.querySelector('.rp-report').scrollIntoView({ block: 'start', behavior: 'smooth' });
  }

  async function open(page, day) {
    if (!day) return;
    S.preview = false;
    try { S.rep = await api('/api/reports/' + day); S.day = day; } catch (e) { LL.toast(LL.errText(e), 'error'); return; }
    LL.deepLink('#/reports/' + day);                     // the back key returns to the report shown before
    draw(page);
  }

  // ------------------------------------------------------------------ one report
  function reportCard(rep, page) {
    const total = rep.sections.reduce((n, s) => n + s.items.filter(i => i.point).length, 0);
    const done = rep.sections.reduce((n, s) => n + s.items.filter(i => i.point && i.done).length, 0);
    const level = !total ? 'ok' : (done === total ? 'done' : rep.level);
    const progress = h('span', { class: 'rp-headline__progress' }, total ? (done === total ? t('reports.all_done') : t('reports.progress', { done, n: total })) : '');
    const headIcon = h('span', { class: 'rp-headline__icon', 'aria-hidden': 'true' });
    headIcon.innerHTML = level === 'ok' || level === 'done' ? svg('<path d="M5 12.5l4.2 4.2L19 7"/>', 20) : svg('<path d="M12 8v5M12 16.5h.01"/><circle cx="12" cy="12" r="9"/>', 20);
    const cover = t('reports.covers', { from: fmtTime(rep.since, rep.tz, rep.day), to: fmtTime(rep.until, rep.tz, rep.day) });
    const notes = [];
    if (rep.preview) notes.push(h('p', { class: 'rp-note rp-note--preview' }, t('reports.preview.note')));
    if (rep.catch_up && rep.planned) notes.push(h('p', { class: 'rp-note' }, t('reports.catch_up', { at: fmtTime(rep.created, rep.tz, rep.day), planned: fmtTime(rep.planned, rep.tz, rep.day) })));
    if (rep.long) notes.push(h('p', { class: 'rp-note' }, t('reports.long', { from: fmtTime(rep.since, rep.tz, rep.day) })));
    const navBtn = (day, label, dir) => h('button', { class: 'icon-btn rp-nav__btn', type: 'button', disabled: !day, 'aria-label': label, title: label, onclick: () => open(page, day) }, dir);
    const nav = rep.preview
      ? h('div', { class: 'rp-nav' }, h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => open(page, S.day || (S.list.reports[0] || {}).day) }, t('reports.preview.close')))
      : h('div', { class: 'rp-nav' }, navBtn(rep.older, t('reports.older'), '‹'), navBtn(rep.newer, t('reports.newer'), '›'));
    const header = h('header', { class: 'rp-report__head' },
      h('div', { class: 'rp-report__top' },
        h('div', { class: 'rp-report__titles' },
          h('h2', { class: 'rp-date' }, rep.preview ? t('reports.preview.title') : fmtDay(rep.day, true)),
          h('p', { class: 'rp-cover muted' }, cover + ' · ' + t('reports.made', { at: fmtTime(rep.created, rep.tz, rep.day) }))),
        nav),
      notes,
      h('div', { class: 'rp-headline rp-headline--' + level }, headIcon, h('b', { class: 'rp-headline__text' }, headline(rep)), progress),
      LL.aiPlainBlock ? LL.aiPlainBlock(rep) : null);     // AI paragraph "In plain words" (optional; the report below stays the truth)
    const sections = h('div', { class: 'rp-sections' }, rep.sections.map(s => sectionEl(s, rep, page)));
    const foot = deliveryNote(rep);
    return h('article', { class: 'card rp-report', 'aria-label': rep.preview ? t('reports.preview.title') : fmtDay(rep.day, true) }, header, sections, foot);
  }

  function sectionEl(s, rep, page) {
    const icon = h('span', { class: 'rp-sec__icon', 'aria-hidden': 'true' });
    icon.innerHTML = svg(ICONS[s.id] || (s.id.indexOf('family:') === 0 ? ICONS.family : ICONS.other), 18);
    const items = s.items.map(i => i.point ? pointEl(i, rep, page) : h('li', { class: 'rp-item rp-item--note', dataset: { level: i.level } },
      h('span', { class: 'rp-item__dot', 'aria-hidden': 'true' }), h('span', { class: 'rp-item__text' }, line(i, rep)), explainEl(i, rep)));
    return h('section', { class: 'rp-sec', dataset: { level: s.level, section: s.id } },
      h('div', { class: 'rp-sec__head' }, icon, h('h3', { class: 'rp-sec__title' }, t(s.title), s.name ? h('span', { class: 'rp-sec__name' }, ' · ' + s.name) : null),
        h('span', { class: 'rp-sec__sum' }, line(s.summary, rep))),
      items.length ? h('ul', { class: 'rp-items' }, items) : null,
      s.more ? h('p', { class: 'rp-more muted' }, t('reports.more', { n: s.more })) : null);
  }

  function pointEl(i, rep, page) {
    const box = h('input', { type: 'checkbox', class: 'rp-check__input', checked: !!i.done, disabled: !!rep.preview || !LL.can('operate') });
    const when = i.done ? h('small', { class: 'rp-item__when' }, t('reports.done_at', { at: fmtTime(i.done, rep.tz, rep.day) })) : null;
    box.addEventListener('change', async () => {
      box.disabled = true;
      try {
        const fresh = await api('/api/reports/' + rep.day + '/items', { method: 'PUT', body: { item: i.id, done: box.checked } });
        S.rep = fresh;
        const row = S.list.reports.find(x => x.day === rep.day); if (row) row.open = fresh.open;
        const y = window.scrollY;
        draw(page);
        window.scrollTo(0, y);
        const again = page.querySelector('.rp-item--point[data-item="' + CSS.escape(i.id) + '"] input');
        if (again) again.focus({ preventScroll: true });
      } catch (e) { box.checked = !box.checked; box.disabled = false; LL.toast(LL.errText(e), 'error'); }
    });
    return h('li', { class: 'rp-item rp-item--point' + (i.done ? ' is-done' : ''), dataset: { level: i.level, item: i.id } },
      h('label', { class: 'rp-check' }, box, h('span', { class: 'rp-check__box', 'aria-hidden': 'true' }),
        h('span', { class: 'rp-item__text' }, h('span', { class: 'rp-item__line' }, line(i, rep)), when)), explainEl(i, rep));
  }

  function explainEl(i, rep) {          // "Explain" (AI) for a saved report's note; absent without an AI set up
    if (rep.preview || !LL.aiExplainButton) return null;
    return LL.aiExplainButton('finding', { day: rep.day, item: i.id }, { small: true });
  }

  function deliveryNote(rep) {
    const d = rep.delivery || {};
    const chans = Object.keys(d);
    if (!chans.length) return null;
    const ok = chans.filter(c => d[c].ok).map(c => t('reports.channel.' + c));
    const bad = chans.filter(c => !d[c].ok);
    return h('footer', { class: 'rp-report__foot' },
      ok.length ? h('p', { class: 'muted' }, t('reports.delivery.sent', { channels: ok.join(', ') })) : null,
      bad.map(c => h('p', { class: 'msg msg--error' }, t('reports.delivery.failed', { channel: t('reports.channel.' + c), reason: t(d[c].key || 'report.send.error') }))));
  }

  // ------------------------------------------------------------------ archive
  function archive(l, page) {
    const rows = l.reports.map(r => {
      const chip = !r.points ? h('span', { class: 'chip chip--ok' }, t('reports.chip.calm'))
        : (r.open ? h('span', { class: 'chip chip--' + (r.level === 'danger' ? 'danger' : 'warn') }, t('reports.chip.open', { n: r.open }))
          : h('span', { class: 'chip chip--muted' }, CHECK_SMALL(), t('reports.chip.done')));
      const a = h('a', { class: 'rp-row' + (r.day === S.day && !S.preview ? ' is-active' : '') + (r.read ? '' : ' is-unread'), href: '#/reports/' + r.day,
        dataset: { day: r.day }, 'aria-current': r.day === S.day && !S.preview ? 'true' : null, onclick: ev => { ev.preventDefault(); open(page, r.day); } },
        h('span', { class: 'rp-row__dot', 'aria-hidden': 'true' }),
        h('span', { class: 'rp-row__date' }, fmtDay(r.day, false)),
        r.read ? null : h('span', { class: 'rp-row__new' }, t('reports.unread')),
        chip);
      return h('li', {}, a);
    });
    return h('aside', { class: 'card rp-archive', 'aria-label': t('reports.archive') },
      h('div', { class: 'rp-archive__head' }, h('h2', {}, t('reports.archive')), h('span', { class: 'muted' }, t('reports.archive.keep', { n: l.keep_days }))),
      h('ul', { class: 'rp-list' }, rows));
  }
  function CHECK_SMALL() { const s = h('span', { class: 'rp-chip-check', 'aria-hidden': 'true' }); s.innerHTML = svg('<path d="M5 12.5l4.2 4.2L19 7"/>', 12); return s; }

  // ------------------------------------------------------------------ settings card (shown in Settings)
  LL.reportSettingsCard = async function () {
    let s;
    try { s = await api('/api/reports/settings'); } catch (e) { return null; }
    const time = h('input', { type: 'time', value: s.time, required: true, class: 'rp-set__time' });
    const tzList = h('datalist', { id: 'rp-tz-list' });
    try { (Intl.supportedValuesOf ? Intl.supportedValuesOf('timeZone') : []).forEach(z => tzList.append(h('option', { value: z }))); } catch (e) { /* old browser */ }
    const tz = h('input', { type: 'text', value: s.tz || '', list: 'rp-tz-list', placeholder: s.tz_effective, autocomplete: 'off', spellcheck: 'false' });
    const keep = h('select', { class: 'select' }, [7, 14, 30, 90, 365].map(n => h('option', { value: String(n) }, t('settings.report.keep.days', { n }))));
    keep.value = String(s.keep_days);
    const link = h('input', { type: 'url', value: s.link || '', placeholder: location.origin, spellcheck: 'false' });
    const msg = h('div', { class: 'msg' });
    const nextLine = h('p', { class: 'muted' }, t('settings.report.next', { when: LL.fmtDate(s.next_at) }));
    const save = h('button', { class: 'btn btn--primary', type: 'button', onclick: async () => {
      LL.msg(msg, '');
      try {
        const r = await api('/api/reports/settings', { method: 'PUT', body: { settings: { time: time.value, tz: tz.value.trim(), keep_days: Number(keep.value), link: link.value.trim() } } });
        nextLine.textContent = t('settings.report.next', { when: LL.fmtDate(r.next_at) });
        tz.placeholder = r.tz_effective;
        LL.toast(t('settings.saved'), 'ok');
      } catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
    } }, t('settings.report.save'));
    const base = h('div', { class: 'grid-2 rp-set__grid' },
      LL.field(t('settings.report.time'), time),
      LL.field(t('settings.report.tz'), tz, t('settings.report.tz.hint', { tz: s.tz_effective })),
      LL.field(t('settings.report.keep'), keep),
      LL.field(t('settings.report.link'), link, t('settings.report.link.hint')));
    const chans = h('div', { class: 'rp-channels' }, CHANNELS.map(ch => channelBox(ch, s)));
    const sec = h('section', { class: 'card settings-card', id: 'report-settings' },
      h('h2', {}, t('settings.report.title')), h('p', { class: 'muted' }, t('settings.report.intro')),
      base, tzList, nextLine, msg, h('div', { class: 'btn-row' }, save),
      h('hr'), h('h3', {}, t('settings.report.delivery')), h('p', { class: 'field__hint' }, t('settings.report.delivery.hint')), chans);
    return sec;
  };

  const FIELDS = {
    email: [['host', 'text'], ['port', 'number'], ['security', 'select'], ['username', 'text'], ['password', 'secret'], ['sender', 'email'], ['to', 'text']],
    webhook: [['url', 'secret']],
    ntfy: [['server', 'url'], ['topic', 'secret'], ['token', 'secret']],
    chatbot: [['token', 'secret'], ['chat', 'text']]
  };
  const KEYMAP = { chat: 'chat_id' };

  function channelBox(ch, s) {
    const cfg = s.channels[ch] || {};
    const has = cfg.secrets || {};
    const state = h('span', { class: 'chip ' + (cfg.enabled ? 'chip--ok' : 'chip--muted') }, t(cfg.enabled ? 'settings.report.on' : 'settings.report.off'));
    const enabled = h('input', { type: 'checkbox', checked: !!cfg.enabled });
    const inputs = {}, secrets = {};
    const fields = FIELDS[ch].map(([name, kind]) => {
      const label = t('settings.report.' + ch + '.' + name);
      if (kind === 'select') {
        const sel = h('select', { class: 'select' }, ['starttls', 'tls', 'none'].map(v => h('option', { value: v }, t('settings.report.email.security.' + v))));
        sel.value = cfg.security || 'starttls';
        inputs[name] = sel;
        return LL.field(label, sel);
      }
      if (kind === 'secret') {
        const inp = h('input', { type: 'password', autocomplete: 'new-password', spellcheck: 'false', placeholder: has[name] ? '••••••••' : '' });
        secrets[name] = inp;
        const clear = has[name] ? h('button', { class: 'btn btn--danger-ghost btn--sm', type: 'button', onclick: () => { inp.value = ''; inp.dataset.clear = '1'; inp.placeholder = ''; clear.remove(); } }, t('settings.report.secret.clear')) : null;
        return LL.field(label, h('div', { class: 'rp-secret' }, inp, clear), has[name] ? t('settings.report.secret.saved') : null);
      }
      const inp = h('input', { type: kind, value: cfg[KEYMAP[name] || name] != null ? String(cfg[KEYMAP[name] || name]) : '', spellcheck: 'false' });
      if (kind === 'number') { inp.min = '1'; inp.max = '65535'; }
      inputs[name] = inp;
      return LL.field(label, inp);
    });
    const msg = h('div', { class: 'msg' });
    const collect = () => {
      const c = { enabled: enabled.checked };
      Object.entries(inputs).forEach(([k, el]) => { c[KEYMAP[k] || k] = k === 'port' ? Number(el.value || 0) : el.value.trim(); });
      const sec = {};
      Object.entries(secrets).forEach(([k, el]) => { if (el.value.trim()) sec[ch + '.' + k] = el.value.trim(); else if (el.dataset.clear) sec[ch + '.' + k] = ''; });
      return { settings: { channels: { [ch]: c } }, secrets: sec };
    };
    const saveBtn = h('button', { class: 'btn btn--primary btn--sm', type: 'button', onclick: async () => {
      LL.msg(msg, '');
      try {
        const r = await api('/api/reports/settings', { method: 'PUT', body: collect() });
        const c = r.channels[ch];
        state.className = 'chip ' + (c.enabled ? 'chip--ok' : 'chip--muted'); state.textContent = t(c.enabled ? 'settings.report.on' : 'settings.report.off');
        Object.values(secrets).forEach(el => { el.value = ''; delete el.dataset.clear; });
        LL.toast(t('settings.saved'), 'ok');
      } catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
    } }, t('settings.report.save'));
    const testBtn = h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: async () => {
      LL.msg(msg, '');
      try {
        await api('/api/reports/settings', { method: 'PUT', body: Object.assign(collect(), {}) });
        const r = await api('/api/reports/test', { method: 'POST', body: { channel: ch } });
        LL.msg(msg, r.ok ? t('settings.report.test.ok') : t('settings.report.test.failed', { reason: t(r.key) }), r.ok ? 'ok' : 'error');
      } catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
    } }, t('settings.report.test'));
    const hint = LL.strings['settings.report.' + ch + '.hint'] ? h('p', { class: 'field__hint' }, t('settings.report.' + ch + '.hint')) : null;
    return h('details', { class: 'rp-ch', dataset: { channel: ch } },
      h('summary', { class: 'rp-ch__sum' }, h('span', { class: 'rp-ch__name' }, t('settings.report.' + ch)), state),
      h('div', { class: 'rp-ch__body' }, h('label', { class: 'check' }, enabled, h('span', {}, t('settings.report.enabled'))), hint,
        h('div', { class: 'grid-2 rp-ch__grid' }, fields), msg, h('div', { class: 'btn-row' }, testBtn, saveBtn)));
  }

  LL.view('reports', reportsView);
})();
