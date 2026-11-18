/* Lanloom — one switch for the four faces of the network: Live (the map), Plan (the segmentation plan), Firewall and Data paths.
   It sits at the top of the Network view's filter bar, of the Firewall tab and of the Data paths page, so the faces of one
   topic are one click apart. Also here: the tags of a device (network, wireless name and band) with the list filter that goes with
   them, and the full screen of the map (the board host is moved into an overlay and back, so the live feed keeps running).
   The pure helpers (no DOM) are exported for the Node test (tests/js/netmodes_test.js). */
(function () {
  'use strict';

  // ------------------------------------------------------------------ pure helpers
  const MODES = [['live', '#/network', 'nm.live'], ['plan', '#/firewall/plan', 'nm.plan'], ['firewall', '#/firewall', 'nav.firewall'], ['paths', '#/paths', 'nav.paths']];
  const BANDS = { '2.4ghz': '24', '5ghz': '5', '6ghz': '6' };

  // key of the network a device sits in: its name, else its VLAN number, else none
  function netKey(tags) {
    if (!tags) return '';
    if (tags.net) return 'net:' + tags.net;
    return tags.vlan != null && tags.vlan !== '' ? 'vlan:' + tags.vlan : '';
  }
  // the filter value ("", "wired", "net:<name>", "vlan:<n>", "wireless:<ssid>") that a row of the device list matches
  function matchNet(dev, value) {
    if (!value) return true;
    const tg = dev && dev.tags;
    if (!tg) return false;
    if (value === 'wired') return !tg.ssid;
    if (value.startsWith('wireless:')) return tg.ssid === value.slice(9);
    return netKey(tg) === value;
  }
  // options of the filter in the order they are shown: every network, every wireless network (names sorted, VLAN numbers numerically)
  function netOptions(devices) {
    const nets = new Map(), wireless = new Set();
    let wired = false;
    (devices || []).forEach(d => {
      const tg = d && d.tags;
      if (!tg) return;
      const k = netKey(tg);
      if (k && !nets.has(k)) nets.set(k, { value: k, name: tg.net || '', vlan: tg.vlan != null && tg.vlan !== '' ? Number(tg.vlan) : null });
      if (tg.ssid) wireless.add(tg.ssid); else wired = true;
    });
    const cmp = (a, b) => (a.name && b.name ? a.name.localeCompare(b.name) : a.name ? -1 : b.name ? 1 : (a.vlan || 0) - (b.vlan || 0));
    return { nets: [...nets.values()].sort(cmp), wireless: [...wireless].sort((a, b) => a.localeCompare(b)), wired };
  }
  // a value from the address that no longer matches any option counts as "all"
  function cleanNet(value, opts) {
    if (!value) return '';
    if (value === 'wired') return opts.wired ? value : '';
    if (value.startsWith('wireless:')) return opts.wireless.includes(value.slice(9)) ? value : '';
    return opts.nets.some(n => n.value === value) ? value : '';
  }
  // the chips of one row: [{kind: 'vlan', v, title}, {kind: 'net', name}, {kind: 'wireless', ssid, band}]
  function tagChips(tags) {
    if (!tags) return [];
    const out = [];
    const hasVlan = tags.vlan != null && tags.vlan !== '';
    if (hasVlan) out.push({ kind: 'vlan', v: tags.vlan, title: tags.net || '' });
    else if (tags.net) out.push({ kind: 'net', name: tags.net });
    if (tags.ssid) out.push({ kind: 'wireless', ssid: tags.ssid, band: BANDS[tags.band] || '' });
    return out;
  }
  const PURE = { MODES, BANDS, netKey, matchNet, netOptions, cleanNet, tagChips };
  if (typeof module !== 'undefined' && module.exports) module.exports = PURE;
  if (typeof window === 'undefined' || !window.Lanloom || !window.Lanloom.h) return;

  // ------------------------------------------------------------------ DOM
  const LL = window.Lanloom;
  const { h, t } = LL;
  LL.NetModes = PURE;

  // the switch: four links, the current one marked (aria-current), the look of the other segmented switches
  LL.netModes = function (active) {
    return h('nav', { class: 'seg nm-seg', 'aria-label': t('nm.label') }, MODES.map(([k, href, label]) =>
      h('a', { href, class: 'seg__btn' + (k === active ? ' is-active' : ''), 'aria-current': k === active ? 'page' : null }, t(label))));
  };

  // the icon button that opens the map full screen
  const FULL_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg>';
  LL.fullButton = function (onclick) {
    const b = h('button', { type: 'button', class: 'icon-btn nm-fullbtn', title: t('nm.full.open'), 'aria-label': t('nm.full.open'), onclick });
    b.innerHTML = FULL_ICON;
    return b;
  };

  // small neutral chips for the network, VLAN and wireless of a device (devices list)
  LL.deviceTags = function (dev) {
    const chips = tagChips(dev && dev.tags).map(c => {
      if (c.kind === 'vlan') return h('span', { class: 'chip chip--muted nm-tag', title: c.title || null }, t('devices.tag.vlan', { v: c.v }));
      if (c.kind === 'net') return h('span', { class: 'chip chip--muted nm-tag' }, c.name);
      return h('span', { class: 'chip chip--muted nm-tag nm-tag--wireless' }, c.band ? t('devices.tag.wireless', { ssid: c.ssid, band: t('devices.tag.band.' + c.band) }) : c.ssid);
    });
    return chips.length ? h('span', { class: 'device-row__tags' }, chips) : null;
  };

  // the filter of the devices list: All / each network / each wireless network / Wired only
  LL.deviceNetSelect = function (devices, value, onChange) {
    const o = netOptions(devices);
    if (!o.nets.length && !o.wireless.length) return null;                 // nothing is known about the networks: no filter
    const netLabel = n => (n.name && n.vlan != null ? t('devices.filter.net_vlan', { name: n.name, v: n.vlan }) : n.name || t('devices.tag.vlan', { v: n.vlan }));
    const sel = h('select', { class: 'select dev-net', 'aria-label': t('devices.filter.label'), onchange: () => onChange(sel.value) },
      h('option', { value: '' }, t('devices.filter.all')),
      o.nets.length ? h('optgroup', { label: t('devices.filter.g_net') }, o.nets.map(n => h('option', { value: n.value }, netLabel(n)))) : null,
      o.wireless.length ? h('optgroup', { label: t('devices.filter.g_wireless') }, o.wireless.map(s => h('option', { value: 'wireless:' + s }, t('devices.filter.wireless', { ssid: s })))) : null,
      o.wired ? h('option', { value: 'wired' }, t('devices.filter.wired')) : null);
    sel.value = cleanNet(value, o);
    sel.cleanValue = sel.value;
    return sel;
  };
  LL.cleanDeviceNet = (devices, value) => cleanNet(value, netOptions(devices));

  // ------------------------------------------------------------------ full screen of the map
  /* host: the element that holds the board (it is moved into the overlay and put back where it was); app: the page root (made
     inert while the overlay is open); onClosed(): called after the overlay is gone. open() pushes one history entry so the back key
     (and the Esc key and the close button) leave the full screen; close(after) runs `after` once the overlay is gone. */
  LL.hostFullScreen = function (host, o) {
    o = o || {};
    let f = null;
    const noop = () => {};
    function teardown() {
      if (!f) return;
      const cur = f; f = null;
      document.removeEventListener('keydown', cur.key, true);
      window.removeEventListener('popstate', cur.pop);
      window.removeEventListener('hashchange', cur.pop);
      if (cur.mark.isConnected) cur.mark.replaceWith(host); else if (o.app) o.app.appendChild(host);
      cur.el.remove();
      document.body.classList.remove('nm-noscroll');
      if (o.app) o.app.inert = false;
      (o.onClosed || noop)();
      if (cur.after) cur.after();
    }
    function close(after) {
      if (!f) { if (after) after(); return; }
      if (after) f.after = after;
      if (f.pushed && history.state && history.state.lanloomMapFull) { history.back(); return; }   // popstate tears down
      teardown();
    }
    function open() {
      if (f) return;
      const x = h('button', { type: 'button', class: 'icon-btn nm-full__close', 'aria-label': t('nm.full.close'), title: t('nm.full.close'), onclick: () => close() }, '✕');
      const body = h('div', { class: 'nm-full__body' });
      const el = h('div', { class: 'nm-full', role: 'dialog', 'aria-modal': 'true', 'aria-label': t('nm.full.aria') },
        h('header', { class: 'nm-full__head' }, h('h2', {}, t('nav.network')), x), body);
      const mark = document.createComment('map');
      host.parentNode.insertBefore(mark, host);
      f = { el, mark, pushed: false, after: null };
      f.key = ev => { if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); close(); } };
      f.pop = () => teardown();
      document.addEventListener('keydown', f.key, true);
      window.addEventListener('popstate', f.pop);
      window.addEventListener('hashchange', f.pop);
      try { history.pushState({ lanloomMapFull: 1 }, '', location.href); f.pushed = true; } catch (e) { /* Esc and the button still close */ }
      body.appendChild(host);
      document.body.appendChild(el);
      document.body.classList.add('nm-noscroll');
      if (o.app) o.app.inert = true;
      x.focus();
    }
    return { open, close, isOpen: () => !!f, destroy: () => { if (f) { f.pushed = false; teardown(); } } };
  };
})();
