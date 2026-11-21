// Wave6:phase→符號表(一眼辨狀態)。載入 app.js 真的 PHASE_GLYPH + phaseGlyph。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '../static/console/app.js'), 'utf8');
const a = src.indexOf('const PHASE_GLYPH');
const b = src.indexOf('const activityFormat');
assert.ok(a >= 0 && b > a, 'source section PHASE_GLYPH..phaseGlyph');
const c = vm.createContext({});
vm.runInContext(src.slice(a, b), c);
const g = (status, extra) => c.phaseGlyph({ status, meta: extra || {} });

test('needs-you phases → raised hand', () => {
  assert.equal(g('needs_input'), '✋');
  assert.equal(g('waiting_approval'), '✋');
});
test('active phases → filled dot', () => {
  for (const s of ['running', 'run', 'busy', 'dispatching']) assert.equal(g(s), '●', s);
});
test('terminal/error phases → distinct marks', () => {
  assert.equal(g('done'), '✓');
  assert.equal(g('completed'), '✓');
  assert.equal(g('failed'), '✕');
  assert.equal(g('error'), '✕');
  assert.equal(g('stalled'), '⚠');
  assert.equal(g('usage_limited'), '⏳');
  assert.equal(g('queued'), '⋯');
});
test('quiet phases have no glyph (安靜是好事)', () => {
  assert.equal(g('idle'), '');
  assert.equal(g('dormant'), '');
  assert.equal(g('waiting'), '');
});
test('locked wins over phase', () => {
  assert.equal(g('running', { locked: true }), '🔒');
});
test('accepts a bare status string too', () => {
  assert.equal(c.phaseGlyph('running'), '●');
  assert.equal(c.phaseGlyph('idle'), '');
});
