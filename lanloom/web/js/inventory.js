/* Lanloom — Inventory: what each device is for, where it stands, who looks after it, how long it is covered and supported, and
   whether it is still there. One table with filters, a side sheet to edit an entry, bulk editing, a compact block in the device card
   and the palette commands. The server keeps the data (lanloom/inventory.py); this file only shows it and sends the changes. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const enc = encodeURIComponent;

  // ------------------------------------------------------------------ small helpers
  // a calendar day (YYYY-MM-DD, no time zone) in the reader's language
  function day(s) {
    if (!s) return '—';
    const d = new Date(s + 'T12:00:00');
    return isNaN(d.getTime()) ? s : d.toLocaleDateString(LL.locale(), { dateStyle: 'medium' });
  }
  const today = () => { const d = new Date(); const p = n => String(n).padStart(2, '0'); return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()); };
  const name = it => it.title || t('kind.' + it.kind);
  const known = k => (LL.strings[k] ? t(k) : '');

  const warrantyChip = it => (it.warranty ? h('span', { class: 'chip chip--' + ({ ok: 'ok', soon: 'warn', expired: 'danger' }[it.warranty.state] || 'muted') },
    t('inventory.warranty.' + it.warranty.state, { days: it.warranty.days_left })) : null);
  const eolChip = it => (it.eol ? h('span', { class: 'chip chip--' + ({ ok: 'ok', soon: 'warn', past: 'danger' }[it.eol.state] || 'muted') },
    t('inventory.eol.' + it.eol.state, { days: it.eol.days_left })) : null);
  const stateChip = it => h('span', { class: 'chip inv-state inv-state--' + it.state + ' chip--' + ({ active: 'muted', missing: 'warn', removed: 'danger', decommissioned: 'muted' }[it.state] || 'muted') },
    it.state === 'missing' && it.missing_days != null ? t('inventory.state.missing_days', { days: it.missing_days }) : t('inventory.state.' + it.state));
  const autoChip = () => h('span', { class: 'chip inv-auto', title: t('inventory.auto.hint') }, t('inventory.auto'));
  const reasonsOf = it => ((it.hints && it.hints.reasons) || []).map(r => known('inventory.reason.' + r)).filter(Boolean);

  // ------------------------------------------------------------------ switch Devices | Inventory (top of both pages)
  LL.inventorySwitch = function (active) {
    return h('nav', { class: 'seg inv-switch', 'aria-label': t('inventory.switch') },
      h('a', { href: '#/devices', class: 'seg__btn' + (active === 'devices' ? ' is-active' : ''), 'aria-current': active === 'devices' ? 'page' : null }, t('nav.devices')),
      h('a', { href: '#/inventory', class: 'seg__btn' + (active === 'inventory' ? ' is-active' : ''), 'aria-current': active === 'inventory' ? 'page' : null }, t('inventory.title')));
  };

  // ------------------------------------------------------------------ compact block in the device card
  LL.inventorySection = function (dev) {
    const box = h('div', { class: 'inv-mini' });
    api('/api/devices/' + enc(dev.id) + '/inventory').then(it => {
      const rows = [[t('inventory.f.purpose'), it.purpose], [t('inventory.f.room'), it.room], [t('inventory.f.purchase'), it.purchase_date ? day(it.purchase_date) : ''],
        [t('inventory.col.warranty'), it.warranty ? h('span', { class: 'inv-val' }, day(it.warranty.until), warrantyChip(it)) : ''],
        [t('inventory.col.eol'), it.eol ? h('span', { class: 'inv-val' }, day(it.eol.until), eolChip(it)) : ''],
        [t('inventory.f.owner'), it.owner], [t('inventory.col.state'), h('span', { class: 'inv-val' }, stateChip(it), it.provisional ? autoChip() : null)]];
      const blank = !it.purpose && !it.room && !it.owner && !it.purchase_date && !it.warranty;
      box.replaceChildren(blank ? h('p', { class: 'muted inv-mini__empty' }, t('inventory.section.empty')) : null, LL.facts(rows),
        h('p', { class: 'inv-mini__link' }, h('a', { href: '#/inventory/' + enc(it.id) }, t('inventory.section.edit'))));
    }).catch(() => box.replaceChildren());
    return h('section', { class: 'detail__sec' }, h('h3', {}, t('inventory.title')), box);
  };

  // ------------------------------------------------------------------ the view
  const CHIPS = [['all', {}, null], ['attention', { state: 'attention' }, null], ['missing', { state: 'missing' }, s => s.missing], ['removed', { state: 'removed' }, s => s.removed],
    ['warranty_soon', { warranty: 'soon' }, s => s.warranty_soon], ['warranty_expired', { warranty: 'expired' }, s => s.warranty_expired],
    ['eol', { eol: 'any' }, s => s.eol_past + s.eol_soon], ['provisional', { provisional: '1' }, s => s.provisional], ['decommissioned', { state: 'decommissioned' }, s => s.decommissioned]];
  const QUICK = ['state', 'warranty', 'eol', 'provisional'];
  const PARAMS = ['state', 'warranty', 'eol', 'provisional', 'room', 'owner', 'kind', 'q', 'sort', 'dir'];
  const STATES = ['attention', 'open', 'active', 'missing', 'removed', 'decommissioned'];
  const SORTS = [['title', 'inventory.col.device'], ['purpose', 'inventory.col.purpose'], ['room', 'inventory.col.room'], ['owner', 'inventory.col.owner'],
    ['warranty_until', 'inventory.col.warranty'], ['eol_until', 'inventory.col.eol'], ['state', 'inventory.col.state'], ['last_seen', 'inventory.col.last_seen']];
  const BULK = [['purpose', 'inventory.f.purpose', 'text'], ['room', 'inventory.f.room', 'text'], ['owner', 'inventory.f.owner', 'select'],
    ['purchase_date', 'inventory.f.purchase', 'date'], ['warranty_until', 'inventory.f.warranty', 'date'], ['shop', 'inventory.f.shop', 'text']];

  async function inventoryView(app, rest) {
    const fr = LL.frame(app, 'devices');
    const may = LL.can('operate');
    const f = {};
    PARAMS.forEach(k => { const v = LL.query.get(k); if (v) f[k] = v; });
    let data = null, openId = null, selectMode = false, loadSeq = 0, alive = true;
    const selected = new Set();
    const here = id => '#/inventory' + (id ? '/' + enc(id) : '') + LL.queryString();
    const syncUrl = () => {
      PARAMS.forEach(k => { if (f[k]) LL.query.set(k, f[k]); else LL.query.delete(k); });
      LL.deepLink(here(openId), true);
    };

    // ---- skeleton
    const count = h('span', { class: 'muted inv-count', role: 'status' });
    const notes = h('div', { class: 'inv-notes stack-sm' });
    const chipBar = h('div', { class: 'inv-chips', role: 'toolbar', 'aria-label': t('inventory.chips') });
    const q = h('input', { type: 'search', class: 'input inv-search', placeholder: t('inventory.search'), 'aria-label': t('inventory.search'), value: f.q || '', autocomplete: 'off', spellcheck: 'false' });
    const sel = (key, label) => h('select', { class: 'select inv-filter', 'aria-label': t(label), onchange: ev => setFilter(key, ev.target.value) });
    const stateSel = sel('state', 'inventory.filter.state'), roomSel = sel('room', 'inventory.filter.room'), ownerSel = sel('owner', 'inventory.filter.owner'), kindSel = sel('kind', 'inventory.filter.kind');
    const sortSel = h('select', { class: 'select inv-sortsel', 'aria-label': t('inventory.sort.label'), onchange: ev => setSort(ev.target.value) },
      SORTS.map(([k, label]) => h('option', { value: k }, t(label))));
    const dirBtn = h('button', { class: 'icon-btn inv-dir', type: 'button', onclick: () => setSort(f.sort || 'title', (f.dir === 'desc' ? 'asc' : 'desc')) });
    const clearBtn = h('button', { class: 'btn btn--ghost btn--sm inv-clear', type: 'button', hidden: true, onclick: clearFilters }, t('inventory.filter.clear'));
    const selectBtn = may ? h('button', { class: 'btn btn--ghost btn--sm inv-select', type: 'button', 'aria-pressed': 'false', onclick: () => { selectMode = !selectMode; if (!selectMode) selected.clear(); drawAll(); } }, t('inventory.bulk.select')) : null;
    const listHost = h('div', { class: 'inv-list' });
    const bulkHost = h('div', { class: 'inv-bulkhost' });
    const settingsHost = h('div', { class: 'inv-settingshost' });
    const panel = h('aside', { class: 'detail inv-sheet', hidden: true, tabindex: '-1', 'aria-label': t('inventory.sheet.label') });
    const page = h('div', { class: 'page page--wide inv-page' },
      h('div', { class: 'page__head inv-head' }, h('h1', {}, t('inventory.title')), count, LL.inventorySwitch('inventory')),
      h('p', { class: 'muted inv-lead' }, t('inventory.lead')), notes, chipBar,
      h('div', { class: 'inv-filters' }, q, stateSel, roomSel, ownerSel, kindSel, h('span', { class: 'inv-sortbox' }, sortSel, dirBtn), selectBtn, clearBtn),
      listHost, bulkHost, settingsHost);
    fr.main.append(page, panel);

    // ---- filters
    let typing = null;
    q.addEventListener('input', () => { clearTimeout(typing); typing = setTimeout(() => setFilter('q', q.value.trim()), 250); });
    function setFilter(key, value) { if (value) f[key] = value; else delete f[key]; syncUrl(); load(); }
    function setSort(key, dir) { if (key === 'title' && !dir) { delete f.sort; } else f.sort = key; if (dir && dir !== 'asc') f.dir = dir; else delete f.dir; if (!f.sort) delete f.dir; syncUrl(); load(); }
    function clearFilters() { ['state', 'warranty', 'eol', 'provisional', 'room', 'owner', 'kind', 'q'].forEach(k => delete f[k]); q.value = ''; syncUrl(); load(); }
    const chipOn = params => QUICK.every(k => (f[k] || '') === (params[k] || ''));
    function pickChip(params) { QUICK.forEach(k => delete f[k]); Object.assign(f, params); syncUrl(); load(); }

    function fillSelect(el, first, options, value) {
      el.replaceChildren(h('option', { value: '' }, first), ...options.map(([v, label]) => h('option', { value: v }, label)));
      if (value && !options.some(o => o[0] === value)) el.append(h('option', { value }, value));
      el.value = value || '';
      el.classList.toggle('is-set', !!value);
    }

    // ---- drawing
    function drawChips() {
      const s = data.summary;
      chipBar.replaceChildren(...CHIPS.map(([id, params, num]) => {
        const on = chipOn(params);
        const n = id === 'all' ? s.total : num ? num(s) : null;
        return h('button', { class: 'mf-chip inv-chip inv-chip--' + id + (on ? ' is-on' : ''), type: 'button', 'aria-pressed': on ? 'true' : 'false', onclick: () => pickChip(params) },
          t('inventory.chip.' + id), n != null ? h('span', { class: 'inv-chip__n' }, LL.fmtNum(n, 0)) : null);
      }));
    }
    function drawFilters() {
      fillSelect(stateSel, t('inventory.filter.all_states'), STATES.map(v => [v, t('inventory.state.' + v)]), f.state);
      fillSelect(roomSel, t('inventory.filter.all_rooms'), data.facets.rooms.map(v => [v, v]), f.room);
      fillSelect(ownerSel, t('inventory.filter.all_owners'), data.facets.owners.map(v => [v, v]), f.owner);
      fillSelect(kindSel, t('inventory.filter.all_kinds'), data.facets.kinds.map(v => [v, t('kind.' + v)]), f.kind);
      sortSel.value = f.sort || 'title';
      const desc = f.dir === 'desc';
      dirBtn.textContent = desc ? '↓' : '↑';
      dirBtn.setAttribute('aria-label', t('inventory.sort.toggle') + ' (' + t(desc ? 'inventory.sort.desc' : 'inventory.sort.asc') + ')');
      dirBtn.title = dirBtn.getAttribute('aria-label');
      clearBtn.hidden = !['state', 'warranty', 'eol', 'provisional', 'room', 'owner', 'kind', 'q'].some(k => f[k]);
      if (selectBtn) { selectBtn.textContent = t(selectMode ? 'inventory.bulk.done' : 'inventory.bulk.select'); selectBtn.setAttribute('aria-pressed', selectMode ? 'true' : 'false'); selectBtn.classList.toggle('is-on', selectMode); }
    }
    function drawNotes() {
      notes.replaceChildren(...[
        data.observing ? null : h('div', { class: 'callout callout--warn', role: 'status' }, h('p', {}, t('inventory.note.not_observing')))].filter(Boolean));
    }

    const th = (key, label, cls) => {
      const sorted = (f.sort || 'title') === key;
      return h('th', { scope: 'col', class: cls || '', 'aria-sort': sorted ? (f.dir === 'desc' ? 'descending' : 'ascending') : 'none' },
        h('button', { class: 'inv-sort', type: 'button', onclick: () => setSort(key, sorted ? (f.dir === 'desc' ? 'asc' : 'desc') : 'asc') }, t(label)));
    };
    const cell = (cls, label, kids, empty) => h('td', { class: 'inv-c inv-c--' + cls + (empty ? ' is-empty' : ''), dataset: { label } }, kids);
    const dash = () => h('span', { class: 'muted' }, '—');

    function rowOf(it) {
      const rows = [];
      const link = h('a', { class: 'inv-title', href: '#/inventory/' + enc(it.id) + LL.queryString(), onclick: ev => { if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button) return; ev.preventDefault(); openSheet(it.id); } }, name(it));
      const fwSrc = it.firmware && it.firmware_source ? known('inventory.fwsrc.' + it.firmware_source) : '';
      const check = h('input', { type: 'checkbox', checked: selected.has(it.id), 'aria-label': t('inventory.bulk.row', { name: name(it) }), onchange: ev => { if (ev.target.checked) selected.add(it.id); else selected.delete(it.id); drawBulk(); drawHeadCheck(); } });
      const eolSrc = it.eol ? h('span', { class: 'muted inv-src', title: it.eol.name || '' }, t('inventory.eol.src.' + it.eol.source)) : null;
      const tr = h('tr', { class: 'inv-row inv-row--' + it.state + (it.id === openId ? ' is-open' : ''), dataset: { id: it.id },
        onclick: ev => { if (!ev.target.closest('a, button, input, select, label')) openSheet(it.id); } },
        selectMode ? h('td', { class: 'inv-c inv-c--check' }, check) : null,
        cell('device', t('inventory.col.device'), [link, h('span', { class: 'inv-sub' }, h('span', { class: 'mono' }, it.ip), h('span', { class: 'muted' }, ' · ' + t('kind.' + it.kind)))]),
        cell('purpose', t('inventory.col.purpose'), it.purpose || dash(), !it.purpose),
        cell('room', t('inventory.col.room'), it.room || dash(), !it.room),
        cell('owner', t('inventory.col.owner'), it.owner || dash(), !it.owner),
        cell('warranty', t('inventory.col.warranty'), it.warranty ? h('span', { class: 'inv-val' }, h('span', { class: 'inv-date' }, day(it.warranty.until)), warrantyChip(it)) : dash(), !it.warranty),
        cell('eol', t('inventory.col.eol'), it.eol ? h('span', { class: 'inv-val' }, h('span', { class: 'inv-date' }, day(it.eol.until)), eolChip(it), eolSrc) : dash(), !it.eol),
        cell('firmware', t('inventory.col.firmware'), it.firmware ? h('span', { class: 'inv-val' }, h('span', { class: 'inv-fw' }, it.firmware), fwSrc ? h('span', { class: 'muted inv-src' }, fwSrc) : null) : dash(), !it.firmware),
        cell('state', t('inventory.col.state'), h('span', { class: 'inv-val' }, stateChip(it), it.provisional && it.state !== 'decommissioned' ? autoChip() : null,
          may && it.provisional && it.state !== 'decommissioned' ? h('button', { class: 'btn btn--ghost btn--sm inv-mini-btn', type: 'button', title: t('inventory.act.confirm'), 'aria-label': t('inventory.act.confirm') + ': ' + name(it), onclick: () => life(it, 'confirm', null, 'inventory.toast.confirmed').catch(e => LL.toast(LL.errText(e), 'error')) }, '✓') : null)));
      rows.push(tr);
      const extra = extraOf(it);
      if (extra) { tr.classList.add('has-extra'); rows.push(h('tr', { class: 'inv-extra-row inv-row--' + it.state }, h('td', { colspan: String(selectMode ? 9 : 8), class: 'inv-extra-cell' }, extra))); }
      return rows;
    }

    // the line under a row: why Lanloom thinks a device is gone, or what became of a decommissioned one
    function extraOf(it) {
      if (it.state === 'removed') {
        const rs = reasonsOf(it);
        return h('div', { class: 'inv-extra' },
          h('div', { class: 'inv-extra__text' },
            rs.length ? h('p', { class: 'inv-why' }, t('inventory.why', { reasons: rs.join(', ') })) : null,
            it.hints && it.hints.successor ? h('p', { class: 'inv-succ' }, t('inventory.successor', { name: it.hints.successor.title })) : null),
          may ? h('div', { class: 'inv-actions' },
            h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => openSheet(it.id, { decom: true }) }, t('inventory.act.decommission')),
            h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => life(it, 'keep', null, 'inventory.toast.kept').catch(e => LL.toast(LL.errText(e), 'error')) }, t('inventory.act.keep'))) : null);
      }
      if (it.state === 'decommissioned' && it.decommissioned) {
        const d = it.decommissioned;
        return h('div', { class: 'inv-extra' },
          h('div', { class: 'inv-extra__text' },
            h('p', {}, t('inventory.decom.since', { date: day(d.at) }),
              d.replaced_by ? [' · ', t('inventory.decom.replaced') + ' ', h('a', { href: '#/inventory/' + enc(d.replaced_by.id) + LL.queryString(), onclick: ev => { ev.preventDefault(); openSheet(d.replaced_by.id); } }, d.replaced_by.title || '—')] : null,
              d.note ? [' · ', h('span', { class: 'muted' }, d.note)] : null)),
          d.seen_again ? h('div', { class: 'inv-actions' }, h('span', { class: 'chip chip--warn' }, t('inventory.seen_again')),
            may ? h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => life(it, 'reactivate', null, 'inventory.toast.reactivated').catch(e => LL.toast(LL.errText(e), 'error')) }, t('inventory.act.reactivate')) : null) : null);
      }
      return null;
    }

    let headCheck = null;
    function drawHeadCheck() {
      if (!headCheck) return;
      const n = data.items.filter(i => selected.has(i.id)).length;
      headCheck.checked = n > 0 && n === data.items.length; headCheck.indeterminate = n > 0 && n < data.items.length;
    }
    function drawList() {
      const items = data.items;
      if (!data.summary.total) { listHost.replaceChildren(h('div', { class: 'empty-note' }, t('inventory.empty'))); return; }
      if (!items.length) { listHost.replaceChildren(h('div', { class: 'empty-note' }, t('inventory.none'))); return; }
      headCheck = selectMode ? h('input', { type: 'checkbox', 'aria-label': t('inventory.bulk.all'), title: t('inventory.bulk.all'), onchange: ev => {
        items.forEach(i => { if (ev.target.checked) selected.add(i.id); else selected.delete(i.id); }); drawList(); drawBulk(); } }) : null;
      const table = h('table', { class: 'inv-table', 'aria-label': t('inventory.title') },
        h('thead', {}, h('tr', {}, selectMode ? h('th', { scope: 'col', class: 'inv-c--check' }, headCheck, h('span', { class: 'sr-only' }, t('inventory.col.select'))) : null,
          th('title', 'inventory.col.device', 'inv-c--device'), th('purpose', 'inventory.col.purpose', 'inv-c--purpose'), th('room', 'inventory.col.room', 'inv-c--room'), th('owner', 'inventory.col.owner', 'inv-c--owner'),
          th('warranty_until', 'inventory.col.warranty', 'inv-c--warranty'), th('eol_until', 'inventory.col.eol', 'inv-c--eol'),
          h('th', { scope: 'col', class: 'inv-c--firmware' }, t('inventory.col.firmware')), th('state', 'inventory.col.state', 'inv-c--state'))),
        h('tbody', {}, items.map(rowOf)));
      table.classList.toggle('is-selecting', selectMode);
      listHost.replaceChildren(h('div', { class: 'inv-tablewrap card' }, table),
        h('p', { class: 'muted fineprint inv-eolnote' }, t('inventory.note.eol', { date: day(data.eol_as_of) })));
      drawHeadCheck();
    }
    function drawCount() {
      const n = data.items.length, total = data.summary.total;
      count.textContent = n === total ? t('inventory.count', { n }) : t('inventory.count_of', { n, total });
    }

    // ---- bulk editing
    function drawBulk() {
      if (!may || !selectMode) { bulkHost.replaceChildren(); return; }
      const n = selected.size;
      const inputs = {};
      const fields = BULK.map(([key, label, type]) => {
        const el = type === 'select'
          ? h('select', { class: 'select', 'aria-label': t(label) }, h('option', { value: '' }, '—'), data.people.map(p => h('option', { value: p }, p)))
          : h('input', { type: type === 'date' ? 'date' : 'text', class: 'input', maxlength: key === 'purpose' ? '120' : '80', list: key === 'room' ? 'inv-rooms' : null, autocomplete: 'off' });
        inputs[key] = el;
        return LL.field(t(label), el);
      });
      const confirm = h('input', { type: 'checkbox', checked: true });
      const msg = h('div', { class: 'msg', role: 'status' });
      const apply = h('button', { class: 'btn btn--primary btn--sm', type: 'button', disabled: !n, onclick: async () => {
        const set = {}; BULK.forEach(([key]) => { const v = inputs[key].value.trim(); if (v) set[key] = v; });
        if (!Object.keys(set).length && !confirm.checked) { LL.msg(msg, t('inventory.bulk.nothing'), 'error'); return; }
        apply.disabled = true;
        try {
          const res = await api('/api/inventory/bulk', { method: 'POST', body: { ids: [...selected], set, confirm: confirm.checked } });
          LL.toast(t('inventory.bulk.result', { changed: res.changed, skipped: res.skipped }), res.skipped ? 'info' : 'ok');
          selected.clear(); await load();
        } catch (e) { LL.msg(msg, LL.errText(e), 'error'); apply.disabled = false; }
      } }, t('inventory.bulk.apply'));
      bulkHost.replaceChildren(h('section', { class: 'inv-bulk card', 'aria-label': t('inventory.bulk.selected', { n }) },
        h('div', { class: 'inv-bulk__head' }, h('strong', {}, t('inventory.bulk.selected', { n })),
          h('button', { class: 'btn btn--ghost btn--sm', type: 'button', disabled: !n, onclick: () => { selected.clear(); drawList(); drawBulk(); } }, t('inventory.bulk.clear'))),
        h('p', { class: 'muted fineprint' }, t('inventory.bulk.lead')),
        h('div', { class: 'inv-bulk__grid' }, fields),
        h('div', { class: 'inv-bulk__foot' }, h('label', { class: 'check' }, confirm, h('span', {}, t('inventory.bulk.confirm'))), apply), msg,
        h('datalist', { id: 'inv-rooms' }, data.facets.rooms.map(r => h('option', { value: r })))));
    }

    // ---- thresholds (admin)
    const LIMITS = { missing_days: [1, 365], removed_days: [7, 730], auto_remove_days: [0, 90], warranty_warn_days: [1, 365], eol_warn_days: [7, 1095] };
    function drawSettings() {
      if (!LL.can('admin')) { settingsHost.replaceChildren(); return; }
      const open = settingsHost.firstChild && settingsHost.firstChild.open;
      const inputs = {};
      const fields = Object.keys(LIMITS).map(k => {
        const el = h('input', { type: 'number', class: 'input', inputmode: 'numeric', min: String(LIMITS[k][0]), max: String(LIMITS[k][1]), value: String(data.settings[k]) });
        inputs[k] = el;
        return LL.field(t('inventory.settings.' + k), el, k === 'auto_remove_days' ? t('inventory.settings.auto_remove_days.hint') : null);
      });
      const msg = h('div', { class: 'msg', role: 'status' });
      const save = h('button', { class: 'btn btn--primary btn--sm', type: 'button', onclick: async () => {
        const body = {}; Object.keys(LIMITS).forEach(k => { body[k] = Number(inputs[k].value); });
        save.disabled = true;
        try { await api('/api/inventory/settings', { method: 'PUT', body }); LL.toast(t('settings.saved'), 'ok'); await load(); }
        catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
        save.disabled = false;
      } }, t('inventory.save'));
      const det = h('details', { class: 'inv-settings card' }, h('summary', {}, t('inventory.settings.title')),
        h('div', { class: 'inv-settings__body' }, h('p', { class: 'muted' }, t('inventory.settings.lead')), h('div', { class: 'inv-settings__grid' }, fields), msg, h('div', { class: 'btn-row' }, save)));
      det.open = !!open;
      settingsHost.replaceChildren(det);
    }

    function drawAll() { drawCount(); drawChips(); drawFilters(); drawNotes(); drawList(); drawBulk(); drawSettings(); }
    async function load() {
      const seq = ++loadSeq;
      const qs = new URLSearchParams();
      PARAMS.forEach(k => { if (f[k]) qs.set(k, f[k]); });
      let res;
      try { res = await api('/api/inventory' + (qs.toString() ? '?' + qs : '')); }
      catch (e) { if (alive && seq === loadSeq) listHost.replaceChildren(h('div', { class: 'msg msg--error' }, LL.errText(e))); return; }
      if (!alive || seq !== loadSeq) return;
      data = res;
      const ids = new Set(data.items.map(i => i.id));
      [...selected].forEach(id => { if (!ids.has(id)) selected.delete(id); });   // only what is on the screen can be changed in bulk
      drawAll();
      if (openId) markOpen();
    }
    const markOpen = () => listHost.querySelectorAll('tr.inv-row').forEach(tr => tr.classList.toggle('is-open', tr.dataset.id === openId));

    // life cycle step: returns the new item (callers show errors where they have room for them)
    async function life(it, action, body, toastKey) {
      const res = await api('/api/devices/' + enc(it.id) + '/lifecycle', { method: 'POST', body: Object.assign({ action }, body || {}) });
      LL.toast(t(toastKey), 'ok');
      await load();
      if (openId === it.id) renderSheet(res);
      return res;
    }

    // ---- the side sheet of one entry
    async function openSheet(id, opts, fromAddress) {
      openId = id;
      if (!fromAddress) LL.deepLink(here(id));
      panel.hidden = false; panel.classList.add('is-open');
      panel.replaceChildren(h('div', { class: 'detail__loading' }, h('div', { class: 'skeleton' })));
      markOpen();
      let it;
      try { it = await api('/api/devices/' + enc(id) + '/inventory'); }
      catch (e) { if (openId === id) panel.replaceChildren(h('div', { class: 'detail__body' }, h('div', { class: 'msg msg--error' }, LL.errText(e)))); return; }
      if (openId !== id) return;
      renderSheet(it, opts);
      panel.focus({ preventScroll: true });
    }
    function closeSheet() {
      const id = openId;
      openId = null; panel.hidden = true; panel.classList.remove('is-open');
      LL.deepLink(here(null)); markOpen();
      const back = id && listHost.querySelector('tr[data-id="' + CSS.escape(id) + '"] a.inv-title');
      if (back) back.focus({ preventScroll: true });
    }
    const onKey = ev => { if (ev.key === 'Escape' && openId && !document.querySelector('.modal, .cmdk, .cmdk__list')) closeSheet(); };
    document.addEventListener('keydown', onKey);

    function renderSheet(it, opts) {
      opts = opts || {};
      const close = h('button', { class: 'icon-btn detail__close', type: 'button', 'aria-label': t('detail.close'), onclick: closeSheet }, '✕');
      const head = h('div', { class: 'detail__head' }, LL.kindIcon(it.kind),
        h('div', { class: 'detail__titles' }, h('h2', {}, name(it)), h('div', { class: 'muted mono' }, [it.ip, it.mac].filter(Boolean).join(' · '))), close);
      const chips = h('div', { class: 'detail__chips' }, stateChip(it), it.provisional && it.state !== 'decommissioned' ? autoChip() : null, warrantyChip(it), eolChip(it),
        h('span', { class: 'chip' }, t('kind.' + it.kind)));
      const sections = [];
      if (it.state === 'removed' || (it.state === 'missing' && reasonsOf(it).length)) {
        const rs = reasonsOf(it);
        sections.push(h('section', { class: 'detail__sec' },
          rs.length ? h('p', { class: 'inv-why' }, t('inventory.why', { reasons: rs.join(', ') })) : null,
          it.hints && it.hints.successor ? h('p', { class: 'inv-succ' }, t('inventory.successor', { name: it.hints.successor.title })) : null));
      }
      if (it.state === 'decommissioned' && it.decommissioned) {
        const d = it.decommissioned;
        sections.push(h('section', { class: 'detail__sec' }, h('p', {}, t('inventory.decom.since', { date: day(d.at) })),
          d.replaced_by ? h('p', {}, t('inventory.decom.replaced') + ' ', h('a', { href: '#/inventory/' + enc(d.replaced_by.id), onclick: ev => { ev.preventDefault(); openSheet(d.replaced_by.id); } }, d.replaced_by.title || '—')) : null,
          d.note ? h('p', { class: 'muted' }, d.note) : null, d.seen_again ? h('p', {}, h('span', { class: 'chip chip--warn' }, t('inventory.seen_again'))) : null));
      }
      sections.push(may ? formSection(it) : readSection(it));
      if (may) sections.push(lifeSection(it, opts));
      sections.push(h('section', { class: 'detail__sec' }, LL.facts([[t('ident.vendor'), it.vendor], [t('ident.model'), it.model], [t('device.first_seen'), it.first_seen ? LL.fmtDate(it.first_seen) : ''],
        [t('device.last_seen'), it.last_seen ? LL.fmtDate(it.last_seen) : '']])));
      panel.replaceChildren(head, chips, h('div', { class: 'detail__body' }, sections));
    }

    const fwDetected = it => (it.firmware && it.firmware_source && it.firmware_source !== 'own'
      ? t('inventory.f.firmware_detected', { value: it.firmware, source: known('inventory.fwsrc.' + it.firmware_source) || it.firmware_source }) : null);

    function readSection(it) {
      return h('section', { class: 'detail__sec' }, LL.facts([[t('inventory.f.purpose'), it.purpose], [t('inventory.f.room'), it.room], [t('inventory.f.owner'), it.owner],
        [t('inventory.f.purchase'), it.purchase_date ? day(it.purchase_date) : ''], [t('inventory.col.warranty'), it.warranty ? h('span', { class: 'inv-val' }, day(it.warranty.until), warrantyChip(it)) : ''],
        [t('inventory.col.eol'), it.eol ? h('span', { class: 'inv-val' }, day(it.eol.until), eolChip(it), h('span', { class: 'muted inv-src' }, t('inventory.eol.src.' + it.eol.source))) : ''],
        [t('inventory.f.shop'), it.shop], [t('inventory.f.note'), it.note], [t('inventory.f.serial'), it.serial || it.serial_reported],
        [t('inventory.col.firmware'), it.firmware ? it.firmware + (it.firmware_source ? ' (' + (known('inventory.fwsrc.' + it.firmware_source) || it.firmware_source) + ')' : '') : '']]));
    }

    function formSection(it) {
      const inp = (type, val, extra) => h('input', Object.assign({ type, class: 'input', value: val || '', autocomplete: 'off' }, extra || {}));
      const el = {
        purpose: inp('text', it.purpose, { maxlength: '120' }), room: inp('text', it.room, { maxlength: '60', list: 'inv-rooms-sheet' }),
        purchase_date: inp('date', it.purchase_date), warranty_until: inp('date', it.warranty_until),
        shop: inp('text', it.shop, { maxlength: '80' }), note: h('textarea', { class: 'input inv-note', rows: '3', maxlength: '300' }),
        serial: inp('text', it.serial, { maxlength: '60', spellcheck: 'false' }), firmware: inp('text', it.firmware_own, { maxlength: '40', spellcheck: 'false' }),
        eol_until: inp('date', it.eol_until),
        owner: h('select', { class: 'select' }, h('option', { value: '' }, t('inventory.f.owner_none')),
          (data.people.includes(it.owner) || !it.owner ? data.people : data.people.concat([it.owner])).map(p => h('option', { value: p }, p)))
      };
      el.note.value = it.note || ''; el.owner.value = it.owner || '';
      const was = { purpose: it.purpose, room: it.room, purchase_date: it.purchase_date, warranty_until: it.warranty_until, shop: it.shop, note: it.note, serial: it.serial,
        firmware: it.firmware_own, eol_until: it.eol_until, owner: it.owner };
      const msg = h('div', { class: 'msg', role: 'alert' });
      const eolHint = it.eol && it.eol.source === 'table' && !it.eol_until ? t('inventory.f.eol_table', { name: it.eol.name, date: day(it.eol.until) }) : null;
      const rep = it.serial_reported && it.serial_reported !== it.serial ? t('inventory.f.serial_reported', { value: it.serial_reported }) : null;
      const save = h('button', { class: 'btn btn--primary', type: 'submit' }, t('inventory.save'));
      const form = h('form', { class: 'inv-form', novalidate: true, onsubmit: async ev => {
        ev.preventDefault();
        const body = {}; Object.keys(was).forEach(k => { const v = el[k].value.trim(); if (v !== (was[k] || '')) body[k] = v; });
        if (!Object.keys(body).length) { closeSheet(); return; }
        save.disabled = true; LL.msg(msg, '');
        try {
          const res = await api('/api/devices/' + enc(it.id) + '/inventory', { method: 'PUT', body });
          LL.toast(t('device.saved'), 'ok'); await load(); renderSheet(res);
        } catch (e) { LL.msg(msg, LL.errText(e), 'error'); save.disabled = false; }
      } },
      LL.field(t('inventory.f.purpose'), el.purpose, t('inventory.f.purpose.hint')),
      h('div', { class: 'inv-form__row' }, LL.field(t('inventory.f.room'), el.room), LL.field(t('inventory.f.owner'), el.owner)),
      h('div', { class: 'inv-form__row' }, LL.field(t('inventory.f.purchase'), el.purchase_date), LL.field(t('inventory.f.warranty'), el.warranty_until)),
      LL.field(t('inventory.f.shop'), el.shop), LL.field(t('inventory.f.note'), el.note),
      LL.field(t('inventory.f.serial'), el.serial, rep),
      LL.field(t('inventory.f.firmware'), el.firmware, fwDetected(it)),
      LL.field(t('inventory.f.eol'), el.eol_until, eolHint),
      msg, h('div', { class: 'btn-row' }, h('button', { class: 'btn btn--ghost', type: 'button', onclick: closeSheet }, t('inventory.cancel')), save),
      h('datalist', { id: 'inv-rooms-sheet' }, data.facets.rooms.map(r => h('option', { value: r }))));
      return h('section', { class: 'detail__sec' }, form);
    }

    function lifeSection(it, opts) {
      const msg = h('div', { class: 'msg', role: 'alert' });
      const run = (action, body, toastKey) => life(it, action, body, toastKey).catch(e => LL.msg(msg, LL.errText(e), 'error'));
      const btns = [];
      if (it.state === 'decommissioned') btns.push(h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => run('reactivate', null, 'inventory.toast.reactivated') }, t('inventory.act.reactivate')));
      else {
        if (it.provisional) btns.push(h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => run('confirm', null, 'inventory.toast.confirmed') }, t('inventory.act.confirm')));
        else if (it.confirmed) btns.push(h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => run('unconfirm', null, 'inventory.toast.unconfirmed') }, t('inventory.act.unconfirm')));
        if (it.state === 'removed') btns.push(h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => run('keep', null, 'inventory.toast.kept') }, t('inventory.act.keep')));
      }
      const decomBox = h('div', { class: 'inv-decom', hidden: it.state === 'decommissioned' || !opts.decom });
      let built = false;
      async function buildDecom() {
        if (built) return; built = true;
        const at = h('input', { type: 'date', class: 'input', value: today(), max: today() });
        const note = h('input', { type: 'text', class: 'input', maxlength: '200', autocomplete: 'off' });
        const rep = h('select', { class: 'select' }, h('option', { value: '' }, t('inventory.life.no_replacement')));
        let others = [];
        try { others = (await api('/api/inventory?state=open&sort=title')).items.filter(o => o.id !== it.id); } catch (e) { /* the choice stays at "no replacement" */ }
        others.forEach(o => rep.append(h('option', { value: o.id }, name(o) + (o.ip ? ' · ' + o.ip : ''))));
        if (it.hints && it.hints.successor && others.some(o => o.id === it.hints.successor.id)) rep.value = it.hints.successor.id;
        const go = h('button', { class: 'btn btn--primary btn--sm', type: 'button', onclick: () => {
          go.disabled = true; LL.msg(msg, '');
          life(it, 'decommission', { at: at.value, replaced_by: rep.value, note: note.value.trim() }, 'inventory.toast.decommissioned').catch(e => { LL.msg(msg, LL.errText(e), 'error'); go.disabled = false; });
        } }, t('inventory.act.decommission'));
        decomBox.replaceChildren(h('p', { class: 'muted fineprint' }, t('inventory.life.decom_lead')),
          h('div', { class: 'inv-form__row' }, LL.field(t('inventory.life.at'), at), LL.field(t('inventory.life.replaced_by'), rep)),
          LL.field(t('inventory.f.note'), note), h('div', { class: 'btn-row' }, go));
      }
      if (it.state !== 'decommissioned') {
        btns.push(h('button', { class: 'btn btn--ghost btn--sm', type: 'button', 'aria-expanded': opts.decom ? 'true' : 'false', onclick: ev => {
          decomBox.hidden = !decomBox.hidden; ev.currentTarget.setAttribute('aria-expanded', decomBox.hidden ? 'false' : 'true'); if (!decomBox.hidden) buildDecom();
        } }, t('inventory.act.decommission')));
        if (opts.decom) buildDecom();
      }
      return h('section', { class: 'detail__sec' }, h('h3', {}, t('inventory.life.title')), h('div', { class: 'btn-row btn-row--wrap' }, btns), decomBox, msg);
    }

    // ---- start
    await load();
    const want = rest && rest[0] ? decodeURIComponent(rest[0]) : '';
    if (want) openSheet(want, {}, true);
    const baseDestroy = fr.destroy;
    fr.destroy = () => { alive = false; clearTimeout(typing); document.removeEventListener('keydown', onKey); if (baseDestroy) baseDestroy.call(fr); };
    return fr;
  }
  LL.view('inventory', inventoryView);

  // ------------------------------------------------------------------ palette commands (every role may look)
  const go = (id, order, labelKey, kwKey, hash) => LL.registerCommand({ id, group: 'views', order, typedOnly: true, label: labelKey, keywords: kwKey, run: () => LL.go(hash) });
  LL.registerCommand({ id: 'go.inventory', group: 'views', order: 3.5, label: () => t('cmdk.go', { name: t('inventory.title') }), keywords: 'inventory.cmd.kw.open', run: () => LL.go('#/inventory') });
  go('inventory-attention', 3.6, 'inventory.cmd.attention', 'inventory.cmd.kw.attention', '#/inventory?state=attention');
  go('inventory-missing', 3.7, 'inventory.cmd.missing', 'inventory.cmd.kw.missing', '#/inventory?state=missing');
  go('inventory-warranty', 3.8, 'inventory.cmd.warranty', 'inventory.cmd.kw.warranty', '#/inventory?warranty=soon');
  go('inventory-eol', 3.9, 'inventory.cmd.eol', 'inventory.cmd.kw.eol', '#/inventory?eol=any');
})();
