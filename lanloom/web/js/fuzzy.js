/* Lanloom — fuzzy matching for the command palette (views and commands are matched here; devices and hub entities on the
   server, lanloom/palette.py, with the same rules). Runs in the page and under Node (tests).

   Every word of the query has to match: 100 a whole word, 80 the start of a word, 55 part of a word, 25–45 a typo
   (one edit for words of 4+ letters, two from 8; a swap of two neighbours counts as one). A text has a title and further
   words (keywords); the title counts fully, the others at 80 %. No dependencies, no DOM.

   Stable API: fold(text), words(text), typoLimit(word), distance(a, b, limit), wordScore(token, haystack, hayWords, typos),
   score(tokens, item, typos), rank(items, query, limit)   with item = {title, extra, ...}  →  [{item, score}] */
(function (root, factory) {
  const F = factory();
  if (typeof module === 'object' && module.exports) module.exports = F;
  else { root.Lanloom = root.Lanloom || {}; root.Lanloom.Fuzzy = F; }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';
  const TYPO_MIN = 4;

  // lower-case, no accents, everything that is not a letter or digit becomes one space
  function fold(text) {
    let s = String(text == null ? '' : text).normalize('NFKD').toLowerCase();
    s = s.replace(/[̀-ͯ]/g, '').replace(/ß/g, 'ss');
    return s.replace(/[^0-9a-z]+/g, ' ').trim();
  }
  const words = text => fold(text).split(' ').filter(Boolean);
  const typoLimit = w => (w.length < TYPO_MIN ? 0 : (w.length < 8 ? 1 : 2));

  // edit distance with a swap of two neighbours as one edit (optimal string alignment), cut off above limit
  function distance(a, b, limit) {
    if (Math.abs(a.length - b.length) > limit) return limit + 1;
    let prev2 = null, prev = Array.from({ length: b.length + 1 }, (_, j) => j);
    for (let i = 1; i <= a.length; i++) {
      const cur = new Array(b.length + 1); cur[0] = i;
      let best = cur[0];
      for (let j = 1; j <= b.length; j++) {
        const c = a[i - 1] === b[j - 1] ? 0 : 1;
        let v = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + c);
        if (prev2 && i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) v = Math.min(v, prev2[j - 2] + 1);
        cur[j] = v; if (v < best) best = v;
      }
      if (best > limit) return limit + 1;
      prev2 = prev; prev = cur;
    }
    return prev[b.length];
  }

  function wordScore(token, haystack, hayWords, typos) {
    if (hayWords.includes(token)) return 100;
    for (const w of hayWords) if (w.startsWith(token)) return 80;
    if (token.length >= 2 && haystack.includes(token)) return 55;
    if (typos) {
      const lim = typoLimit(token);
      if (lim) {
        let best = lim + 1;
        for (const w of hayWords) {
          const cut = w.length > token.length ? w.slice(0, token.length + 1) : w;   // a typo inside the beginning of a longer word
          best = Math.min(best, distance(token, cut, lim), distance(token, w, lim));
          if (best === 0) break;
        }
        if (best <= lim) return 45 - 10 * (best - 1);
      }
    }
    return 0;
  }

  // prepared once per item: {title, extra} → adds the folded texts
  function prepare(item) {
    if (item._t === undefined) {
      item._t = fold(item.title); item._x = fold(item.extra);
      item._tw = item._t ? item._t.split(' ') : []; item._xw = item._x ? item._x.split(' ') : [];
    }
    return item;
  }
  function score(tokens, item, typos) {
    prepare(item);
    if (!tokens.length) return 1;
    let total = 0;
    for (const tok of tokens) {
      const s = Math.max(wordScore(tok, item._t, item._tw, typos), Math.floor(0.8 * wordScore(tok, item._x, item._xw, typos)));
      if (!s) return 0;
      total += s;
    }
    return Math.floor(total / tokens.length);
  }
  // best matches first; typo tolerance only when nothing matches exactly
  function rank(items, query, limit) {
    const tokens = words(query);
    let out = [];
    for (const typos of [false, true]) {
      out = [];
      for (const item of items) { const s = score(tokens, item, typos); if (s) out.push({ item, score: s }); }
      if (out.length || !tokens.length) break;
    }
    out.sort((a, b) => (b.score - a.score) || (a.item.order || 0) - (b.item.order || 0) || (a.item._t.length - b.item._t.length));
    return limit ? out.slice(0, limit) : out;
  }

  return { fold, words, typoLimit, distance, wordScore, score, rank, TYPO_MIN };
});
