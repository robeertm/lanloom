/* Lanloom — firewall editor: view and change mode, drag and drop or "Move to…", a device sheet (network, name, fixed
   address - each only when the controller can change it), draft bar, plain-language preview, passkey approval
   (external executor) or simulation, progress, history and undo. Routes: #/firewall/device/<address> opens the sheet
   of a device, #/firewall/pick a device picker (both used by the command palette). */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const STAGES = ['verify', 'gate', 'backup', 'apply', 'settle', 'watch', 'done'];
  const ICON = {
    lock: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>',
    grip: '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><circle cx="9" cy="6" r="1.6"/><circle cx="15" cy="6" r="1.6"/><circle cx="9" cy="12" r="1.6"/><circle cx="15" cy="12" r="1.6"/><circle cx="9" cy="18" r="1.6"/><circle cx="15" cy="18" r="1.6"/></svg>',
    pin: '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 21s-6-5.6-6-11a6 6 0 0 1 12 0c0 5.4-6 11-6 11z"/><circle cx="12" cy="10" r="2.2"/></svg>',
    shield: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M12 2.8 4.5 5.6v6c0 4.6 3.2 8.2 7.5 9.6 4.3-1.4 7.5-5 7.5-9.6v-6z"/><path d="m8.8 12 2.3 2.3 4.3-4.6" stroke-linecap="round"/></svg>'
  };
  LL.FW_ICON = ICON.shield;

  function pin() { const el = svg(ICON.pin, 'fw-tile__pin'); el.setAttribute('role', 'img'); el.setAttribute('aria-label', t('fw.tile.fixed')); el.removeAttribute('aria-hidden'); el.title = t('fw.tile.fixed'); return el; }
  function svg(html, cls) { const s = h('span', { class: cls || 'fw-ico', 'aria-hidden': 'true' }); s.innerHTML = html; return s; }
  function tr(item) { const v = Object.assign({}, item[1] || {}); if (v.reason && LL.strings[v.reason]) v.reason = t(v.reason); return t(item[0], v); }

  function hasKey(v, key) { const w = v && v.capabilities && v.capabilities.write; return !!w && Object.prototype.hasOwnProperty.call(w, key); }
  // one line for a change of a job (progress card, history): "Phone → Media" or "Phone · name, fixed address"
  function sumText(x) {
    if (x.to && !x.kind) return x.name + ' → ' + x.to;
    const parts = [];
    if (x.to) parts.push('→ ' + x.to);
    (x.fields || []).filter(f => f !== 'network').forEach(f => parts.push(f === 'name' && x.name_new ? t('fw.sum.name', { name: x.name_new }) : t('fw.field.' + f)));
    return x.name + (parts.length ? ' · ' + parts.join(', ') : '');
  }
  LL.fwSumText = sumText;

  function firewallView() {
    return async function (app, rest) {
      const fr = LL.frame(app, 'firewall');
      const page = h('div', { class: 'page fw-page' });
      const bar = h('div', { class: 'fw-draftbar', hidden: true, role: 'region', 'aria-label': t('fw.draft.label') });
      fr.main.append(page, bar);
      const st = { view: null, mode: 'view', pv: null, drag: null, timer: null, source: null, pol: { pair: null, builtin: false, q: '' } };
      try { st.mode = localStorage.getItem('ll-fw-mode') === 'change' ? 'change' : 'view'; st.source = localStorage.getItem('ll-fw-source') || null; } catch (e) { /* private mode */ }

      async function load() {
        try { st.view = await api('/api/firewall' + (st.source ? '?source=' + encodeURIComponent(st.source) : '')); } catch (e) { page.replaceChildren(h('div', { class: 'msg msg--error' }, LL.errText(e))); return; }
        st.pv = st.view.mode !== 'live' && draftCount(st.view) ? await api('/api/firewall/preview', { method: 'POST', body: {} }).catch(() => null) : null;
        render();
        clearTimeout(st.timer);
        if (st.view.running) st.timer = setTimeout(load, 1500);
        else if (st.view.mode === 'live') st.timer = setTimeout(load, 60000);
      }

      async function setMoves(moves, edits) {
        const body = { moves };
        if (edits) body.edits = edits;
        try { await api('/api/firewall/draft', { method: 'PUT', body }); }
        catch (e) { LL.toast(fwErr(e), 'error'); }
        await load();
      }
      function setEdits(devId, fields) {
        const e = Object.assign({}, st.view.draft.edits || {});
        if (fields && Object.keys(fields).length) e[devId] = fields; else delete e[devId];
        return setMoves(Object.assign({}, st.view.draft.moves), e);
      }
      function stage(devId, netId) { const m = Object.assign({}, st.view.draft.moves); m[devId] = netId; return setMoves(m); }
      async function takeSuggestion(moves) {           // AI suggestions become a draft only by the user's click
        await api('/api/firewall/draft', { method: 'PUT', body: { moves } });
        st.mode = 'change'; try { localStorage.setItem('ll-fw-mode', 'change'); } catch (e) { /* ignore */ }
        await load();
      }
      function unstage(devId) {
        const m = Object.assign({}, st.view.draft.moves); delete m[devId];
        const e = Object.assign({}, st.view.draft.edits || {}); delete e[devId];
        return setMoves(m, e);
      }
      const draftCount = v => new Set(Object.keys(v.draft.moves || {}).concat(Object.keys(v.draft.edits || {}))).size;

      // ---------------------------------------------------------------- rendering
      // controls appear only for what the connected controller can do (capability list from the server)
      function canMove(v) { const w = v.capabilities && v.capabilities.write; return LL.can('operate') && (!w || Object.prototype.hasOwnProperty.call(w, 'move')); }
      function canEdit(v, field) { return LL.can('operate') && v.mode !== 'live' && hasKey(v, 'client.set.' + field); }
      function canChange(v) { return canMove(v) || canEdit(v, 'name') || canEdit(v, 'fixed_ip'); }

      function render() {
        const v = st.view;
        if (!canChange(v)) st.mode = 'view';
        if (!v.state) { page.replaceChildren(head(v), h('div', { class: 'empty-state' }, h('h2', {}, t('fw.nostate.title')), h('p', { class: 'muted' }, t('fw.nostate.text')))); bar.hidden = true; return; }
        const kids = [head(v)];
        if (v.mode === 'simulation') kids.push(h('div', { class: 'callout fw-sim' }, h('strong', {}, t('fw.sim.title')), ' ', t('fw.sim.text'),
          LL.can('admin') ? h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: resetSim, disabled: !!v.running }, t('fw.sim.reset')) : null));
        if (v.mode === 'live') kids.push(h('div', { class: 'callout fw-live' }, svg(ICON.lock, 'fw-live__ico'), h('span', {}, t(v.state.hosts && v.state.hosts.length ? 'fw.live.callout_hv' : 'fw.live.callout', { name: v.live.name }))));
        else if (!LL.can('operate')) kids.push(h('div', { class: 'callout' }, t('fw.role.readonly')));
        else if (!canChange(v)) kids.push(h('div', { class: 'callout' }, t('fw.executor.readonly')));
        if (v.quiet) kids.push(h('div', { class: 'callout callout--warn' }, t('fw.quiet.now', { hours: v.quiet_hours })));
        if (v.running) kids.push(progressCard(v.running));
        if (!(v.mode === 'live' && v.state.hosts && v.state.hosts.length)) kids.push(board(v));
        st.pol.source = v.mode === 'live' ? v.source : null;     // "Explain" (AI) reads the rule from this live source
        if (v.state.policies && v.state.policies.length) kids.push(LL.fwPolicies(v.state, st.pol, render));
        if (v.mode === 'live') kids.push(...LL.fwLiveExtras(v.state));
        const hist = history(v);
        if (hist) kids.push(hist);
        page.replaceChildren(...kids);
        renderBar();
      }

      function head(v) {
        const seg = h('div', { class: 'seg', role: 'group', 'aria-label': t('fw.mode.label') },
          ['view', 'change'].map(m => h('button', { type: 'button', class: 'seg__btn' + (st.mode === m ? ' is-active' : ''), 'aria-pressed': String(st.mode === m), onclick: () => {
            st.mode = m; try { localStorage.setItem('ll-fw-mode', m); } catch (e) { /* ignore */ } render();
          } }, t('fw.mode.' + m))));
        const ctrl = v.state ? v.state.controller || {} : {};
        const chip = v.mode === 'live' ? h('span', { class: 'chip chip--accent' }, t('fw.chip.live'))
          : v.mode === 'simulation' ? h('span', { class: 'chip chip--warn' }, t('fw.chip.simulation'))
          : h('span', { class: 'chip ' + (v.executor.connected ? 'chip--ok' : 'chip--danger') }, t(v.executor.connected ? 'fw.chip.connected' : 'fw.chip.offline'));
        const meta = v.mode === 'live' ? [v.live.name, v.live.version ? t('fw.live.version', { v: v.live.version }) : null, v.live.age_s != null ? t('fw.live.age', { age: LL.fmtAge(v.live.age_s) }) : null].concat(v.state && v.state.hosts && v.state.hosts.length ? LL.hvMeta(v.state) : []).filter(Boolean).join(' · ')
          : ctrl.label;
        const srcs = v.sources || [];
        const pick = srcs.length > 1 ? h('div', { class: 'seg fw-source', role: 'group', 'aria-label': t('fw.source.label') }, srcs.map(o => {
          const on = o.key === v.source;
          return h('button', { type: 'button', class: 'seg__btn' + (on ? ' is-active' : ''), 'aria-pressed': String(on), title: o.kind === 'live' ? t('fw.source.live_hint') : '', onclick: () => {
            st.source = o.key; try { localStorage.setItem('ll-fw-source', o.key); } catch (e) { /* ignore */ } st.pol.pair = null; load();
          } }, o.kind === 'live' ? o.name : t(o.kind === 'simulation' ? 'fw.source.sim' : 'fw.source.executor'));
        })) : null;
        return h('div', { class: 'page__head fw-head' },
          h('div', { class: 'fw-head__title' }, h('h1', {}, t('nav.firewall')), h('div', { class: 'fw-head__meta' }, chip, meta ? h('span', { class: 'muted' }, meta) : null)),
          h('div', { class: 'fw-head__tools' }, pick, canMove(v) && LL.aiFirewallButton ? LL.aiFirewallButton(v, v.draft.moves, takeSuggestion) : null, canChange(v) ? seg : null, v.mode === 'live' || !LL.can('admin') ? null : h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => openExecutor(v) }, t('fw.executor.button'))));
      }

      function board(v) {
        const s = v.state;
        const moves = v.draft.moves || {};
        const changes = {};
        (st.pv && st.pv.changes || []).forEach(c => { changes[c.device] = c; });
        const zones = s.zones.map(z => {
          const nets = s.networks.filter(n => n.zone === z.id && n.purpose !== 'wan' && n.purpose !== 'vpn');
          if (!nets.length) return null;
          return h('section', { class: 'fw-zone card' },
            h('header', { class: 'fw-zone__head' }, h('h2', {}, z.name),
              h('div', { class: 'fw-zone__badges' },
                h('span', { class: 'chip ' + (z.internet ? 'chip--ok' : 'chip--muted') }, t(z.internet ? 'fw.zone.internet' : 'fw.zone.no_internet')),
                z.discovery ? h('span', { class: 'chip chip--accent' }, t('fw.zone.discovery')) : null)),
            h('p', { class: 'fw-zone__reach muted' }, reachText(s, z)),
            h('div', { class: 'fw-nets' }, nets.map(n => network(v, n, moves, changes))));
        }).filter(Boolean);
        return h('div', { class: 'fw-board' + (st.mode === 'change' ? ' is-change' : '') }, zones);
      }

      function reachText(s, z) {
        const names = id => (s.zones.find(x => x.id === id) || {}).name;
        const allow = (a, b) => a === b || (s.allow || []).some(([x, y]) => (x === a || x === '*') && (y === b || y === '*'));
        const reach = s.zones.filter(o => o.id !== z.id && allow(z.id, o.id)).map(o => names(o.id));
        return reach.length ? t('fw.zone.reaches', { zones: reach.join(', ') }) : t('fw.zone.reaches_none');
      }

      function network(v, n, moves, changes) {
        const s = v.state;
        const here = s.devices.filter(d => d.network === n.id && !moves[d.id]);
        const leaving = s.devices.filter(d => d.network === n.id && moves[d.id]);
        const arriving = s.devices.filter(d => moves[d.id] === n.id);
        const box = h('div', { class: 'fw-net' + (n.management ? ' fw-net--mgmt' : ''), dataset: { net: n.id } },
          h('div', { class: 'fw-net__head' }, h('span', { class: 'fw-net__name' }, n.name), h('span', { class: 'mono muted fw-net__cidr' }, n.cidr),
            n.vlan != null ? h('span', { class: 'chip chip--muted fw-net__vlan' }, 'VLAN ' + n.vlan) : null,
            n.management ? h('span', { class: 'chip chip--muted' }, svg(ICON.lock), t('fw.net.management')) : null),
          h('div', { class: 'fw-tiles' },
            here.map(d => tile(v, d)), arriving.map(d => tile(v, d, changes[d.id])), leaving.map(d => ghost(s, d, moves[d.id])),
            !here.length && !arriving.length && !leaving.length ? h('div', { class: 'fw-net__empty muted' }, t('fw.net.empty')) : null),
          h('div', { class: 'fw-net__drop', 'aria-hidden': 'true' }, t('fw.drop.here', { net: n.name })));
        if (st.mode === 'change') {
          box.addEventListener('dragover', ev => { if (st.drag && v.targets[st.drag][n.id] === null) { ev.preventDefault(); box.classList.add('is-over'); } });
          box.addEventListener('dragleave', () => box.classList.remove('is-over'));
          box.addEventListener('drop', ev => { ev.preventDefault(); box.classList.remove('is-over'); const id = st.drag; endDrag(); if (id && v.targets[id][n.id] === null) stage(id, n.id); });
        }
        return box;
      }

      function tile(v, d, change) {
        const moved = !!change;
        const locked = !!d.protected;
        const edit = (v.draft.edits || {})[d.id];
        const el = h('div', { class: 'fw-tile' + (locked ? ' is-locked' : '') + (moved ? ' is-new' : '') + (v.requester === d.id ? ' is-self' : ''),
          dataset: { dev: d.id }, title: locked ? t('fw.tile.stays_why', { reason: t(d.protected) }) : d.name },
          st.mode === 'change' && !locked ? svg(ICON.grip, 'fw-tile__grip') : null,
          h('div', { class: 'fw-tile__main' },
            h('div', { class: 'fw-tile__name' }, edit && edit.name ? edit.name : d.name),
            h('div', { class: 'fw-tile__meta mono' }, moved ? ['→ ', h('b', {}, change.new_ip)]
              : edit && edit.fixed_ip != null ? (edit.fixed_ip ? [pin(), h('b', {}, edit.fixed_ip)] : h('s', {}, d.fixed_ip || d.ip)) : [d.fixed_ip ? pin() : null, d.ip || '—'])),
          moved ? h('span', { class: 'chip chip--accent fw-tile__badge' }, t('fw.tile.new'))
            : edit ? h('span', { class: 'chip chip--accent fw-tile__badge' }, t('fw.tile.edited'))
            : locked ? h('span', { class: 'fw-tile__lock', role: 'img', 'aria-label': t('fw.tile.stays') }, svg(ICON.lock))
              : v.requester === d.id ? h('span', { class: 'chip chip--muted fw-tile__badge' }, t('fw.tile.you')) : null);
        if (d.online === false) el.classList.add('is-offline');
        if (st.mode === 'change' && !locked) {
          el.tabIndex = 0;
          el.setAttribute('role', 'button');
          el.setAttribute('aria-label', t(canMove(v) ? 'fw.tile.move_aria' : 'fw.tile.change_aria', { name: d.name }));
          el.draggable = canMove(v);
          el.addEventListener('dragstart', ev => { st.drag = d.id; ev.dataTransfer.effectAllowed = 'move'; try { ev.dataTransfer.setData('text/plain', d.id); } catch (e) { /* old engines */ } markTargets(v, d.id); });
          el.addEventListener('dragend', endDrag);
          el.addEventListener('click', () => openDevice(v, d));
          el.addEventListener('keydown', ev => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); openDevice(v, d); } });
        }
        return el;
      }

      function ghost(s, d, to) {
        const net = s.networks.find(n => n.id === to) || {};
        return h('div', { class: 'fw-tile fw-tile--ghost', title: t('fw.tile.leaving', { net: net.name }) },
          h('div', { class: 'fw-tile__main' }, h('div', { class: 'fw-tile__name' }, h('s', {}, d.name)), h('div', { class: 'fw-tile__meta' }, '→ ' + net.name)),
          st.mode === 'change' ? h('button', { class: 'icon-btn fw-tile__undo', type: 'button', 'aria-label': t('fw.draft.undo_one', { name: d.name }), onclick: () => unstage(d.id) }, '↺') : null);
      }

      function markTargets(v, devId) {
        page.querySelectorAll('.fw-net').forEach(box => {
          const why = v.targets[devId][box.dataset.net];
          box.classList.toggle('is-target', why === null);
          box.classList.toggle('is-blocked', why !== null && why !== 'fw.why.same');
        });
        page.querySelector('.fw-board').classList.add('is-dragging');
      }
      function endDrag() {
        st.drag = null;
        page.querySelectorAll('.fw-net').forEach(b => b.classList.remove('is-target', 'is-blocked', 'is-over'));
        const b = page.querySelector('.fw-board'); if (b) b.classList.remove('is-dragging');
      }

      // ---------------------------------------------------------------- "Move to…" sheet (phone, keyboard, click)
      function openMove(v, d) {
        const s = v.state;
        let pick = null;
        const go = h('button', { class: 'btn btn--primary btn--block', type: 'button', disabled: true, onclick: () => { dlg.close(); stage(d.id, pick); } }, t('fw.move.choose'));
        const list = h('div', { class: 'fw-move', role: 'radiogroup', 'aria-label': t('fw.move.title', { name: d.name }) }, s.networks.map(n => {
          const why = v.targets[d.id][n.id];
          const zone = s.zones.find(z => z.id === n.zone) || {};
          const btn = h('button', { type: 'button', class: 'fw-move__opt', role: 'radio', 'aria-checked': 'false', disabled: why !== null,
            onclick: () => { pick = n.id; list.querySelectorAll('.fw-move__opt').forEach(b => { b.classList.toggle('is-picked', b === btn); b.setAttribute('aria-checked', String(b === btn)); });
              go.disabled = false; go.textContent = t('fw.move.stage', { net: n.name }); } },
            h('span', { class: 'fw-move__name' }, n.name, ' ', h('span', { class: 'mono muted' }, n.cidr)),
            h('span', { class: 'fw-move__zone muted' }, why && why !== 'fw.why.same' ? t(why) : why === 'fw.why.same' ? t('fw.why.same') : zone.name + ' · ' + t(zone.internet ? 'fw.zone.internet' : 'fw.zone.no_internet')));
          return btn;
        }));
        const dlg = LL.dialog(t('fw.move.title', { name: d.name }), h('div', {}, h('p', { class: 'muted' }, t('fw.move.lead', { ip: d.ip })), list), go);
        dlg.el.classList.add('modal--sheet');
      }

      // ---------------------------------------------------------------- device sheet: network, name, fixed address
      // Only what the controller can change is offered (capability list). Tap a tile on a phone -> this sheet.
      function openDevice(v, d) {
        const editName = canEdit(v, 'name'), editIp = canEdit(v, 'fixed_ip');
        if (!editName && !editIp) { if (canMove(v)) openMove(v, d); return; }
        const s = v.state;
        const staged = (v.draft.edits || {})[d.id] || {};
        const stagedNet = (v.draft.moves || {})[d.id] || null;
        let pick = stagedNet;
        const nameIn = h('input', { type: 'text', class: 'input', maxlength: '64', autocomplete: 'off', value: staged.name != null ? staged.name : d.name });
        const curFixed = staged.fixed_ip != null ? staged.fixed_ip : (d.fixed_ip || '');
        const fixedCb = h('input', { type: 'checkbox', checked: !!curFixed });
        const ipIn = h('input', { type: 'text', class: 'input mono', inputmode: 'decimal', autocomplete: 'off', spellcheck: 'false', value: curFixed || d.ip || '', disabled: !curFixed });
        fixedCb.addEventListener('change', () => { ipIn.disabled = !fixedCb.checked; if (fixedCb.checked && !ipIn.value) ipIn.value = d.ip || ''; if (fixedCb.checked) ipIn.focus(); });
        const msg = h('div', { class: 'msg' });
        const netName = id => (s.networks.find(n => n.id === id) || {}).name || '?';
        const netRow = canMove(v) && !d.protected ? h('div', { class: 'fw-dev__row' },
          h('div', { class: 'fw-dev__label' }, t('fw.dev.network')),
          h('div', { class: 'fw-dev__value' }, h('span', { class: 'fw-dev__net' }, pick ? netName(d.network) + ' → ' + netName(pick) : netName(d.network)),
            h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => { dlg.close(); openMove(v, d); } }, t('fw.dev.move')))) : null;
        const body = h('div', { class: 'fw-dev' },
          h('p', { class: 'muted fw-dev__lead' }, [d.ip || '—', d.online === false ? t('fw.dev.offline') : null].filter(Boolean).join(' · ')),
          netRow,
          editName ? LL.field(t('fw.dev.name'), nameIn) : null,
          editIp ? h('div', { class: 'fw-dev__fixed' },
            h('label', { class: 'check' }, fixedCb, h('span', {}, t('fw.dev.fixed'))),
            LL.field(t('fw.dev.fixed_ip'), ipIn, t('fw.dev.fixed_hint', { cidr: (s.networks.find(n => n.id === (pick || d.network)) || {}).cidr || '' }))) : null,
          msg);
        const save = h('button', { class: 'btn btn--primary btn--block', type: 'button', onclick: async () => {
          const fields = {};
          const name = nameIn.value.trim();
          if (editName && name && name !== d.name) fields.name = name;
          if (editName && !name) { LL.msg(msg, t('fw.dev.name_empty'), 'error'); return; }
          if (editIp) {
            const want = fixedCb.checked ? ipIn.value.trim() : '';
            if (fixedCb.checked && !/^[0-9a-fA-F:.]{2,45}$/.test(want)) { LL.msg(msg, t('fw.dev.ip_invalid'), 'error'); return; }
            if (want !== (d.fixed_ip || '')) fields.fixed_ip = want;
          }
          dlg.close();
          await setEdits(d.id, fields);
        } }, t('fw.dev.stage'));
        const dlg = LL.dialog(t('fw.dev.title', { name: d.name }), body, save);
        dlg.el.classList.add('modal--sheet');
        setTimeout(() => (editName ? nameIn : ipIn).focus(), 30);
      }

      // a searchable list of the devices that can be changed (command palette: "Change a device…")
      function openPicker(v) {
        const s = v.state;
        const all = s.devices.filter(d => !d.protected).sort((a, b) => a.name.localeCompare(b.name));
        const q = h('input', { type: 'search', class: 'input', placeholder: t('fw.pick.search'), 'aria-label': t('fw.pick.search'), autocomplete: 'off' });
        const list = h('div', { class: 'fw-move fw-pick', role: 'listbox', 'aria-label': t('fw.pick.title') });
        const draw = () => {
          const needle = q.value.trim().toLowerCase();
          const hits = all.filter(d => !needle || d.name.toLowerCase().includes(needle) || String(d.ip || '').includes(needle)).slice(0, 60);
          list.replaceChildren(...(hits.length ? hits.map(d => h('button', { type: 'button', class: 'fw-move__opt', role: 'option', onclick: () => { dlg.close(); openDevice(v, d); } },
            h('span', { class: 'fw-move__name' }, d.name, ' ', h('span', { class: 'mono muted' }, d.ip || '—')),
            h('span', { class: 'fw-move__zone muted' }, (s.networks.find(n => n.id === d.network) || {}).name || ''))) : [h('p', { class: 'muted' }, t('fw.pick.none'))]));
        };
        q.addEventListener('input', draw);
        draw();
        const dlg = LL.dialog(t('fw.pick.title'), h('div', { class: 'fw-dev' }, q, list));
        dlg.el.classList.add('modal--sheet');
        setTimeout(() => q.focus(), 30);
      }

      // ---------------------------------------------------------------- draft bar
      function renderBar() {
        const v = st.view;
        const moves = v.draft.moves || {};
        const edits = v.draft.edits || {};
        const ids = [...new Set(Object.keys(moves).concat(Object.keys(edits)))];
        bar.hidden = !ids.length;
        if (!ids.length) { fr.main.classList.remove('has-draftbar'); return; }
        fr.main.classList.add('has-draftbar');
        const s = v.state;
        const chips = ids.map(id => {
          const d = s.devices.find(x => x.id === id) || { name: '?' }; const n = s.networks.find(x => x.id === moves[id]);
          const e = edits[id] || {};
          const parts = [];
          if (n) parts.push('→ ' + n.name);
          if (e.name) parts.push(t('fw.sum.name', { name: e.name }));
          if (e.fixed_ip != null) parts.push(e.fixed_ip ? t('fw.sum.fixed', { ip: e.fixed_ip }) : t('fw.sum.fixed_off'));
          return h('button', { type: 'button', class: 'fw-chip', title: t('fw.draft.undo_one', { name: d.name }), onclick: () => unstage(id) }, d.name + ' ' + parts.join(' · '), h('span', { 'aria-hidden': 'true' }, ' ✕'));
        });
        bar.replaceChildren(
          h('div', { class: 'fw-draftbar__info' }, h('strong', {}, t('fw.draft', { n: ids.length })),
            v.draft.undo_of ? h('span', { class: 'chip chip--muted' }, t('fw.draft.is_undo')) : null, h('div', { class: 'fw-draftbar__chips' }, chips)),
          h('div', { class: 'fw-draftbar__actions' },
            h('button', { class: 'btn btn--ghost', type: 'button', onclick: async () => { await api('/api/firewall/draft', { method: 'DELETE' }); load(); } }, t('fw.draft.discard')),
            h('button', { class: 'btn btn--primary', type: 'button', disabled: !!v.running, onclick: openPreview }, t('fw.draft.preview'))));
      }

      // ---------------------------------------------------------------- preview
      async function openPreview() {
        let pv;
        try { pv = await api('/api/firewall/preview', { method: 'POST', body: {} }); } catch (e) { LL.toast(LL.errText(e), 'error'); return; }
        const v = st.view;
        const confirm = new Set();
        const needs = new Set(pv.confirm);
        const msg = h('div', { class: 'msg' });
        const ext = v.mode === 'external';
        const applyBtn = h('button', { class: 'btn btn--primary btn--block fw-apply', type: 'button' }, t(ext ? 'fw.preview.apply_passkey' : 'fw.preview.apply_sim'));
        const refresh = () => { applyBtn.disabled = !LL.can('admin') || pv.blocks.length > 0 || [...needs].some(k => !confirm.has(k)) || (ext && !LL.passkeySupported()); };
        const editRows = c => (c.edit || []).length ? h('ul', { class: 'fw-pv__edits' }, c.edit.map(e => h('li', {},
          h('span', { class: 'fw-pv__field' }, t('fw.field.' + e.field)), ' ',
          h('s', { class: e.field === 'fixed_ip' ? 'mono' : '' }, e.from || t('fw.preview.none_value')), ' → ',
          h('b', { class: e.field === 'fixed_ip' ? 'mono' : '' }, e.to || t('fw.preview.none_value'))))) : null;
        const changes = h('div', { class: 'fw-pv__changes' }, pv.changes.map(c => !c.to ? h('div', { class: 'fw-pv__change' },
          h('div', { class: 'fw-pv__what' }, h('strong', {}, c.name), h('span', { class: 'muted' }, ' · ' + t('fw.preview.settings'))), editRows(c)) : h('div', { class: 'fw-pv__change' },
          h('div', { class: 'fw-pv__what' }, h('strong', {}, c.name), h('span', { class: 'muted' }, ' · ' + t('fw.preview.from_to', { from: c.from, to: c.to }))),
          h('div', { class: 'mono fw-pv__ip' }, h('s', {}, c.old_ip), ' → ', h('b', {}, c.new_ip)), editRows(c),
          h('div', { class: 'fw-pv__cols' },
            h('div', { class: 'fw-pv__box fw-pv__box--ok' }, h('h4', {}, t('fw.preview.may')), h('ul', {}, c.may.length ? c.may.map(x => h('li', {}, tr(x))) : [h('li', { class: 'muted' }, t('fw.preview.nothing'))])),
            h('div', { class: 'fw-pv__box fw-pv__box--no' }, h('h4', {}, t('fw.preview.may_not')), h('ul', {}, c.may_not.length ? c.may_not.map(x => h('li', {}, tr(x))) : [h('li', { class: 'muted' }, t('fw.preview.nothing_lost'))]))))));
        const confirmKey = w => {
          if (w[0] === 'fw.warn.private_address') return pv.confirm.find(k => k.startsWith('private_address:') && pv.changes.some(c => k === 'private_address:' + c.device && c.name === w[1].name));
          return { 'fw.warn.self': 'self', 'fw.warn.quiet': 'quiet' }[w[0]];
        };
        const warns = h('ul', { class: 'fw-pv__warns' }, pv.warnings.map(w => {
          const key = confirmKey(w);
          if (key && needs.has(key)) {
            const cb = h('input', { type: 'checkbox', onchange: () => { cb.checked ? confirm.add(key) : confirm.delete(key); refresh(); } });
            return h('li', { class: 'fw-warn fw-warn--confirm' }, h('label', { class: 'check' }, cb, h('span', {}, tr(w), h('em', { class: 'muted' }, ' ' + t('fw.preview.confirm')))));
          }
          return h('li', { class: 'fw-warn' }, tr(w));
        }));
        const blocks = pv.blocks.length ? h('ul', { class: 'fw-pv__blocks' }, pv.blocks.map(b => h('li', {}, tr(b)))) : null;
        const minutes = ext ? Math.round(((v.executor && v.executor.watch_s) || 3600) / 60) : 0;
        const protect = h('div', { class: 'callout fw-pv__protect' }, svg(ICON.shield, 'fw-pv__shield'),
          h('div', {}, h('strong', {}, t('fw.preview.protect_title')), h('p', {}, t(ext ? 'fw.preview.protect_text' : 'fw.preview.protect_sim', { min: minutes || 60 }))));
        const passHint = !LL.can('admin') ? h('p', { class: 'msg msg--info' }, t('fw.role.approve_admin'))
          : ext && !LL.passkeySupported() ? h('p', { class: 'msg msg--error' }, t('fw.preview.needs_secure')) : null;
        const body = h('div', { class: 'fw-pv' },
          h('h3', {}, t('fw.preview.changes')), changes,
          blocks ? [h('h3', {}, t('fw.preview.blocked')), blocks] : null,
          pv.warnings.length ? [h('h3', {}, t('fw.preview.watch_out')), warns] : null,
          protect, passHint, msg);
        const dlg = LL.dialog(t('fw.preview.title'), body, applyBtn);
        dlg.el.classList.add('modal--wide');
        refresh();
        applyBtn.addEventListener('click', async () => {
          applyBtn.disabled = true; LL.msg(msg, '');
          try {
            const p = await api('/api/firewall/plan', { method: 'POST', body: { confirmed: [...confirm] } });
            const body2 = { plan: p.plan.id };
            if (p.sign) body2.assertion = await signPlan(p.sign);
            await api('/api/firewall/approve', { method: 'POST', body: body2 });
            dlg.close(); LL.toast(t(ext ? 'fw.toast.queued' : 'fw.toast.sim_started'), 'ok'); load();
          } catch (e) { LL.msg(msg, fwErr(e), 'error'); refresh(); }
        });
      }

      function fwErr(e) {
        if (e && e.key && e.key.startsWith('fw.') && LL.strings[e.key]) return t(e.key, e.vars || {});
        return LL.errText(e);
      }

      async function signPlan(sign) {
        if (!sign.allow.length) { const err = new Error('fw.err.no_passkey'); err.key = 'fw.err.no_passkey'; throw err; }
        const cred = await navigator.credentials.get({ publicKey: {
          challenge: LL.unb64(sign.challenge), rpId: sign.rp_id, userVerification: 'required', timeout: 120000,
          allowCredentials: sign.allow.map(id => ({ type: 'public-key', id: LL.unb64(id) })) } });
        const r = cred.response;
        return { credential_id: LL.b64(cred.rawId), authenticator_data: LL.b64(r.authenticatorData), client_data: LL.b64(r.clientDataJSON), signature: LL.b64(r.signature) };
      }

      // ---------------------------------------------------------------- progress and history
      function progressCard(job) {
        const p = job.progress || { stage: job.stage };
        const idx = STAGES.indexOf(p.stage);
        const until = p.watch_until ? Math.max(0, p.watch_until - Math.round(Date.now() / 1000)) : null;
        return h('section', { class: 'card fw-progress', 'aria-live': 'polite' },
          h('h2', {}, t('fw.progress.title')),
          h('p', { class: 'muted' }, job.summary.map(sumText).join(' · ')),
          h('ol', { class: 'fw-steps' }, STAGES.slice(0, -1).map((s, i) => h('li', { class: i < idx ? 'is-done' : i === idx ? 'is-now' : '' },
            h('span', { class: 'fw-steps__dot', 'aria-hidden': 'true' }, i < idx ? '✓' : ''), t('fw.stage.' + s),
            s === 'watch' && i === idx && until != null ? h('span', { class: 'muted' }, ' · ' + t('fw.progress.left', { s: until })) : null))));
      }

      function history(v) {
        if (!v.history.length) return null;
        const now = Date.now() / 1000;
        return h('section', { class: 'card fw-history' }, h('h2', {}, t('fw.history.title')),
          h('ul', {}, v.history.slice(0, 8).map(j => h('li', { class: 'fw-history__row' },
            h('span', { class: 'chip ' + ({ done: 'chip--ok', rolled_back: 'chip--warn', failed: 'chip--danger', refused: 'chip--danger', running: 'chip--accent' }[j.status] || 'chip--muted') }, t('fw.status.' + j.status)),
            h('span', { class: 'fw-history__what' }, j.summary.map(sumText).join(', '), j.undo_of ? h('span', { class: 'muted' }, ' · ' + t('fw.history.undo_of')) : null),
            h('span', { class: 'muted fw-history__when' }, LL.fmtDate(j.t), j.reason && j.status !== 'done' && LL.strings[j.reason] ? ' · ' + t(j.reason) : ''),
            j.status === 'done' && j.inverse && j.undo_until > now && LL.can('operate') ? h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: async () => {
              try { await api('/api/firewall/undo', { method: 'POST', body: { plan: j.plan } }); st.mode = 'change'; await load(); openPreview(); } catch (e) { LL.toast(fwErr(e), 'error'); }
            } }, t('fw.history.undo')) : null))));
      }

      async function resetSim() {
        try { await api('/api/firewall/reset', { method: 'POST', body: {} }); await api('/api/firewall/draft', { method: 'DELETE' }); LL.toast(t('fw.sim.reset_done'), 'ok'); load(); }
        catch (e) { LL.toast(fwErr(e), 'error'); }
      }

      // ---------------------------------------------------------------- executor dialog
      async function openExecutor(v) {
        const ex = v.executor || {};
        const rows = [[t('fw.executor.mode'), t(v.mode === 'simulation' ? 'fw.executor.mode_sim' : 'fw.executor.mode_ext')],
          [t('fw.executor.target'), ex.target || '—'], [t('fw.executor.armed'), t(ex.armed ? 'fw.executor.yes' : 'fw.executor.no')]];
        if (v.mode === 'external') rows.push([t('fw.executor.seen'), ex.age_s != null ? LL.fmtAge(ex.age_s) : '—']);
        if (v.mode === 'external' && ex.version) rows.push([t('fw.executor.version'), ex.version + (ex.outdated ? ' · ' + t('fw.executor.outdated') : '')]);
        const keyBox = h('div', { class: 'fw-keys' });
        const body = h('div', {}, h('p', { class: 'muted' }, t(v.mode === 'simulation' ? 'fw.executor.lead_sim' : 'fw.executor.lead_ext')),
          h('table', { class: 'mini-table' }, h('tbody', {}, rows.map(r => h('tr', {}, h('td', {}, r[0]), h('td', {}, r[1]))))),
          h('h3', {}, t('fw.executor.keys')), keyBox, h('p', { class: 'muted' }, t('fw.executor.docs')));
        LL.dialog(t('fw.executor.title'), body);
        try {
          const k = await api('/api/firewall/keys');
          keyBox.replaceChildren(k.keys.length ? h('ul', {}, k.keys.map((key, i) => h('li', { class: 'fw-keys__row' },
            h('span', {}, k.names[i] || 'Passkey'), h('code', { class: 'mono' }, key.fingerprint),
            (ex.keys || []).includes(key.fingerprint) ? h('span', { class: 'chip chip--ok' }, t('fw.executor.pinned')) : null,
            h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => {
              const text = JSON.stringify(key);
              (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(() => LL.toast(t('fw.executor.copied'), 'ok'), () => window.prompt(t('fw.executor.copy'), text));
            } }, t('fw.executor.copy'))))) : h('p', { class: 'muted' }, t('fw.executor.no_keys')));
        } catch (e) { keyBox.replaceChildren(h('p', { class: 'msg msg--error' }, LL.errText(e))); }
      }

      await load();
      const want = rest && rest[0];
      if ((want === 'device' || want === 'pick') && st.view && st.view.state) {
        if (st.view.mode === 'live' && st.view.sources.some(o => o.key === 'editor')) { st.source = 'editor'; await load(); }
        window.history.replaceState(null, '', LL.base + '#/firewall');
        if (canChange(st.view)) {
          st.mode = 'change'; try { localStorage.setItem('ll-fw-mode', 'change'); } catch (e) { /* ignore */ } render();
          const ip = want === 'device' ? decodeURIComponent(rest[1] || '') : '';
          const dev = ip ? st.view.state.devices.find(d => d.ip === ip || d.fixed_ip === ip) : null;
          if (dev && !dev.protected) openDevice(st.view, dev);
          else if (want === 'pick' || ip) { if (ip && !dev) LL.toast(t('fw.pick.not_here'), 'info'); openPicker(st.view); }
        } else LL.toast(t(LL.can('operate') ? 'fw.executor.readonly' : 'fw.role.readonly'), 'info');
      }
      return { destroy: () => { clearTimeout(st.timer); fr.destroy(); } };
    };
  }

  LL.view('firewall', firewallView());

  // ------------------------------------------------------------------ command palette (picked up by js/palette.js)
  LL.registerCommand = LL.registerCommand || function (cmd) { LL.commands = (LL.commands || []).filter(c => c.id !== cmd.id).concat([cmd]); };
  LL.registerAction = LL.registerAction || function (act) { LL.actions = (LL.actions || []).filter(a => a.id !== act.id).concat([act]); };
  LL.registerCommand({ id: 'firewall-change-device', group: 'commands', order: 40, label: 'fw.cmd.change', keywords: 'fw.cmd.change_keywords',
    available: () => LL.can('operate'), run: () => LL.go('#/firewall/pick') });
  LL.registerAction({ id: 'firewall-move', kinds: ['device'], order: 20, label: 'fw.cmd.move_to', available: hit => LL.can('operate') && !!hit.ip,
    run: hit => LL.go('#/firewall/device/' + encodeURIComponent(hit.ip)) });
})();

