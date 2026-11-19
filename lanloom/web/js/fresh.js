/* Lanloom — how old is what a view shows? (O10)
   Three small tools for views that show data from the server:
   - LL.fresh.watch(host, every, refresh): the view reports each good state with ok(); when the last good state is older than
     three cadences (at least three minutes) a calm band "Data stale since HH:MM - refreshing by itself" appears above the
     view and the content is dimmed. It is checked every second against the clock, so it also appears when no answer comes
     at all, and it goes away with the next good state. While it is shown the view is asked again by itself.
   - the same refresh runs at once when the tab becomes visible again or the network comes back (sleep, network change).
   - LL.fresh.gate(every): the "one request at a time" lock of a view that gives a request up after max(30 s, 3 cadences)
     and asks again, so a request that never ends can never block the view for ever. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t } = LL;
  const MIN_STALE_MS = 3 * 60 * 1000, CADENCES = 3, RETRY_MS = 15000, GRACE_MS = 5000, MIN_LOCK_MS = 30000, MIN_WAKE_AGE_MS = 30000;
  const watches = new Set();

  const staleAfter = every => Math.max(MIN_STALE_MS, CADENCES * (every || 0));
  const clock = ts => new Date(ts).toLocaleTimeString(LL.locale(), { hour12: false, hour: '2-digit', minute: '2-digit' });

  // opts.band === false: only the refresh after sleep / network change (small parts of a card that must not stack bands)
  function watch(host, every, refresh, opts) {
    const w = { band_: !(opts && opts.band === false), host, every: every || 0, refresh, lastOk: Date.now(), tried: 0, grace: 0, band: null, retry: 0, dead: false, seen: false, born: Date.now() };
    function ask() {
      w.tried = Date.now();
      if (typeof w.refresh !== 'function') return;
      try { Promise.resolve(w.refresh()).catch(() => {}); } catch (e) { /* a failing refresh is asked again later */ }
    }
    function show(on) {
      if (on) {
        const text = t('stale.band', { time: clock(w.lastOk) });
        if (!w.band) {
          w.band = h('div', { class: 'll-stale', role: 'status' }, h('i', { class: 'll-stale__dot', 'aria-hidden': 'true' }), h('span', { class: 'll-stale__text' }, text));
          if (host.parentNode) host.parentNode.insertBefore(w.band, host);
          host.classList.add('is-outdated');
        } else if (w.band.lastChild.textContent !== text) w.band.lastChild.textContent = text;
        if (!w.band.isConnected && host.parentNode) host.parentNode.insertBefore(w.band, host);
      } else if (w.band) {
        w.band.remove(); w.band = null; host.classList.remove('is-outdated');
      }
    }
    function check() {
      if (w.dead) return;
      const now = Date.now();
      if (!host.isConnected) { if (w.seen || now - w.born > 30000) destroy(); return; }    // a section that is built first and put in place a moment later is not given up
      w.seen = true;
      if (document.hidden || !host.getClientRects().length) { w.grace = now + GRACE_MS; return; }    // nobody looks (background tab, closed rail): nothing ages, the first seconds after are free
      const stale = w.band_ && now - w.lastOk > staleAfter(w.every) && now >= w.grace;
      show(stale);
      if (stale && !document.hidden && now - w.tried >= RETRY_MS) ask();
    }
    function destroy() {
      if (w.dead) return;
      w.dead = true; clearInterval(w.timer); clearTimeout(w.retry); watches.delete(w);
      if (w.band) { w.band.remove(); w.band = null; }
      host.classList.remove('is-outdated');
    }
    w.ask = ask;
    w.timer = setInterval(check, 1000);
    watches.add(w);
    return {
      // a good state was drawn
      ok() { if (w.dead) return; w.lastOk = Date.now(); if (w.band) show(false); },
      // a request failed: ask again soon (once), the view would otherwise stay on its message until the next cadence
      fail() { if (w.dead || w.retry) return; w.retry = setTimeout(() => { w.retry = 0; if (!w.dead && host.isConnected && !document.hidden) ask(); }, RETRY_MS); },
      destroy,
      stale: () => !!w.band,
      lastOk: () => w.lastOk
    };
  }

  // back from sleep, a tab that was in the background, the network is back: every visible view whose state is older than 30 s asks
  // at once (a state of a few seconds is left alone - a redraw would only cost the reader their open parts and their focus)
  function wake() {
    if (document.hidden) return;
    const now = Date.now();
    watches.forEach(w => {
      if (w.dead || !w.host.isConnected) return;
      if (now - w.lastOk < MIN_WAKE_AGE_MS) return;      // the shown state is seconds old (a short switch of tabs): nothing to fetch, nothing to redraw
      w.grace = now + GRACE_MS;              // the answer is on its way: no band for the first seconds
      w.ask();
    });
    if (document.querySelector('.boot-error')) LL.route();      // the page itself could not start (no state): try again
  }
  document.addEventListener('visibilitychange', wake);
  window.addEventListener('online', wake);

  // one request at a time, but not for ever: enter() returns a token (0 = a request is running), leave(token) frees the lock,
  // live(token) tells whether this request is still the current one (an abandoned one must not draw its late answer)
  function gate(every) {
    let busy = false, at = 0, seq = 0;
    return {
      enter() {
        if (busy && Date.now() - at < Math.max(MIN_LOCK_MS, CADENCES * (every || 0))) return 0;
        busy = true; at = Date.now(); return ++seq;
      },
      live: token => token === seq,
      leave(token) { if (token === seq) busy = false; }
    };
  }

  LL.fresh = { watch, gate, staleAfter };
})();
