/* Applies the remembered colour mode and theme before the page is drawn (no flash). */
(function () {
  var r = document.documentElement;
  try {
    var m = localStorage.getItem('ls-mode'), p = localStorage.getItem('ls-palette');
    if (m === 'light' || m === 'dark') r.setAttribute('data-theme', m);
    if (p === 'aurora') r.setAttribute('data-palette', p);
  } catch (e) { /* private mode: keep the defaults */ }
})();
