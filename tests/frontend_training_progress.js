// Offline polling regression. Run with node tests/frontend_training_progress.js.
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

test('progress keeps its last valid calendar during 429 and invalid responses', async () => {
  const elements = new Map();
  function element() {
    return {
      dataset: {}, style: {}, addEventListener() {}, setAttribute() {}, append() {}, replaceChildren() {},
    };
  }
  function getElementById(id) {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  }
  const snapshot = {
    schema_version: 1, start_day: '2026-06-01', end_day_exclusive: '2026-06-02',
    generated_at: '2026-06-02T14:00:00+08:00', source: 'test', total_days: 1,
    audited_days: 1, collected: 2, labelled: 2, days: [
      {day: '2026-06-01', windows: 1, complete_windows: 1, audited_windows: 1, returned: 2,
        collected: 2, labelled: 2},
    ],
  };
  let clock = Date.parse(snapshot.generated_at);
  const body = (data, etag = 'v1') => ({
    ok: true, headers: {get: key => key === 'etag' ? etag : null}, json: async () => data,
  });
  const replies = [
    body(snapshot),
    {ok: false, status: 429, headers: {get: () => '120'}},
    body(snapshot),
    body({...snapshot, days: [{day: 'invalid'}]}, 'v2'),
    body({...snapshot, generated_at: '2026-06-02T13:00:00+08:00'}, 'old'),
    body({...snapshot, collected: 3}, 'v3'),
  ];
  let requested = 0;
  const context = vm.createContext({
    document: {getElementById, createElement: element, createDocumentFragment: element},
    AbortSignal, Date: class extends Date { static now() { return clock; } },
    setInterval() {}, fetch: async () => replies[requested++],
  });
  const source = fs.readFileSync(path.join(__dirname, '../web/training-progress/progress.js'), 'utf8');
  vm.runInContext(source, context);
  await new Promise(resolve => setImmediate(resolve));
  const refresh = () => vm.runInContext('refresh()', context);
  assert.equal(getElementById('collected').textContent, '2');
  assert.equal(getElementById('pending-labels').textContent, '0');
  assert.match(getElementById('finished-days').textContent, /^1 个有资讯的日期/);
  await refresh();
  assert.match(getElementById('message').textContent, /刷新失败.*14:00:00/);
  clock += 119000;
  await refresh();
  assert.equal(requested, 2);
  clock += 1000;
  await refresh();
  assert.match(getElementById('message').textContent, /^更新于/);
  await refresh();
  assert.match(getElementById('message').textContent, /刷新失败/);
  clock += 15000;
  await refresh();
  assert.match(getElementById('message').textContent, /刷新失败.*14:00:00/);
  assert.equal(getElementById('collected').textContent, '2');
  clock += 30000;
  await refresh();
  assert.equal(getElementById('collected').textContent, '3');
  assert.equal(getElementById('pending-labels').textContent, '1');
});
