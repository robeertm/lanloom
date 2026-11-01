/* Lanloom — small inline line icons per component kind (24×24 grid, stroke = currentColor). Original, generic shapes. */
(function (root, factory) {
  const I = factory();
  if (typeof module === 'object' && module.exports) module.exports = I;
  else { root.Lanloom = root.Lanloom || {}; root.Lanloom.Icons = I; }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const PATHS = {
    internet: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.6 2.4 3.9 5.2 3.9 8.5s-1.3 6.1-3.9 8.5c-2.6-2.4-3.9-5.2-3.9-8.5s1.3-6.1 3.9-8.5z"/>',
    router: '<rect x="3" y="12.5" width="18" height="7" rx="2"/><path d="M7 16h.01M10.5 16h.01M8 12.5 6 5.5M16 12.5l2-7"/><path d="M14.5 16H18"/>',
    switch: '<rect x="2.5" y="7" width="19" height="10" rx="2"/><path d="M6 11v2M9 11v2M12 11v2M15 11v2M18 11v2"/>',
    ap: '<path d="M4.5 9.5a10.6 10.6 0 0 1 15 0M7.3 12.4a6.6 6.6 0 0 1 9.4 0M10 15.2a2.8 2.8 0 0 1 4 0"/><circle cx="12" cy="18.3" r=".9"/>',
    nas: '<rect x="4.5" y="3.5" width="15" height="17" rx="2"/><path d="M4.5 9.2h15M4.5 14.8h15M8 6.4h.01M8 12h.01M8 17.6h.01M11.5 6.4H16M11.5 12H16M11.5 17.6H16"/>',
    server: '<rect x="3.5" y="4" width="17" height="7" rx="1.8"/><rect x="3.5" y="13" width="17" height="7" rx="1.8"/><path d="M7 7.5h.01M7 16.5h.01M11 7.5h6M11 16.5h6"/>',
    computer: '<rect x="3" y="4.5" width="18" height="11.5" rx="1.8"/><path d="M9 20h6M12 16v4"/>',
    phone: '<rect x="7" y="2.8" width="10" height="18.4" rx="2.4"/><path d="M11 18h2"/>',
    printer: '<path d="M7 8.5V3.8h10v4.7"/><rect x="3.5" y="8.5" width="17" height="8" rx="2"/><path d="M7 14h10v6.2H7z"/><path d="M17 11.5h.01"/>',
    tv: '<rect x="2.8" y="4.5" width="18.4" height="12" rx="1.8"/><path d="M8 20.5h8M12 16.5v4"/>',
    media: '<circle cx="12" cy="12" r="8.5"/><path d="M10 8.6v6.8l5.6-3.4z"/>',
    camera: '<path d="M3 7.5h12.5v9H3z"/><path d="M15.5 10.5l5-2.8v8.6l-5-2.8"/><circle cx="9.2" cy="12" r="2.1"/>',
    hub: '<path d="M4 11 12 4.5 20 11"/><path d="M6 9.5v10h12v-10"/><circle cx="12" cy="14.5" r="1.8"/><path d="M12 12.7v-1.6M10.4 15.4l-1.4.8M13.6 15.4l1.4.8"/>',
    iot: '<rect x="7" y="7" width="10" height="10" rx="1.6"/><path d="M10 7V4M14 7V4M10 20v-3M14 20v-3M7 10H4M7 14H4M20 10h-3M20 14h-3"/>',
    service: '<path d="M12 3.2 19.6 7.5v9L12 20.8 4.4 16.5v-9z"/><path d="M4.4 7.5 12 11.8l7.6-4.3M12 11.8v9"/>',
    broker: '<circle cx="12" cy="12" r="2.6"/><circle cx="4.8" cy="6" r="1.7"/><circle cx="19.2" cy="6" r="1.7"/><circle cx="4.8" cy="18" r="1.7"/><circle cx="19.2" cy="18" r="1.7"/><path d="M6.3 7.2l3.6 3M17.7 7.2l-3.6 3M6.3 16.8l3.6-3M17.7 16.8l-3.6-3"/>',
    self: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.4"/><circle cx="12" cy="12" r="1"/>',
    unknown: '<circle cx="12" cy="12" r="8.5"/><path d="M9.6 9.4a2.5 2.5 0 0 1 4.8.9c0 1.7-2.4 2.1-2.4 3.7"/><path d="M12 16.9h.01"/>'
  };
  const KINDS = Object.keys(PATHS);

  function svg(kind, size) {
    const s = size || 18;
    const body = PATHS[kind] || PATHS.unknown;
    return `<svg class="ll-icon" width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${body}</svg>`;
  }

  return { svg, KINDS, PATHS };
});
