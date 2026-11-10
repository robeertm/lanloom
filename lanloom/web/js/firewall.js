/* Lanloom — firewall editor: view and change mode, drag and drop or "Move to…", draft bar, plain-language
   preview, passkey approval (external executor) or simulation, progress, history and undo. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const STAGES = ['verify', 'gate', 'backup', 'apply', 'settle', 'watch', 'done'];
  const ICON = {
    lock: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>',
    grip: '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><circle cx="9" cy="6" r="1.6"/><circle cx="15" cy="6" r="1.6"/><circle cx="9" cy="12" r="1.6"/><circle cx="15" cy="12" r="1.6"/><circle cx="9" cy="18" r="1.6"/><circle cx="15" cy="18" r="1.6"/></svg>',
    shield: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M12 2.8 4.5 5.6v6c0 4.6 3.2 8.2 7.5 9.6 4.3-1.4 7.5-5 7.5-9.6v-6z"/><path d="m8.8 12 2.3 2.3 4.3-4.6" stroke-linecap="round"/></svg>'
  };
  LL.FW_ICON = ICON.shield;

  function svg(html, cls) { const s = h('span', { class: cls || 'fw-ico', 'aria-hidden': 'true' }); s.innerHTML = html; return s; }
  function tr(item) { const v = Object.assign({}, item[1] || {}); if (v.reason && LL.strings[v.reason]) v.reason = t(v.reason); return t(item[0], v); }

  function firewallView() {
    return async function (app) {
      const fr = LL.frame(app, 'firewall');
      const page = h('div', { class: 'page fw-page' });
      const bar = h('div', { class: 'fw-draftbar', hidden: true, role: 'region', 'aria-label': t('fw.draft.label') });
      fr.main.append(page, bar);
      const st = { view: null, mode: 'view', pv: null, drag: null, timer: null };
      try { st.mode = localStorage.getItem('ll-fw-mode') === 'change' ? 'change' : 'view'; } catch (e) { /* private mode */ }

      async function load() {
        try { st.view = await api('/api/firewall'); } catch (e) { page.replaceChildren(h('div', { class: 'msg msg--error' }, LL.errText(e))); return; }
        st.pv = Object.keys(st.view.draft.moves || {}).length ? await api('/api/firewall/preview', { method: 'POST', body: {} }).catch(() => null) : null;
        render();
        clearTimeout(st.timer);
        if (st.view.running) st.timer = setTimeout(load, 1500);
      }

      async function setMoves(moves) {
        try { await api('/api/firewall/draft', { method: 'PUT', body: { moves } }); }
        catch (e) { LL.toast(LL.errText(e), 'error'); }
        await load();
      }
      function stage(devId, netId) { const m = Object.assign({}, st.view.draft.moves); m[devId] = netId; return setMoves(m); }
      function unstage(devId) { const m = Object.assign({}, st.view.draft.moves); delete m[devId]; return setMoves(m); }

      // ---------------------------------------------------------------- rendering
      function render() {
        const v = st.view;
        if (!v.state) { page.replaceChildren(head(v), h('div', { class: 'empty-state' }, h('h2', {}, t('fw.nostate.title')), h('p', { class: 'muted' }, t('fw.nostate.text')))); bar.hidden = true; return; }
        const kids = [head(v)];
        if (v.mode === 'simulation') kids.push(h('div', { class: 'callout fw-sim' }, h('strong', {}, t('fw.sim.title')), ' ', t('fw.sim.text'),
          h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: resetSim, disabled: !!v.running }, t('fw.sim.reset'))));
        if (v.quiet) kids.push(h('div', { class: 'callout callout--warn' }, t('fw.quiet.now', { hours: v.quiet_hours })));
        if (v.running) kids.push(progressCard(v.running));
        kids.push(board(v));
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
        const chip = v.mode === 'simulation' ? h('span', { class: 'chip chip--warn' }, t('fw.chip.simulation'))
          : h('span', { class: 'chip ' + (v.executor.connected ? 'chip--ok' : 'chip--danger') }, t(v.executor.connected ? 'fw.chip.connected' : 'fw.chip.offline'));
        return h('div', { class: 'page__head fw-head' },
          h('div', { class: 'fw-head__title' }, h('h1', {}, t('nav.firewall')), h('div', { class: 'fw-head__meta' }, chip, ctrl.label ? h('span', { class: 'muted' }, ctrl.label) : null)),
          h('div', { class: 'fw-head__tools' }, seg, h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => openExecutor(v) }, t('fw.executor.button'))));
      }

      function board(v) {
        const s = v.state;
        const moves = v.draft.moves || {};
        const changes = {};
        (st.pv && st.pv.changes || []).forEach(c => { changes[c.device] = c; });
        const zones = s.zones.map(z => {
          const nets = s.networks.filter(n => n.zone === z.id);
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
        const el = h('div', { class: 'fw-tile' + (locked ? ' is-locked' : '') + (moved ? ' is-new' : '') + (v.requester === d.id ? ' is-self' : ''),
          dataset: { dev: d.id }, title: locked ? t('fw.tile.stays_why', { reason: t(d.protected) }) : d.name },
          st.mode === 'change' && !locked ? svg(ICON.grip, 'fw-tile__grip') : null,
          h('div', { class: 'fw-tile__main' },
            h('div', { class: 'fw-tile__name' }, d.name),
            h('div', { class: 'fw-tile__meta mono' }, moved ? ['→ ', h('b', {}, change.new_ip)] : d.ip)),
          moved ? h('span', { class: 'chip chip--accent fw-tile__badge' }, t('fw.tile.new'))
            : locked ? h('span', { class: 'fw-tile__lock', role: 'img', 'aria-label': t('fw.tile.stays') }, svg(ICON.lock))
              : v.requester === d.id ? h('span', { class: 'chip chip--muted fw-tile__badge' }, t('fw.tile.you')) : null);
        if (st.mode === 'change' && !locked) {
          el.tabIndex = 0;
          el.setAttribute('role', 'button');
          el.setAttribute('aria-label', t('fw.tile.move_aria', { name: d.name }));
          el.draggable = true;
          el.addEventListener('dragstart', ev => { st.drag = d.id; ev.dataTransfer.effectAllowed = 'move'; try { ev.dataTransfer.setData('text/plain', d.id); } catch (e) { /* old engines */ } markTargets(v, d.id); });
          el.addEventListener('dragend', endDrag);
          el.addEventListener('click', () => openMove(v, d));
          el.addEventListener('keydown', ev => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); openMove(v, d); } });
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

      // ---------------------------------------------------------------- draft bar
      function renderBar() {
        const v = st.view;
        const moves = v.draft.moves || {};
        const ids = Object.keys(moves);
        bar.hidden = !ids.length;
        if (!ids.length) { fr.main.classList.remove('has-draftbar'); return; }
        fr.main.classList.add('has-draftbar');
        const s = v.state;
        const chips = ids.map(id => {
          const d = s.devices.find(x => x.id === id) || { name: '?' }; const n = s.networks.find(x => x.id === moves[id]) || { name: '?' };
          return h('button', { type: 'button', class: 'fw-chip', title: t('fw.draft.undo_one', { name: d.name }), onclick: () => unstage(id) }, d.name + ' → ' + n.name, h('span', { 'aria-hidden': 'true' }, ' ✕'));
        });
        bar.replaceChildren(
          h('div', { class: 'fw-draftbar__info' }, h('strong', {}, t(ids.length === 1 ? 'fw.draft.one' : 'fw.draft.many', { n: ids.length })),
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
        const refresh = () => { applyBtn.disabled = pv.blocks.length > 0 || [...needs].some(k => !confirm.has(k)) || (ext && !LL.passkeySupported()); };
        const changes = h('div', { class: 'fw-pv__changes' }, pv.changes.map(c => h('div', { class: 'fw-pv__change' },
          h('div', { class: 'fw-pv__what' }, h('strong', {}, c.name), h('span', { class: 'muted' }, ' · ' + t('fw.preview.from_to', { from: c.from, to: c.to }))),
          h('div', { class: 'mono fw-pv__ip' }, h('s', {}, c.old_ip), ' → ', h('b', {}, c.new_ip)),
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
        const passHint = ext && !LL.passkeySupported() ? h('p', { class: 'msg msg--error' }, t('fw.preview.needs_secure')) : null;
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
          h('p', { class: 'muted' }, job.summary.map(x => x.name + ' → ' + x.to).join(' · ')),
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
            h('span', { class: 'fw-history__what' }, j.summary.map(x => x.name + ' → ' + x.to).join(', '), j.undo_of ? h('span', { class: 'muted' }, ' · ' + t('fw.history.undo_of')) : null),
            h('span', { class: 'muted fw-history__when' }, LL.fmtDate(j.t), j.reason && j.status !== 'done' && LL.strings[j.reason] ? ' · ' + t(j.reason) : ''),
            j.status === 'done' && j.inverse && j.undo_until > now ? h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: async () => {
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
      return { destroy: () => { clearTimeout(st.timer); fr.destroy(); } };
    };
  }

  LL.view('firewall', firewallView());
})();