/* Lanloom — read-only parts of the firewall tab for configurations read by a device-family adapter: zone matrix,
   rule list (with hit counters where the controller reports them), port forwarding, wireless networks, equipment
   and ports. Nothing here can change anything. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t } = LL;
  const fmtCount = n => (n == null ? '' : n >= 1e9 ? LL.fmtNum(n / 1e9, 1) + '\u00a0G' : n >= 1e6 ? LL.fmtNum(n / 1e6, 1) + '\u00a0M' : n >= 1e4 ? LL.fmtNum(Math.round(n / 1e3), 0) + '\u00a0k' : LL.fmtNum(n, 0));

  function zoneName(s, id) { const z = s.zones.find(x => x.id === id); return z ? z.name : '—'; }
  function netName(s, id) { const n = s.networks.find(x => x.id === id); return n ? n.name : '?'; }
  function wholeZone(p) { return Object.keys(p.src).every(k => k === 'zone') && Object.keys(p.dst).every(k => k === 'zone') && !p.ports && p.proto === 'any' && !(p.states || []).length && !p.scheduled; }

  function sideText(s, side) {
    const m = side.match;
    let what = zoneName(s, side.zone);
    if (m) {
      const list = (m.items || []).slice(0, 3);
      const more = (m.items || []).length > 3 ? ' +' + ((m.items || []).length - 3) : '';
      if (m.kind === 'network') what = list.map(id => netName(s, id)).join(', ') + more;
      else if (m.kind === 'ip' || m.kind === 'domain') what = (m.group ? m.group + ': ' : '') + list.join(', ') + more;
      else if (m.kind === 'region') what = t('fw.pol.match.region', { items: list.join(', ') + more });
      else if (m.kind === 'client') what = t('fw.pol.match.client', { n: m.count || 0 });
      else if (m.kind === 'app') what = t('fw.pol.match.app', { n: m.count || 0 });
      else what = t('fw.pol.match.' + (m.kind === 'vpn' ? 'vpn' : 'other'));
      if (m.opposite) what = t('fw.pol.not', { what });
    }
    if (side.ports) what += ' · ' + t('fw.pol.ports', { ports: side.ports });
    return what;
  }

  LL.fwPolicies = function (s, f, rerender) {
    const zones = s.zones;
    const allow = new Set((s.allow || []).map(([a, b]) => a + '>' + b));
    const pols = s.policies.slice().sort((a, b) => (a.order - b.order) || (a.name < b.name ? -1 : 1));
    const custom = {};
    pols.forEach(p => { if (!p.builtin && !wholeZone(p)) { const k = p.src.zone + '>' + p.dst.zone; custom[k] = (custom[k] || 0) + 1; } });
    const pick = k => { f.pair = f.pair === k ? null : k; rerender(); };
    const matrix = h('div', { class: 'fw-matrix-wrap' }, h('table', { class: 'fw-matrix' },
      h('thead', {}, h('tr', {}, h('th', { scope: 'col', class: 'fw-matrix__corner' }, h('span', {}, t('fw.pol.from')), ' ↓ ', h('span', {}, t('fw.pol.to')), ' →'),
        zones.map(z => h('th', { scope: 'col' }, z.name)))),
      h('tbody', {}, zones.map(a => h('tr', {}, h('th', { scope: 'row' }, a.name), zones.map(b => {
        if (a.id === b.id) return h('td', { class: 'fw-matrix__self' }, '·');
        const k = a.id + '>' + b.id;
        const ok = allow.has(k);
        const n = custom[k] || 0;
        return h('td', {}, h('button', { type: 'button', class: 'fw-cell ' + (ok ? 'is-allow' : 'is-block') + (f.pair === k ? ' is-picked' : ''),
          'aria-pressed': String(f.pair === k), title: t(ok ? 'fw.pol.cell_allow' : 'fw.pol.cell_block', { from: a.name, to: b.name, n }),
          onclick: () => pick(k) }, h('span', { 'aria-hidden': 'true' }, ok ? '✓' : '✕'), n ? h('span', { class: 'fw-cell__n' }, String(n)) : null));
      }))))));
    const q = h('input', { type: 'search', class: 'input fw-pol__q', placeholder: t('fw.pol.search'), value: f.q, 'aria-label': t('fw.pol.search') });
    const bi = h('input', { type: 'checkbox', checked: f.builtin });
    const listBox = h('div', { class: 'fw-pol__groups' });
    function drawList() {
      const needle = f.q.trim().toLowerCase();
      const shown = pols.filter(p => (f.builtin || f.pair || !p.builtin) && (!f.pair || p.src.zone + '>' + p.dst.zone === f.pair)
        && (!needle || (p.name + ' ' + sideText(s, p.src) + ' ' + sideText(s, p.dst)).toLowerCase().includes(needle)));
      const groups = [];
      const idx = {};
      shown.forEach(p => { const k = p.src.zone + '>' + p.dst.zone; if (!(k in idx)) { idx[k] = groups.length; groups.push({ k, src: p.src.zone, dst: p.dst.zone, rows: [] }); } groups[idx[k]].rows.push(p); });
      const zi = id => zones.findIndex(z => z.id === id);
      groups.sort((x, y) => (zi(x.src) - zi(y.src)) || (zi(x.dst) - zi(y.dst)));
      listBox.replaceChildren(...(groups.length ? groups.map(g => h('section', { class: 'fw-pol__group' },
        h('h3', { class: 'fw-pol__pair' }, zoneName(s, g.src), h('span', { class: 'muted', 'aria-hidden': 'true' }, ' → '), zoneName(s, g.dst),
          h('span', { class: 'chip ' + (allow.has(g.k) ? 'chip--ok' : 'chip--muted') }, t(allow.has(g.k) ? 'fw.pol.allow' : 'fw.pol.block'))),
        h('ol', { class: 'fw-pol__rows' }, g.rows.map(p => h('li', { class: 'fw-rule' + (p.enabled ? '' : ' is-off') + (p.builtin ? ' is-builtin' : '') },
          h('span', { class: 'fw-rule__act fw-rule__act--' + p.action }, t('fw.pol.action.' + (['allow', 'block', 'reject'].includes(p.action) ? p.action : 'other'))),
          h('div', { class: 'fw-rule__main' },
            h('div', { class: 'fw-rule__name' }, p.name),
            h('div', { class: 'fw-rule__meta' }, sideText(s, p.src) + ' → ' + sideText(s, p.dst)
              + (p.proto !== 'any' ? ' · ' + p.proto.toUpperCase() : '') + (p.ports ? ' · ' + t('fw.pol.ports', { ports: p.ports }) : ''),
              (p.states || []).length ? ' · ' + t('fw.pol.states', { states: p.states.join(', ') }) : '')),
          h('div', { class: 'fw-rule__side' },
            p.hits != null ? h('span', { class: 'fw-rule__hits', title: t('fw.pol.hits', { n: p.hits }) }, t('fw.pol.hits', { n: fmtCount(p.hits) })) : null,
            !p.enabled ? h('span', { class: 'chip chip--muted' }, t('fw.pol.disabled')) : null,
            p.scheduled ? h('span', { class: 'chip chip--muted' }, t('fw.pol.scheduled')) : null,
            p.builtin ? h('span', { class: 'chip chip--muted' }, t('fw.pol.builtin')) : null,
            f.source && LL.aiExplainButton ? LL.aiExplainButton('rule', { source: f.source, id: p.id }, { small: true, title: p.name }) : null))))))
        : [h('p', { class: 'muted fw-pol__none' }, t('fw.pol.none'))]));
    }
    q.addEventListener('input', () => { f.q = q.value; drawList(); });
    bi.addEventListener('change', () => { f.builtin = bi.checked; drawList(); });
    drawList();
    const nCustom = pols.filter(p => !p.builtin).length;
    return h('section', { class: 'card fw-pol' },
      h('header', { class: 'fw-pol__head' }, h('h2', {}, t('fw.pol.title')),
        h('p', { class: 'muted' }, t('fw.pol.lead', { n: pols.length, own: nCustom, z: zones.length }))),
      h('h3', { class: 'fw-sub' }, t('fw.pol.matrix')), h('p', { class: 'muted fw-hint' }, t('fw.pol.matrix_hint')), matrix,
      h('div', { class: 'fw-pol__tools' }, q, h('label', { class: 'check' }, bi, h('span', {}, t('fw.pol.builtin_show'))),
        f.pair ? h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => pick(f.pair) }, t('fw.pol.all_pairs')) : null),
      listBox);
  };

  function table(cols, rows) {
    return h('div', { class: 'fw-table-wrap' }, h('table', { class: 'fw-table' }, h('thead', {}, h('tr', {}, cols.map(c => h('th', { scope: 'col' }, c)))),
      h('tbody', {}, rows.map(r => h('tr', {}, r.map(c => h('td', {}, c)))))));
  }

  LL.fwLiveExtras = function (s) {
    if (s.hosts && s.hosts.length) return LL.hvFirewallCards(s);
    const out = [];
    const eq = s.equipment || [];
    if (eq.length) {
      const order = { router: 0, switch: 1, ap: 2 };
      out.push(h('section', { class: 'card fw-eq' }, h('h2', {}, t('fw.eq.title')),
        h('div', { class: 'fw-eq__list' }, eq.slice().sort((a, b) => (order[a.kind] ?? 3) - (order[b.kind] ?? 3) || (a.name < b.name ? -1 : 1)).map(e => {
          const ports = (s.ports || []).filter(p => p.device === e.id).sort((a, b) => a.idx - b.idx);
          const up = e.uplink && e.uplink.device ? eq.find(x => x.id === e.uplink.device) : null;
          return h('article', { class: 'fw-eq__item' },
            h('header', { class: 'fw-eq__head' }, LL.kindIcon(e.kind === 'ap' ? 'ap' : e.kind === 'router' ? 'router' : 'switch'),
              h('div', { class: 'fw-eq__titles' }, h('strong', {}, e.name), h('span', { class: 'muted mono' }, [e.model, e.mgmt_ip, e.firmware].filter(Boolean).join(' · '))),
              h('span', { class: 'chip ' + (e.online ? 'chip--ok' : 'chip--danger') }, t(e.online ? 'fw.eq.online' : 'fw.eq.offline'))),
            up ? h('p', { class: 'muted fw-eq__up' }, t('fw.eq.uplink', { name: up.name, port: e.uplink.port != null ? e.uplink.port : '—' })) : null,
            ports.length ? h('ul', { class: 'fw-ports', 'aria-label': t('fw.eq.ports') }, ports.map(p => h('li', {
              class: 'fw-port' + (p.link ? ' is-up' : '') + (p.is_uplink ? ' is-uplink' : '') + (p.poe && p.poe.on ? ' is-poe' : ''),
              title: [p.name, p.link ? (p.speed ? p.speed + ' Mbit/s' : t('fw.eq.link')) : t('fw.eq.nolink'), p.native_vlan != null ? 'VLAN ' + p.native_vlan : null,
                p.poe && p.poe.on ? t('fw.eq.poe') : null, p.is_uplink ? t('fw.eq.is_uplink') : null, p.profile].filter(Boolean).join(' · ') },
              h('span', { class: 'fw-port__n' }, String(p.idx)),
              h('span', { class: 'fw-port__s' }, p.link ? (p.speed >= 1000 ? LL.fmtNum(p.speed / 1000) + 'G' : p.speed ? String(p.speed) : '•') : '–')))) : null);
        }))));
    }
    const ss = s.ssids || [];
    if (ss.length) out.push(h('section', { class: 'card fw-extra' }, h('h2', {}, t('fw.ssid.title')),
      table([t('fw.col.name'), t('fw.col.network'), t('fw.col.bands'), t('fw.col.security'), t('fw.col.state')],
        ss.map(w => [h('strong', {}, w.name), w.network ? s.networks.filter(n => n.id === w.network).map(n => n.name)[0] || '—' : t('fw.ssid.native'),
          (w.bands || []).map(b => b.replace('ghz', ' GHz')).join(', ') || '—', t('fw.sec.' + (w.security || 'other').replace(/[^a-z0-9_]/g, '_')) === 'fw.sec.' + (w.security || 'other').replace(/[^a-z0-9_]/g, '_') ? w.security : t('fw.sec.' + w.security),
          [w.enabled ? t('fw.state.on') : t('fw.state.off'), w.hidden ? t('fw.ssid.hidden') : null, w.guest ? t('fw.ssid.guest') : null].filter(Boolean).join(' · ')]))));
    const fw = s.port_forwards || [];
    out.push(h('section', { class: 'card fw-extra' }, h('h2', {}, t('fw.fwd.title')),
      fw.length ? table([t('fw.col.name'), t('fw.col.outside'), t('fw.col.target'), t('fw.col.proto'), t('fw.col.state')],
        fw.map(p => [h('strong', {}, p.name), h('span', { class: 'mono' }, p.wan_port || '—'), h('span', { class: 'mono' }, (p.dst_ip || '?') + (p.dst_port ? ':' + p.dst_port : '')),
          (p.proto || '').toUpperCase().replace('_', '/'), [p.enabled ? t('fw.state.on') : t('fw.state.off'), p.source_limited ? t('fw.fwd.limited') : null].filter(Boolean).join(' · ')]))
        : h('p', { class: 'muted' }, t('fw.fwd.none'))));
    return out;
  };
})();
