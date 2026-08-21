const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const Store = require('./src/store');

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dp-multisite-'));
const store = new Store(dir);
store.saveSite({ id: 'omtogel', name: 'OMTOGEL', active: true });
store.saveSite({ id: 'rupiahtoto', name: 'RUPIAHTOTO', active: true });
assert.equal(store.getSites().length, 2);
assert.equal(store.getSite('rupiahtoto').name, 'RUPIAHTOTO');

const mk = (id, siteId) => ({ id, siteId, siteName: siteId.toUpperCase(), date: '21-08-2026', period: { label: '21-08-2026' }, createdAt: new Date().toISOString(), finishedAt: new Date().toISOString(), summary: { totalIds: 1, hasDp: 1, noDp: 0, invalid: 0, totalNominal: 100000 }, results: [{ userId: 'abc', status: 'has_dp' }] });
store.addHistory(mk('batch-a', 'omtogel'));
store.addHistory(mk('batch-b', 'rupiahtoto'));
assert.equal(store.getHistory('omtogel').length, 1);
assert.equal(store.getHistory('rupiahtoto').length, 1);
assert.equal(store.getBatch('batch-b').siteId, 'rupiahtoto');
store.clearHistory('rupiahtoto');
assert.equal(store.getHistory('rupiahtoto').length, 0);
assert.equal(store.getHistory('omtogel').length, 1);
console.log('MULTISITE STORE TEST OK');
