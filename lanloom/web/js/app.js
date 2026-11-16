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
    for (const kid of kids.flat()) {
      if (kid == null || kid === false) continue;
      el.appendChild(typeof kid === 'string' || typeof kid === 'number' ? document.createTextNode(String(kid)) : kid);
    }
    return el;
  }
  LL.h = h;

  // ------------------------------------------------------------------ API
  async function api(path, opts) {
    opts = opts || {};
    const init = { method: opts.method || 'GET', credentials: 'same-origin', cache: 'no-store', headers: { 'Accept': 'application/json' } };
    if (init.method !== 'GET') init.headers['X-Lanloom'] = '1';
    if (opts.body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.body); }
    const r = await fetch(path, init);
    let data = {};
    try { data = await r.json(); } catch (e) { /* empty body */ }
    if (!r.ok) {
      const err = new Error(data.error || data.detail || ('HTTP ' + r.status));
      err.status = r.status; err.key = data.error || data.detail; err.wait = data.wait;
      if (r.status === 401 && !opts.allow401) LL.go('#/login');
      throw err;
    }
    return data;
  }
  LL.api = api;

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
    if (e.key && LL.strings[e.key]) return t(e.key);
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
    const units = [['GB', 1e9], ['MB', 1e6], ['kB', 1e3]];
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
  LL.passkeyLogin = async function () {
    const o = await api('/api/passkey/login/options', { method: 'POST', body: {} });
    const opt = Object.assign({}, o.options, { challenge: unb64(o.options.challenge) });
    if (opt.allowCredentials) opt.allowCredentials = opt.allowCredentials.map(c => Object.assign({}, c, { id: unb64(c.id) }));
    const cred = await navigator.credentials.get({ publicKey: opt });
    return api('/api/passkey/login/verify', { method: 'POST', body: { ceremony: o.ceremony, credential: credJson(cred) } });
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
    const id = input.id || ('f' + Math.random().toString(36).slice(2, 9));
    input.id = id;
    return h('div', { class: 'field' }, h('label', { for: id, class: 'field__label' }, label), input, hint ? h('div', { class: 'field__hint' }, hint) : null);
  };
  LL.msg = function (el, text, kind) { el.textContent = text || ''; el.className = 'msg' + (text ? ' msg--' + (kind || 'info') : ''); };

  // ------------------------------------------------------------------ routing
  LL.state = null;
  LL.go = function (hash) { if (location.hash !== hash) location.hash = hash; else route(); };
  const views = {};
  LL.view = function (name, fn) { views[name] = fn; };
  let current = null;

  let routeSeq = 0;
  async function route() {
    const seq = ++routeSeq;
    const app = document.getElementById('app');
    const hash = location.hash || '#/';
    const [name, ...rest] = hash.replace(/^#\/?/, '').split('/');
    let st;
    try { st = LL.state = await api('/api/state', { allow401: true }); if (st && st.rate_unit) LL.rateUnit = st.rate_unit === 'bits' ? 'bits' : 'bytes'; }
    catch (e) { app.replaceChildren(h('div', { class: 'boot-error' }, t('error.offline'))); return; }
    if (!Object.keys(LL.strings).length || (st.language && st.language !== LL.lang)) await loadLang(LL.pickLang(st.language));
    let target = name || 'map';
    if (st.setup_needed) target = 'setup';
    else if (!st.authenticated) target = 'login';
    else if (!st.setup_done) target = 'setup';
    else if (target === 'login' || target === 'setup') target = 'map';
    if (seq !== routeSeq) return;            // a newer navigation started meanwhile
    if (target !== name) history.replaceState(null, '', '#/' + target);
    if (current && current.destroy) { try { current.destroy(); } catch (e) { /* ignore */ } }
    current = null;
    const fn = views[target] || views.map;
    current = await fn(app, rest, st) || null;
  }
  LL.route = route;
  window.addEventListener('hashchange', route);
  window.addEventListener('DOMContentLoaded', route);

  // ------------------------------------------------------------------ sign-in page
  LL.view('login', async function (app) {
    const user = h('input', { type: 'text', autocomplete: 'username', autocapitalize: 'none', spellcheck: 'false', required: true });
    const pass = h('input', { type: 'password', autocomplete: 'current-password', required: true });
    const msg = h('div', { class: 'msg' });
    const submit = h('button', { class: 'btn btn--primary btn--block', type: 'submit' }, t('login.submit'));
    const form = h('form', { class: 'stack', onsubmit: async (ev) => {
      ev.preventDefault(); submit.disabled = true; LL.msg(msg, '');
      try { await api('/api/login', { method: 'POST', body: { username: user.value, password: pass.value }, allow401: true }); LL.go('#/map'); }
      catch (e) { LL.msg(msg, LL.errText(e), 'error'); submit.disabled = false; pass.select(); }
    } }, LL.field(t('login.username'), user), LL.field(t('login.password'), pass), msg, submit);
    const pk = LL.passkeySupported() ? h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: async () => {
      LL.msg(msg, '');
      try { await LL.passkeyLogin(); LL.go('#/map'); } catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
    } }, t('login.passkey')) : null;
    app.replaceChildren(h('main', { class: 'auth-page' },
      h('div', { class: 'auth-card card' },
        h('div', { class: 'auth-card__brand' }, LL.logo(36), h('div', {}, h('div', { class: 'brand-name' }, 'Lanloom'), h('div', { class: 'muted' }, t('app.tagline')))),
        h('h1', { class: 'auth-card__title' }, t('login.title')),
        form,
        pk ? h('div', { class: 'divider' }, h('span', {}, t('login.or'))) : null,
        pk,
        h('p', { class: 'auth-card__foot muted' }, t('login.forgot'))),
      LL.footer()));
    setTimeout(() => { if (document.activeElement === document.body) user.focus(); }, 30);
  });

  LL.footer = function () {
    return h('footer', { class: 'page-foot muted' }, 'Lanloom ' + ((LL.state && LL.state.version) || ''), h('span', { class: 'dot-sep' }, '·'), t('app.readonly'));
  };
})();
