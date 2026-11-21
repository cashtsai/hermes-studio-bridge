// cardText 必須能從「視覺卡」(body.visual) 取出可貼走的純文字 —— 否則人格(hermes)
// 的富區塊回覆,複製/回覆/分享全拿到空字串(機主 2026-11-21 回報的「人格訊息無法
// 回覆複製轉貼」)。載入 static/console/app.js 的出貨真函式驗證。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/console/app.js'), 'utf8');
const a = source.indexOf('function visualText(');
const b = source.indexOf('function visualReplyHtml(');
assert.ok(a >= 0 && b > a, 'source section visualText..cardText');
const c = vm.createContext({});
vm.runInContext(source.slice(a, b), c);

test('plain text card still works (unchanged path)', () => {
  assert.equal(c.cardText({ body: { text: '嗨' } }), '嗨');
});

test('visual card: text lives in blocks → cardText extracts it', () => {
  const card = { body: { visual: { blocks: [
    { type: 'text', text: '水鏡先生說:' },
    { type: 'markdown', text: '**重點**在這' },
  ] } } };
  assert.equal(c.cardText(card), '水鏡先生說:\n**重點**在這');
});

test('visual card with only fallback_text (image/link blocks) → uses fallback_text', () => {
  const card = { body: { visual: { blocks: [{ type: 'image', url: 'https://x/y.png' }], fallback_text: '一張圖' } } };
  assert.equal(c.cardText(card), '一張圖');
});

test('link/file blocks contribute their label', () => {
  const card = { body: { visual: { blocks: [{ type: 'link', label: '開會記錄', url: 'https://x' }] } } };
  assert.equal(c.cardText(card), '開會記錄');
});

test('top-level body.text wins over visual (visual is only the fallback)', () => {
  const card = { body: { text: '純文字優先', visual: { blocks: [{ type: 'text', text: '不該用這個' }] } } };
  assert.equal(c.cardText(card), '純文字優先');
});

test('empty visual → empty string (no crash)', () => {
  assert.equal(c.cardText({ body: { visual: { blocks: [] } } }), '');
  assert.equal(c.cardText({ body: {} }), '');
  assert.equal(c.cardText({}), '');
});
