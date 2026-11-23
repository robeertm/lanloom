// O15: which edge of a sideways row still has content beyond it (extracted from js/handy.js between the swipe-sides markers)
const fs = require('fs'), path = require('path');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'lanloom', 'web', 'js', 'handy.js'), 'utf8');
const m = /\/\/ <swipe-sides>([\s\S]*?)\/\/ <\/swipe-sides>/.exec(src);
if (!m) { console.error('markers missing'); process.exit(1); }
const swipeSides = new Function(m[1] + '; return swipeSides;')();
const out = {
  fits: swipeSides(0, 300, 300, false),
  ltrStart: swipeSides(0, 800, 300, false),
  ltrMiddle: swipeSides(200, 800, 300, false),
  ltrEnd: swipeSides(500, 800, 300, false),
  rtlStart: swipeSides(0, 800, 300, true),
  rtlMiddle: swipeSides(-200, 800, 300, true),
  rtlEnd: swipeSides(-500, 800, 300, true),
  subpixel: swipeSides(0.4, 300.6, 300, false),
};
console.log(JSON.stringify(out));
