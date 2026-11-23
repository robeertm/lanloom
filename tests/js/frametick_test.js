/* lanloom/web/js/frametick.js: frames drawn per second at a given screen interval, per motion setting. Prints one JSON object. */
const path = require('path');
const { frameGate } = require(path.join(__dirname, '..', '..', 'lanloom', 'web', 'js', 'frametick.js'));
function run(motion, gap, secs) {
  const g = frameGate({ motion: () => motion });
  let n = 0;
  for (let ts = 1000; ts < 1000 + secs * 1000; ts += gap) if (g.draw(ts)) n++;
  return n / secs;
}
const out = { gaps: {} };
for (const gap of [6, 1000 / 120, 1000 / 60]) {
  out.gaps[gap.toFixed(1)] = {};
  for (const m of ['auto', 'full', 'saver', 'off']) out.gaps[gap.toFixed(1)][m] = run(m, gap, 5);
}
// slow frames (45 ms) step the rate down below the ceiling, smooth frames bring it back
const g = frameGate({ motion: () => 'full' });
let ts = 1000; for (let i = 0; i < 200; i++) { g.draw(ts); ts += 45; }
out.slow = g.fps();
for (let i = 0; i < 4000; i++) { g.draw(ts); ts += 16.7; }
out.recovered = g.fps();
// an auto setting never steps "up" past 30
const a = frameGate({ motion: () => 'auto' }); out.autoStart = a.fps();
console.log(JSON.stringify(out));
