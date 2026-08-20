const assert = require('assert');
const { classifyDeposits } = require('./src/logic');

function row(date, by, coin, lastCoin, info='Deposit') { return { date, by, coin, lastCoin, info }; }

// 50k -> 25k, 10 menit, saldo tidak dimainkan: bonus harus terdeteksi.
let x = classifyDeposits([
  row('20-08-2026 10:00:00', 'agentA', 50000, 150000),
  row('20-08-2026 10:10:00', 'agentB', 25000, 175000)
]);
assert.equal(x.valid.length, 1);
assert.equal(x.valid[0].coin, 50000);
assert.equal(x.bonus50.length, 1);
assert.equal(x.bonus50[0].coin, 25000);

// Lewat 30 menit: bukan bonus, dua-duanya DP valid.
x = classifyDeposits([
  row('20-08-2026 10:00:00', 'agentA', 50000, 150000),
  row('20-08-2026 10:31:00', 'agentB', 25000, 175000)
]);
assert.equal(x.valid.length, 2);
assert.equal(x.bonus50.length, 0);

// Saldo dimainkan di antaranya: walau 50% dan <=30 menit, jangan tandai bonus.
x = classifyDeposits([
  row('20-08-2026 10:00:00', 'agentA', 50000, 150000),
  row('20-08-2026 10:10:00', 'agentB', 25000, 165000) // balance sebelum=140k, bukan 150k
]);
assert.equal(x.valid.length, 2);
assert.equal(x.bonus50.length, 0);

// Semua 5.000 diabaikan jika bukan pasangan bonus yang valid.
x = classifyDeposits([row('20-08-2026 11:00:00', 'agentApaSaja', 5000, 5000)]);
assert.equal(x.valid.length, 0);
assert.equal(x.ignored.length, 1);

// Agent autobonus tidak masuk DP valid.
x = classifyDeposits([row('20-08-2026 11:00:00', 'vkpaaautobonus2', 100000, 100000)]);
assert.equal(x.valid.length, 0);
assert.equal(x.ignored.length, 1);

// Toleransi nominal +/- 1.000.
x = classifyDeposits([
  row('20-08-2026 12:00:00', 'agentA', 73000, 173000),
  row('20-08-2026 12:15:00', 'agentB', 36500, 209500)
]);
assert.equal(x.valid.length, 1);
assert.equal(x.bonus50.length, 1);

console.log('logic tests: OK');
