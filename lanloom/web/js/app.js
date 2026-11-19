/* Lanloom — app shell: API helper, translations, theme, routing, sign-in. */
(function () {
  'use strict';
  const LL = window.Lanloom = window.Lanloom || {};

  // ------------------------------------------------------------------ DOM helper
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (v === true) el.setAttribute(k, '');
      else el.setAttribute(k, v);
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid == null || kid === false) continue;
      el.appendChild(typeof kid === 'string' || typeof kid === 'number' ? document.createTextNode(String(kid)) : kid);
    }
    return el;
  }
  LL.h = h;

  // ------------------------------------------------------------------ deep link survives the sign-in (O9)
  // A signed-out visitor who opens a deep link (#/devices/<id>, #/reports/<day> ...) or whose session ends in the middle of the
  // work is sent to the sign-in. The tab remembers where they wanted to go - only the hash of this very page, only a known view,
  // short, valid for ten minutes, usable once - and brings them there after the sign-in (password, second step, passkey,
  // required enrolment). The hash never reaches the server, so there is no redirect target that someone else could set.
  const RETURN_KEY = 'lanloom.return', RETURN_MAX_MS = 10 * 60 * 1000, RETURN_MAX_LEN = 200;
  const AUTH_VIEWS = ['login', 'setup', 'invite', 'enroll'];
  let signedOut = false;        // after an explicit sign-out nothing is remembered until the next sign-in
  let pendingNotice = null;     // shown once the view after the sign-in is drawn (in the language of the account)
  function parseReturn(hash) {
    if (typeof hash !== 'string' || hash.length > RETURN_MAX_LEN || !/^#\/[\w.~%:@,;=&+\/?-]*$/.test(hash)) return null;
    const raw = hash.slice(2), qpos = raw.indexOf('?');
    const parts = (qpos >= 0 ? raw.slice(0, qpos) : raw).split('/');
    return { hash, name: parts[0], parts, plain: qpos < 0 && parts.length === 1 };
  }
  // a hash worth remembering: a known view of the app that is not part of the sign-in itself
  function knownView(p) { return !!p && !!p.name && !AUTH_VIEWS.includes(p.name) && !!views[p.name]; }
  function rememberReturn(hash) {
    if (signedOut) return;
    const p = parseReturn(hash);
    if (!knownView(p) || (p.name === 'overview' && p.plain)) return;
    try { sessionStorage.setItem(RETURN_KEY, JSON.stringify({ h: hash, t: Date.now() })); } catch (e) { /* storage closed: no return, the overview as before */ }
  }
  function clearReturn() { try { sessionStorage.removeItem(RETURN_KEY); } catch (e) { /* ignore */ } }
  function takeReturn() {          // usable once: reading deletes
    let v = null;
    try { v = JSON.parse(sessionStorage.getItem(RETURN_KEY) || 'null'); } catch (e) { v = null; }
    clearReturn();
    if (!v || typeof v.h !== 'string' || typeof v.t !== 'number') return null;
    const age = Date.now() - v.t;
    return age >= 0 && age <= RETURN_MAX_MS ? v.h : null;
  }
  // the lowest role a deep link needs (the same rule the navigation and the palette follow; the server decides anyway)
  LL.routeRole = function (hash) {
    const p = parseReturn(hash);
    if (!p) return 'viewer';
    const sub = p.parts[1];
    if (p.name === 'sites') return 'admin';
    if (p.name === 'settings') {
      if (sub === 'reports') return 'operate';
      const page = (LL.settingsPages || []).find(x => x[1] && x[1] === sub);
      return page ? (page[3] === 'admin' ? 'admin' : (page[3] === 'viewer' ? 'viewer' : 'operate')) : 'viewer';
    }
    if (p.name === 'firewall' && ['pick', 'networks', 'forwards', 'reservations'].includes(sub)) return 'operate';
    return 'viewer';
  };

  // ------------------------------------------------------------------ API
  // Every address is relative to the page, so Lanloom also works below a sub-path of a reverse proxy.
  LL.base = location.pathname.replace(/[^/]*$/, '') || '/';
  LL.url = path => (path.charAt(0) === '/' ? LL.base + path.slice(1) : path);
  // Every request has a deadline (O10): a server, proxy or network that never answers must not hold a view for ever. 20 s for
  // everything; the places that really work long (AI, update, backup, export/import, vulnerability feed, clean-up, scans,
  // tests of connections) get 120 s, and a caller can set its own with opts.timeout. An expired deadline is a calm error
  // ('error.timeout', e.timeout), never a crash; it is never repeated quietly (a change may have reached the server).
  const API_TIMEOUT_MS = 20000, API_LONG_TIMEOUT_MS = 120000;
  const LONG_PATHS = /^\/api\/(ai|backup|export|vulns|storage|update|integrations|mqtt-out|notify|agents\/proxy|scan|reports\/test|diagnostics)(\/|\?|$)/;
  LL.apiTimeout = (path, timeout) => timeout > 0 ? timeout : (LONG_PATHS.test(path) ? API_LONG_TIMEOUT_MS : API_TIMEOUT_MS);
  function timedOut(ms) {
    const err = new Error('timed out');
    err.status = 0; err.key = 'error.timeout'; err.timeout = true; err.after = ms;
    return err;
  }
  // The site of this page (M2). The cookie that remembers the last site is shared by every tab of the browser, so it cannot
  // say which site THIS page shows: every request carries the page's own site in a header, the server names the site it
  // answered for in the same header, and an answer for any other site is thrown away. A page has one site for its whole
  // life; switching reloads it (after every running request was cancelled).
  const TAB_SITE = 'lanloom.site.tab', RESYNC_KEY = 'lanloom.site.resync', SITE_RE = /^(home|s-[0-9a-f]{8})$/;
  const inflight = new Set();                 // cancel handles of the requests that are running
  let switching = false;                      // a switch or a resync is on its way: nothing that is still running is drawn or counted
  const never = () => new Promise(() => {});
  function tabSite() {
    let v = null;
    try { v = sessionStorage.getItem(TAB_SITE) || localStorage.getItem('ll-site'); } catch (e) { /* storage closed */ }
    return v && SITE_RE.test(v) ? v : null;
  }
  LL.pageSite = () => { const s = LL.state && LL.state.site; return (s && s.id) || tabSite(); };
  LL.switching = () => switching;
  // key of the browser storage that belongs to the site of this page (the home site keeps the plain key)
  LL.siteKey = base => { const id = LL.pageSite(); return id && id !== 'home' ? base + '@' + id : base; };
  // deep link into a site: '#/devices/d-1' -> '#/devices/d-1?site=s-12345678' (the page switches before it draws)
  LL.siteHash = (hash, id) => id && id !== LL.pageSite() ? hash + (hash.indexOf('?') >= 0 ? '&' : '?') + 'site=' + encodeURIComponent(id) : hash;
  LL.rememberTabSite = id => { try { sessionStorage.setItem(TAB_SITE, id); } catch (e) { /* storage closed */ } };
  // everything that belongs to the site that is left stops: running requests are cancelled and never settle (a cancelled
  // request is no failure), the live feed is closed without a message
  LL.leaveSite = function () {
    switching = true;
    inflight.forEach(c => { try { c.abort(); } catch (e) { /* already done */ } });
    inflight.clear();
    if (LL.live && LL.live.shutdown) LL.live.shutdown();
  };
  // the server answered for (or told us about) another site than this page shows: follow it once, never in a loop
  LL.siteResync = async function (actual) {
    if (switching) return;
    let at = 0;
    try { at = Number(sessionStorage.getItem(RESYNC_KEY)) || 0; } catch (e) { /* storage closed */ }
    if (Date.now() - at < 8000) return;        // just done: the message stays, no reload loop
    if (!actual) {
      try { const st = await api('/api/state', { allow401: true, noSite: true }); actual = st && st.site && st.site.id; } catch (e) { return; }
    }
    if (!actual || actual === LL.pageSite()) return;
    try { sessionStorage.setItem(RESYNC_KEY, String(Date.now())); } catch (e) { /* storage closed */ }
    LL.leaveSite();
    LL.rememberTabSite(actual);
    try { localStorage.setItem('ll-site', actual); } catch (e) { /* private mode */ }
    location.reload();
  };
  async function api(path, opts) {
    opts = opts || {};
    const init = { method: opts.method || 'GET', credentials: 'same-origin', cache: 'no-store', headers: { 'Accept': 'application/json' } };
    if (init.method !== 'GET') init.headers['X-Lanloom'] = '1';
    const asked = opts.noSite ? null : LL.pageSite();
    if (asked) init.headers['X-Lanloom-Site'] = asked;
    if (switching) return never();
    if (opts.body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.body); }
    const ctl = typeof AbortController === 'function' ? new AbortController() : null;
    const limit = LL.apiTimeout(path, opts.timeout);
    const timer = ctl ? setTimeout(() => ctl.abort(), limit) : 0;
    if (ctl) { init.signal = ctl.signal; inflight.add(ctl); }
    let r, data = {};
    try {
      r = await fetch(LL.url(path), init);
      try { data = await r.json(); } catch (e) { if (ctl && ctl.signal.aborted) throw e; /* empty body */ }
    } catch (e) {
      if (switching) return never();           // cancelled by the switch: not a failure, nothing is shown
      if (ctl && ctl.signal.aborted) throw timedOut(limit);
      throw e;
    } finally { clearTimeout(timer); if (ctl) inflight.delete(ctl); }
    if (switching) return never();             // an answer that arrives while the page leaves its site is dropped
    const got = r.headers.get('X-Lanloom-Site');
    if (asked && got && got !== asked && path !== '/api/state') {
      // an answer made for another site than this page shows (the access changed, the site is gone): never drawn
      LL.siteResync(got);
      const err = new Error('site.err.changed'); err.status = 409; err.key = 'site.err.changed'; err.site = got;
      throw err;
    }
    if (!r.ok) {
      // critical actions ask for a fresh confirmation (passkey, one-time code or password) and are then sent once more
      if (r.status === 403 && data.error === 'auth.err.stepup' && !opts.noStepUp && LL.stepUp) {
        if (await LL.stepUp(data.methods || [])) return api(path, Object.assign({}, opts, { noStepUp: true }));
      }
      const err = new Error(data.error || data.detail || ('HTTP ' + r.status));
      err.status = r.status; err.key = data.error || data.detail; err.wait = data.wait; err.vars = data.vars;
      if (r.status === 401 && !opts.allow401) { rememberReturn(location.hash); LL.go('#/login'); }   // session over in the middle of the work: come back to this view
      if (r.status === 403 && data.error === 'auth.err.enroll') LL.go('#/enroll');
      throw err;
    }
    if (init.method === 'POST' && path === '/api/logout') { signedOut = true; clearReturn(); }
    return data;
  }
  LL.api = api;
  // what the signed-in role may use (only for showing controls - the server decides)
  LL.can = what => !!(LL.state && LL.state.can && LL.state.can[what]);

  // ------------------------------------------------------------------ translations
  LL.strings = {};
  LL.lang = 'en';
  function t(key, vars) {
    let s = LL.strings[key];
    if (s == null) s = key;
    return LL.Text.fill(s, vars, LL.lang);
  }
  LL.t = t;
  LL.errText = function (e) {
    if (!e) return t('error.generic');
    if (e.status === 429) return t('auth.err.rate', { s: e.wait || 60 });
    if (e.key && LL.strings[e.key]) return t(e.key, e.vars);
    if (e.name === 'NotAllowedError') return t('passkey.err.cancelled');
    return t('error.generic');
  };
  // The languages of the app: code and the name in that language (a reader finds theirs in any language). Keep in step with
  // lanloom/i18n/<code>.json (tests/test_i18n_complete.py checks it).
  LL.LANGUAGES = [['en', 'English'], ['de', 'Deutsch']];
  LL.langOptions = () => LL.LANGUAGES.map(([code, name]) => h('option', { value: code }, name));
  const knownLang = code => (LL.LANGUAGES.some(l => l[0] === code) ? code : null);
  async function loadLang(lang) {
    lang = knownLang(lang) || 'en';
    LL.strings = await api('/api/i18n/' + lang);
    LL.lang = lang;
    document.documentElement.lang = lang;
    try { localStorage.setItem('ll-lang', lang); } catch (e) { /* private mode */ }
  }
  LL.loadLang = loadLang;
  LL.pickLang = function (serverLang) {
    let stored = null;
    try { stored = localStorage.getItem('ll-lang'); } catch (e) { /* ignore */ }
    if (knownLang(serverLang)) return serverLang;
    if (knownLang(stored)) return stored;
    for (const tag of (navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language || 'en'])) {
      const code = knownLang(String(tag).toLowerCase().split('-')[0]);
      if (code) return code;
    }
    return 'en';
  };

  // ------------------------------------------------------------------ theme
  LL.theme = function () {
    let v = 'auto';
    try { v = localStorage.getItem('ll-theme') || 'auto'; } catch (e) { /* ignore */ }
    return v;
  };
  LL.effectiveTheme = function () {
    const v = LL.theme();
    if (v !== 'auto') return v;
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  };
  LL.setTheme = function (v) {
    try { localStorage.setItem('ll-theme', v); } catch (e) { /* ignore */ }
    document.documentElement.dataset.theme = v;
    document.dispatchEvent(new CustomEvent('ll-theme', { detail: LL.effectiveTheme() }));
  };
  document.documentElement.dataset.theme = LL.theme();

  // ------------------------------------------------------------------ colour theme (accent palette, remembered per device)
  // The palettes are defined in css/palettes.css (the default one lives in tokens.css); this list only names them.
  LL.PALETTES = ['circuit', 'aurora'];
  LL.palette = function () {
    let v = 'circuit';
    try { v = localStorage.getItem('ll-palette') || 'circuit'; } catch (e) { /* ignore */ }
    return LL.PALETTES.includes(v) ? v : 'circuit';
  };
  // the browser chrome (address bar on phones) follows the page ground of the chosen palette and mode
  LL.paintChrome = function () {
    const bg = getComputedStyle(document.documentElement).getPropertyValue('--ll-bg').trim();
    if (bg) document.querySelectorAll('meta[name="theme-color"]').forEach(m => { m.setAttribute('content', bg); });
  };
  LL.setPalette = function (v) {
    if (!LL.PALETTES.includes(v)) v = 'circuit';
    try { localStorage.setItem('ll-palette', v); } catch (e) { /* ignore */ }
    document.documentElement.dataset.palette = v;
    LL.paintChrome();
    document.dispatchEvent(new CustomEvent('ll-theme', { detail: LL.effectiveTheme() }));   // the board re-reads its colours
  };
  document.documentElement.dataset.palette = LL.palette();
  LL.paintChrome();
  document.addEventListener('ll-theme', LL.paintChrome);
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
      if (LL.theme() === 'auto') document.dispatchEvent(new CustomEvent('ll-theme', { detail: LL.effectiveTheme() }));
    });
  }

  // ------------------------------------------------------------------ formatting
  // BCP 47 tag of the page language: numbers, dates and times follow it (24-hour clock, decimal comma, ...)
  const LOCALES = { en: 'en-GB', de: 'de-DE' };   // regional variant per language (24-hour clock, day.month order, ...)
  LL.locale = () => LOCALES[LL.lang] || LL.lang;
  // a number the way the language writes it; digits fixes the fraction digits, otherwise up to two
  LL.fmtNum = (n, digits) => LL.Text.num(LL.locale(), n, digits);
  // one decimal below 10, none above: 1.2 MB, 12 MB (decimal comma in German)
  const sized = (v, u) => LL.fmtNum(v, v >= 10 ? 0 : 1) + '\u00a0' + u;
  LL.fmtBytes = function (n) {
    n = Number(n) || 0;
    const units = [['TB', 1e12], ['GB', 1e9], ['MB', 1e6], ['kB', 1e3]];
    for (const [u, f] of units) if (n >= f) return sized(n / f, u);
    return LL.fmtNum(Math.round(n), 0) + '\u00a0B';
  };
  LL.rateUnit = 'bytes';                                   // 'bytes' | 'bits' (server setting rate_unit)
  LL.fmtBits = function (n) {
    n = (Number(n) || 0) * 8;
    const units = [['Gbit', 1e9], ['Mbit', 1e6], ['kbit', 1e3]];
    for (const [u, f] of units) if (n >= f) return sized(n / f, u);
    return LL.fmtNum(Math.round(n), 0) + '\u00a0bit';
  };
  LL.fmtRate = n => (LL.rateUnit === 'bits' ? LL.fmtBits(n) : LL.fmtBytes(n)) + '/s';
  LL.fmtAge = function (s) {
    if (s == null) return '—';
    s = Math.max(0, Math.round(s));
    if (s < 90) return t('time.seconds', { n: s });
    if (s < 5400) return t('time.minutes', { n: Math.round(s / 60) });
    if (s < 172800) return t('time.hours', { n: Math.round(s / 3600) });
    return t('time.days', { n: Math.round(s / 86400) });
  };
  LL.fmtDate = function (ts) {
    if (!ts) return '—';
    try { return new Date(ts * 1000).toLocaleString(LL.locale(), { dateStyle: 'medium', timeStyle: 'short', hour12: false }); }
    catch (e) { return new Date(ts * 1000).toISOString(); }
  };

  // ------------------------------------------------------------------ passkeys
  const b64 = buf => { let s = ''; const b = new Uint8Array(buf); for (let i = 0; i < b.length; i++) s += String.fromCharCode(b[i]); return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, ''); };
  const unb64 = s => { s = s.replace(/-/g, '+').replace(/_/g, '/'); while (s.length % 4) s += '='; const r = atob(s), b = new Uint8Array(r.length); for (let i = 0; i < r.length; i++) b[i] = r.charCodeAt(i); return b.buffer; };
  LL.b64 = b64; LL.unb64 = unb64;
  function credJson(c) {
    const r = c.response;
    const out = { id: c.id, rawId: b64(c.rawId), type: c.type, clientExtensionResults: c.getClientExtensionResults ? c.getClientExtensionResults() : {}, response: { clientDataJSON: b64(r.clientDataJSON) } };
    if (c.authenticatorAttachment) out.authenticatorAttachment = c.authenticatorAttachment;
    if (r.attestationObject) { out.response.attestationObject = b64(r.attestationObject); if (r.getTransports) { try { out.response.transports = r.getTransports(); } catch (e) { /* old */ } } }
    else { out.response.authenticatorData = b64(r.authenticatorData); out.response.signature = b64(r.signature); if (r.userHandle) out.response.userHandle = b64(r.userHandle); }
    return out;
  }
  LL.passkeySupported = () => !!(window.isSecureContext && window.PublicKeyCredential && navigator.credentials);
  LL.passkeyRegister = async function (name) {
    const o = await api('/api/passkey/register/options', { method: 'POST', body: {} });
    const opt = Object.assign({}, o.options, { challenge: unb64(o.options.challenge), user: Object.assign({}, o.options.user, { id: unb64(o.options.user.id) }) });
    if (opt.excludeCredentials) opt.excludeCredentials = opt.excludeCredentials.map(c => Object.assign({}, c, { id: unb64(c.id) }));
    const cred = await navigator.credentials.create({ publicKey: opt });
    return api('/api/passkey/register/verify', { method: 'POST', body: { ceremony: o.ceremony, credential: credJson(cred), name: name || '' } });
  };
  LL.passkeyAssert = async function (o) {
    const opt = Object.assign({}, o.options, { challenge: unb64(o.options.challenge) });
    if (opt.allowCredentials) opt.allowCredentials = opt.allowCredentials.map(c => Object.assign({}, c, { id: unb64(c.id) }));
    return credJson(await navigator.credentials.get({ publicKey: opt }));
  };
  LL.passkeyLogin = async function (extra) {
    const o = await api('/api/passkey/login/options', { method: 'POST', body: {} });
    const credential = await LL.passkeyAssert(o);
    return api('/api/passkey/login/verify', { method: 'POST', body: Object.assign({ ceremony: o.ceremony, credential }, extra || {}) });
  };
  LL.deviceName = function () {
    const u = navigator.userAgent || '';
    if (/Mobi|Android|iPhone/.test(u)) return t('passkey.name.phone');   // identifier: user-agent tokens
    if (/iPad|Tablet/.test(u)) return t('passkey.name.tablet');   // identifier: user-agent tokens
    return t('passkey.name.computer');
  };

  // ------------------------------------------------------------------ shared bits
  LL.logo = function (size) {
    size = size || 28;
    const wrap = h('span', { class: 'brand-mark', 'aria-hidden': 'true' });
    wrap.innerHTML = '<svg width="' + size + '" height="' + size + '" viewBox="0 0 32 32" fill="none"><rect x="1.5" y="1.5" width="29" height="29" rx="8" class="bm-bg"/>' +
      '<path d="M8 10h6l4 4h6M8 16h16M8 22h6l4-4h6" class="bm-trace"/><circle cx="8" cy="10" r="2" class="bm-pad"/><circle cx="8" cy="16" r="2" class="bm-pad"/><circle cx="8" cy="22" r="2" class="bm-pad"/>' +
      '<circle cx="24" cy="14" r="2" class="bm-pad"/><circle cx="24" cy="16" r="0" /><circle cx="24" cy="18" r="2" class="bm-pad"/></svg>';
    return wrap;
  };
  LL.toast = function (text, kind) {
    let host = document.querySelector('.toasts');
    if (!host) { host = h('div', { class: 'toasts', role: 'status' }); document.body.appendChild(host); }
    const el = h('div', { class: 'toast toast--' + (kind || 'info') }, text);
    host.appendChild(el);
    setTimeout(() => { el.classList.add('is-out'); setTimeout(() => el.remove(), 300); }, 4200);
  };
  LL.field = function (label, input, hint) {
    // a wrapper around the control (password field with a clear button, ...) hands the label to the control inside it
    const ctl = /^(INPUT|SELECT|TEXTAREA|BUTTON)$/.test(input.tagName) ? input : (input.querySelector('input:not([type=hidden]),select,textarea') || input);
    const id = ctl.id || ('f' + Math.random().toString(36).slice(2, 9));
    ctl.id = id;
    return h('div', { class: 'field' }, h('label', { for: id, class: 'field__label' }, label), input, hint ? h('div', { class: 'field__hint' }, hint) : null);
  };
  LL.msg = function (el, text, kind) { el.textContent = text || ''; el.className = 'msg' + (text ? ' msg--' + (kind || 'info') : ''); };

  // ------------------------------------------------------------------ command palette registers
  // Commands (js/palette.js shows them): {id, label, keywords?, group?, available?(), run(), order?}; label and keywords are
  // translation keys or functions returning text. Actions are the choices of a search hit: {id, kinds: ['device', ...], label,
  // available?(hit), run(hit), order?}. A view or feature registers its own entries, so nothing appears before it exists.
  LL.commands = LL.commands || [];
  LL.registerCommand = LL.registerCommand || function (cmd) {
    LL.commands = LL.commands.filter(c => c.id !== cmd.id).concat([cmd]);
  };
  LL.actions = LL.actions || [];
  LL.registerAction = function (act) {
    LL.actions = LL.actions.filter(a => a.id !== act.id).concat([act]);
  };

  // ------------------------------------------------------------------ routing
  LL.state = null;
  LL.go = function (hash) { if (location.hash !== hash) location.hash = hash; else route(); };
  const views = {};
  LL.view = function (name, fn) { views[name] = fn; };
  let current = null;

  let routeSeq = 0;
  async function route() {
    if (LL.holdRoute && LL.holdRoute()) return;     // a sheet closed itself with the back step: nothing below changes
    const seq = ++routeSeq;
    const app = document.getElementById('app');
    // #/<view>/<part>...?<query>: the query carries view state (map area filter, device list filter ...)
    const raw = (location.hash || '#/').replace(/^#\/?/, '');
    const qpos = raw.indexOf('?');
    LL.query = new URLSearchParams(qpos >= 0 ? raw.slice(qpos + 1) : '');
    const [name, ...rest] = (qpos >= 0 ? raw.slice(0, qpos) : raw).split('/');
    let st;
    const before = LL.state && LL.state.site && LL.state.site.id;
    try { st = LL.state = await api('/api/state', { allow401: true }); if (st && st.rate_unit) LL.rateUnit = st.rate_unit === 'bits' ? 'bits' : 'bytes'; }
    catch (e) { if (switching) return; app.replaceChildren(h('div', { class: 'boot-error' }, t('error.offline'))); return; }
    if (switching || seq !== routeSeq) return;
    if (st && st.site && st.site.id) {          // M2: the site of this page is the one the server answered for
      if (before && before !== st.site.id && LL.siteResync) { LL.siteResync(st.site.id); return; }     // the access changed while the page was open
      LL.rememberTabSite(st.site.id);
    }
    if (!Object.keys(LL.strings).length || (st.language && st.language !== LL.lang)) await loadLang(LL.pickLang(st.language));
    let target = name || 'overview';
    if (st.setup_needed) target = 'setup';
    else if (target === 'invite' && !st.authenticated) target = 'invite';      // invitation or reset link, opened signed out
    else if (!st.authenticated) target = 'login';
    else if (st.enroll) target = 'enroll';                                      // second factor required: set it up first
    else if (!st.setup_done) target = 'setup';
    else if (target === 'setup' && !st.demo_view && LL.can('admin') && LL.setupAgainActive && LL.setupAgainActive()) target = 'setup';   // Settings > Setup: run again
    else if (target === 'login' || target === 'setup' || target === 'invite' || target === 'enroll') target = 'overview';
    if (seq !== routeSeq) return;            // a newer navigation started meanwhile
    if (st.authenticated) signedOut = false;
    if ((target === 'login' || target === 'enroll') && name !== target && !st.setup_needed) rememberReturn(location.hash);   // O9: deep link before the sign-in
    if (target !== name) history.replaceState(null, '', LL.base + '#/' + target);
    // a deep link into another site (#/devices/<id>?site=<id>, M2) switches before anything is drawn, also from a cold start
    if (st.authenticated && LL.query.has('site') && LL.siteDeepLink && await LL.siteDeepLink()) return;
    if (seq !== routeSeq) return;
    if (current && current.destroy) { try { current.destroy(); } catch (e) { /* ignore */ } }
    current = null;
    const fn = views[target] || views.map;
    current = await fn(app, rest, st) || null;
    if (pendingNotice && st.authenticated && seq === routeSeq) { LL.toast(t(pendingNotice)); pendingNotice = null; }
  }
  LL.route = route;
  // Deep link of a state inside a view (selected component, opened report, filter): the address follows, the back key
  // returns to the state before (the browser fires hashchange, the view is drawn again from the address).
  LL.deepLink = function (hash, replace) {
    if (location.hash === hash || (!location.hash && hash === '#/')) return;
    const url = LL.base + hash;
    if (replace) history.replaceState(null, '', url); else history.pushState(null, '', url);
  };
  // the query of a hash ('' or '?a=b'), e.g. to keep the area filter when a component card opens
  LL.queryString = () => { const q = LL.query ? LL.query.toString() : ''; return q ? '?' + q : ''; };
  window.addEventListener('hashchange', route);
  window.addEventListener('DOMContentLoaded', route);

  // ------------------------------------------------------------------ sign-in page
  function authPage(app, ...kids) {
    app.replaceChildren(h('main', { class: 'auth-page' },
      h('div', { class: 'auth-card card' },
        h('div', { class: 'auth-card__brand' }, LL.logo(36), h('div', {}, h('div', { class: 'brand-name' }, 'Lanloom'), h('div', { class: 'muted' }, t('app.tagline')))),
        ...kids),
      LL.footer()));
  }
  LL.authPage = authPage;
  // After the sign-in: the remembered deep link (once), unless the account is on a public computer or the link came with an
  // invitation; a view the role may not use or that is gone leads to the overview with a quiet notice.
  // opts: {publicComputer, plain}
  function afterSignIn(res, opts) {
    opts = opts || {};
    signedOut = false;
    if (res && res.enroll) { LL.go('#/enroll'); return; }           // the remembered link waits for the enrolment
    finishSignIn(opts);
  }
  async function finishSignIn(opts) {
    let dest = takeReturn();
    if (opts.plain || opts.publicComputer) dest = null;
    if (dest) {
      try {
        LL.state = await api('/api/state', { allow401: true });       // the rights of the account that just signed in
        const p = parseReturn(dest), need = LL.routeRole(dest);
        if (!knownView(p) || (need !== 'viewer' && !LL.can(need === 'admin' ? 'admin' : 'operate'))) {
          pendingNotice = knownView(p) ? 'login.return.denied' : 'login.return.gone';
          dest = null;
        }
      } catch (e) { dest = null; }
    }
    LL.go(dest || '#/overview');
  }
  LL.afterSignIn = afterSignIn;

  LL.view('login', async function (app) {
    const user = h('input', { type: 'text', autocomplete: 'username', autocapitalize: 'none', spellcheck: 'false', required: true });
    const pass = h('input', { type: 'password', autocomplete: 'current-password', required: true });
    const pub = h('input', { type: 'checkbox', class: 'login-public' });
    const msg = h('div', { class: 'msg' });
    const submit = h('button', { class: 'btn btn--primary btn--block', type: 'submit' }, t('login.submit'));
    const form = h('form', { class: 'stack', onsubmit: async (ev) => {
      ev.preventDefault(); submit.disabled = true; LL.msg(msg, '');
      try {
        const res = await api('/api/login', { method: 'POST', body: { username: user.value, password: pass.value, public_computer: pub.checked }, allow401: true });
        if (res.second) { secondStep(app, res.second, pub.checked); return; }
        afterSignIn(res, { publicComputer: pub.checked });
      } catch (e) { LL.msg(msg, LL.errText(e), 'error'); submit.disabled = false; pass.select(); }
    } }, LL.field(t('login.username'), user), LL.field(t('login.password'), pass),
      h('label', { class: 'check' }, pub, h('span', {}, t('login.public'), h('span', { class: 'field__hint' }, t('login.public.hint')))), msg, submit);
    pub.addEventListener('change', () => { user.setAttribute('autocomplete', pub.checked ? 'off' : 'username'); });
    const pk = LL.passkeySupported() ? h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: async () => {
      LL.msg(msg, '');
      try { afterSignIn(await LL.passkeyLogin({ public_computer: pub.checked }), { publicComputer: pub.checked }); } catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
    } }, t('login.passkey')) : null;
    authPage(app, h('h1', { class: 'auth-card__title' }, t('login.title')), form,
      pk ? h('div', { class: 'divider' }, h('span', {}, t('login.or'))) : null, pk,
      h('p', { class: 'auth-card__foot muted' }, t('login.forgot')));
    setTimeout(() => { if (document.activeElement === document.body) user.focus(); }, 30);
  });

  // second step: one-time code from the app, a recovery code, or a passkey
  function secondStep(app, second, publicComputer) {
    let kind = second.methods.includes('totp') ? 'totp' : (second.methods.includes('recovery') ? 'recovery' : 'passkey');
    const code = h('input', { type: 'text', inputmode: 'numeric', autocomplete: 'one-time-code', autocapitalize: 'none', spellcheck: 'false', maxlength: '20', class: 'input code-input' });
    const label = h('label', { class: 'field__label', for: 'second-code' }, '');
    code.id = 'second-code';
    const msg = h('div', { class: 'msg' });
    const submit = h('button', { class: 'btn btn--primary btn--block', type: 'submit' }, t('login.second.submit'));
    const lead = h('p', { class: 'muted' }, '');
    const form = h('form', { class: 'stack', onsubmit: async ev => {
      ev.preventDefault(); submit.disabled = true; LL.msg(msg, '');
      try { afterSignIn(await api('/api/login/second', { method: 'POST', body: { ticket: second.ticket, kind, code: code.value }, allow401: true }), { publicComputer }); }
      catch (e) {
        submit.disabled = false; code.select();
        if (e.key === 'auth.err.ticket') { LL.msg(msg, t('auth.err.ticket'), 'error'); setTimeout(() => LL.go('#/login'), 1800); }
        else LL.msg(msg, LL.errText(e), 'error');
      }
    } }, lead, h('div', { class: 'field' }, label, code), msg, submit);
    const sw = h('div', { class: 'second-switch' });
    function show() {
      form.hidden = kind === 'passkey';
      lead.textContent = t('login.second.lead.' + kind);
      label.textContent = t('login.second.label.' + kind);
      code.setAttribute('inputmode', kind === 'totp' ? 'numeric' : 'text');
      code.value = ''; LL.msg(msg, '');
      sw.replaceChildren(...second.methods.filter(m => m !== kind && (m !== 'passkey' || LL.passkeySupported())).map(m =>
        h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => { if (m === 'passkey') usePasskey(); else { kind = m; show(); code.focus(); } } }, t('login.second.use.' + m))));
    }
    async function usePasskey() {
      LL.msg(msg, '');
      try { afterSignIn(await LL.passkeyLogin({ ticket: second.ticket, public_computer: publicComputer }), { publicComputer }); } catch (e) { form.hidden = false; LL.msg(msg, LL.errText(e), 'error'); }
    }
    authPage(app, h('h1', { class: 'auth-card__title' }, t('login.second.title')), form, sw,
      h('p', { class: 'auth-card__foot muted' }, h('a', { href: '#/login', onclick: ev => { ev.preventDefault(); LL.route(); } }, t('login.second.back'))));
    show();
    if (kind === 'passkey' && LL.passkeySupported()) usePasskey(); else setTimeout(() => code.focus(), 30);
  }

  LL.footer = function () {
    return h('footer', { class: 'page-foot muted' }, 'Lanloom ' + ((LL.state && LL.state.version) || ''), h('span', { class: 'dot-sep' }, '·'), t('app.readonly'));
  };
})();
