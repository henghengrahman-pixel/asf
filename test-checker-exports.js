const assert = require('assert');
const Module = require('module');
const originalLoad = Module._load;
Module._load = function(request, parent, isMain) {
  if (request === 'playwright') {
    return { chromium: { launch: async () => ({}) } };
  }
  return originalLoad.call(this, request, parent, isMain);
};
const checker = require('./src/checker');
assert.strictEqual(typeof checker.createChecker, 'function');
assert.strictEqual(typeof checker.launchBrowser, 'function');
assert.strictEqual(typeof checker.todayJakarta, 'function');
console.log('checker exports: OK');
