/* Lanloom landing page: colour mode and theme switches, the tour viewer, the clip. No network access, no libraries. */
(function () {
  'use strict';
  var root = document.documentElement;
  var dark = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : { matches: false };
  var still = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };
  var $ = function (id) { return document.getElementById(id); };
  var VIEWS = [
    ['overview', 'Overview', 'The overview: how the network is doing, six key figures and what stands out.'],
    ['map', 'Map', 'The map: zones, traces and dots; a legend that doubles as a filter; hover a tile to light only the traces it needs.'],
    ['network', 'Network', 'The network view: gateway, switches and access points, one zone per subnet, and what hangs on which port.'],
    ['devices', 'Devices', 'Every device with its type, network and services; the same list in stability and link views.'],
    ['data-paths', 'Data paths', 'Where the devices talk to on the internet, by country, over real cables or as arcs in the rain.'],
    ['nightly-reports', 'Nightly report', 'The night in plain words, with a headline and items you can tick off.'],
    ['control-room', 'Control room', 'The internet, storage, battery backup and backup chains, each with the age of its picture.'],
    ['firewall', 'Firewall', 'Zones as a board with the traffic between them; rules in plain words; read only unless you arm the executor.'],
    ['automations', 'Automations', 'The automations of a home-automation hub in plain words, with conflicts and what they depend on.'],
    ['inventory', 'Inventory', 'Purpose, room, owner, warranty and the end of updates for every device.'],
    ['energy', 'Energy', 'Grid, solar and battery feeding the home and its consumers, with cost and CO₂ per lane.'],
    ['vulnerabilities', 'Vulnerabilities', 'Published vulnerabilities that match a device’s maker, model and firmware, offline and strict on purpose.'],
    ['rewind', 'Rewind', 'Faults bundled into incidents, with a heat map of 90 days and the last 7 days by hour.'],
    ['settings', 'Settings', 'Sources, adapters, notifications, sign-in security, backup and appearance.']
  ];
  var st = { view: 'overview', mode: null, pal: null };

  function effectiveMode() {
    var t = root.getAttribute('data-theme');
    return t === 'light' || t === 'dark' ? t : (dark.matches ? 'dark' : 'light');
  }
  function save(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* ignore */ } }

  // ---- header switches
  var tb = $('theme-btn'), pb = $('palette-btn');
  function paintHeader() {
    var t = root.getAttribute('data-theme');
    var name = t === 'light' ? 'Light' : t === 'dark' ? 'Dark' : 'Auto';
    tb.textContent = name; tb.setAttribute('aria-label', 'Colour mode: ' + name.toLowerCase());
    var p = root.getAttribute('data-palette') === 'aurora' ? 'Aurora' : 'Circuit';
    pb.textContent = p; pb.setAttribute('aria-label', 'Colour theme: ' + p);
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) { var bg = getComputedStyle(root).getPropertyValue('--bg').trim(); if (bg) meta.setAttribute('content', bg); }
  }
  tb.addEventListener('click', function () {
    var t = root.getAttribute('data-theme'), n = t === 'auto' ? 'light' : t === 'light' ? 'dark' : 'auto';
    root.setAttribute('data-theme', n); save('ls-mode', n); paintHeader(); follow();
  });
  pb.addEventListener('click', function () {
    var n = root.getAttribute('data-palette') === 'aurora' ? 'circuit' : 'aurora';
    root.setAttribute('data-palette', n); save('ls-palette', n); paintHeader(); follow();
  });
  if (dark.addEventListener) dark.addEventListener('change', function () { paintHeader(); follow(); });

  // ---- tour viewer: follows the page until the reader picks something in the tour
  var seg = $('tour-views'), img = $('tour-img'), cap = $('tour-cap'), shot = $('tour-shot');
  VIEWS.forEach(function (v) {
    var b = document.createElement('button');
    b.type = 'button'; b.textContent = v[1]; b.dataset.v = v[0]; b.setAttribute('aria-pressed', v[0] === st.view ? 'true' : 'false');
    seg.appendChild(b);
  });
  function press(group, value) {
    Array.prototype.forEach.call(group.querySelectorAll('button'), function (b) { b.setAttribute('aria-pressed', b.dataset.v === value ? 'true' : 'false'); });
  }
  function drawTour() {
    var mode = st.mode || effectiveMode(), pal = st.pal || root.getAttribute('data-palette') || 'circuit';
    var v = VIEWS.filter(function (x) { return x[0] === st.view; })[0];
    img.src = 'img/' + st.view + '-' + mode + '-' + pal + '.png';
    img.alt = v[1] + ' of the sample network, ' + mode + ' mode, colour theme ' + (pal === 'aurora' ? 'Aurora' : 'Circuit');
    img.height = st.view === 'data-paths' ? 1060 : 900;
    shot.dataset.view = st.view; cap.textContent = v[2];
    press($('tour-mode'), mode); press($('tour-pal'), pal); press(seg, st.view);
  }
  seg.addEventListener('click', function (e) { var b = e.target.closest('button'); if (b) { st.view = b.dataset.v; drawTour(); } });
  $('tour-mode').addEventListener('click', function (e) { var b = e.target.closest('button'); if (b) { st.mode = b.dataset.v; drawTour(); } });
  $('tour-pal').addEventListener('click', function (e) { var b = e.target.closest('button'); if (b) { st.pal = b.dataset.v; drawTour(); } });

  // ---- phone pictures: light on circuit, dark on aurora (the two looks that were taken)
  function drawPhones() {
    var look = effectiveMode() === 'dark' ? 'dark-aurora' : 'light-circuit';
    Array.prototype.forEach.call(document.querySelectorAll('img[data-phone]'), function (i) { i.src = 'img/phone-' + i.dataset.phone + '-' + look + '.png'; });
  }
  function follow() { drawPhones(); if (!st.mode && !st.pal) drawTour(); }

  // ---- the clip: plays unless the reader asked for less motion; then the still picture stays with controls
  var clip = $('demo');
  if (clip && clip.play) {
    if (still.matches) { clip.controls = true; }
    else { var p = clip.play(); if (p && p.catch) p.catch(function () { clip.controls = true; }); }
  }

  paintHeader(); drawPhones(); drawTour();
})();
