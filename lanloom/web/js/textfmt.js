/* Lanloom — text and number formatting for the translations (loaded before app.js; also runs under Node for the tests).
   Translated texts may carry plural forms in a small ICU subset:
       {n, plural, one {# device} other {# devices}}
   Categories come from Intl.PluralRules of the page language (one, other, and in other languages few, many, …); "=0" style
   exact matches win over the category; "#" stands for the number, written the way the language writes numbers.
   Everything else is {name} replacement. lanloom/textfmt.py does the same for texts rendered on the server, and
   tests/test_i18n_format.py keeps the two in step. */
(function (root) {
  'use strict';

  function category(lang, n) {
    try { return new Intl.PluralRules(lang).select(n); } catch (e) { return n === 1 ? 'one' : 'other'; }
  }

  // a number as the language writes it (decimal separator, grouping); "digits" fixes the fraction digits
  function num(lang, n, digits) {
    n = Number(n);
    if (!isFinite(n)) return '–';
    const o = digits == null ? { maximumFractionDigits: 2 } : { minimumFractionDigits: digits, maximumFractionDigits: digits };
    try { return new Intl.NumberFormat(lang, o).format(n); } catch (e) { return digits == null ? String(n) : n.toFixed(digits); }
  }

  function plurals(s, vars, lang) {
    let out = '', i = 0;
    for (;;) {
      const j = s.indexOf('{', i);
      if (j < 0) return out + s.slice(i);
      const head = /^\{(\w+),\s*plural,\s*/.exec(s.slice(j));
      if (!head) { out += s.slice(i, j + 1); i = j + 1; continue; }
      out += s.slice(i, j);
      let p = j + head[0].length;
      const branch = {};
      while (p < s.length && s[p] !== '}') {
        const c = /^(=\d+|[a-z]+)\s*\{/.exec(s.slice(p));
        if (!c) break;
        p += c[0].length;
        let depth = 1, q = p;
        while (q < s.length && depth) { if (s[q] === '{') depth++; else if (s[q] === '}') depth--; q++; }
        branch[c[1]] = s.slice(p, q - 1);
        p = q;
        while (p < s.length && /\s/.test(s[p])) p++;
      }
      i = p + 1;
      const raw = vars ? vars[head[1]] : null;
      if (raw == null) { out += (branch.other || '').replace(/#/g, '{' + head[1] + '}'); continue; }
      const n = Number(raw);
      const text = branch['=' + n] != null ? branch['=' + n] : (branch[category(lang, n)] != null ? branch[category(lang, n)] : (branch.other || ''));
      out += text.replace(/#/g, num(lang, n));
    }
  }

  function fill(s, vars, lang) {
    s = String(s);
    if (s.indexOf(', plural,') >= 0) s = plurals(s, vars, lang || 'en');
    return vars ? s.replace(/\{(\w+)\}/g, (m, k) => (vars[k] != null ? String(vars[k]) : m)) : s;
  }

  const api = { fill, num, category };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else { root.Lanloom = root.Lanloom || {}; root.Lanloom.Text = api; }
})(typeof self !== 'undefined' ? self : this);
