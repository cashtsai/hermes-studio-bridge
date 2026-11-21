// Wave2:折疊步驟的工具語意圖示 —— 讀/改/跑/查網/思考 各自對的圖示。
// 載入 transcript.js 的出貨真函式 stepIcon 驗證。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '../static/console/transcript.js'), 'utf8');
const a = src.indexOf('function stepIcon(');
const b = src.indexOf('function renderTurnContent(');
assert.ok(a >= 0 && b > a, 'source section stepIcon');
const c = vm.createContext({});
vm.runInContext(src.slice(a, b), c);
const icon = (kind, tool) => c.stepIcon({ kind, body: tool ? { tool } : {} });

test('thinking / reasoning / 💭-text → brain', () => {
  assert.equal(icon('thinking'), '🧠');
  assert.equal(icon('reasoning'), '🧠');
  assert.equal(icon('text'), '🧠');
});
test('read-family tools → eye', () => {
  for (const t of ['Read', 'Grep', 'Glob', 'LS', 'view_file', 'search']) assert.equal(icon('tool_call', t), '👁', t);
});
test('edit-family tools → pen', () => {
  for (const t of ['Write', 'Edit', 'apply_patch', 'create_file', 'update']) assert.equal(icon('tool_call', t), '✎', t);
});
test('web tools → globe', () => {
  for (const t of ['WebFetch', 'WebSearch', 'browser_navigate', 'curl']) assert.equal(icon('tool_call', t), '🌐', t);
});
test('shell tools → run marker', () => {
  for (const t of ['Bash', 'run_command', 'exec', 'shell']) assert.equal(icon('tool_call', t), '▸', t);
});
test('unknown tool → generic wrench', () => {
  assert.equal(icon('tool_result', 'SomeMysteryTool'), '🔧');
  assert.equal(icon('tool_call', ''), '🔧');
});
