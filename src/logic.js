function parseCoin(value) {
  const n = Number(String(value || '').replace(/[^0-9-]/g, ''));
  return Number.isFinite(n) ? n : 0;
}

function parseJakartaDateTime(value) {
  const m = String(value || '').trim().match(/^(\d{2})-(\d{2})-(\d{4})\s+(\d{2}):(\d{2}):(\d{2})$/);
  if (!m) return NaN;
  const [, dd, mm, yyyy, HH, MM, SS] = m;
  // Waktu pada situs adalah WIB (UTC+7). Konversi ke epoch UTC agar selisih menit stabil.
  return Date.UTC(Number(yyyy), Number(mm) - 1, Number(dd), Number(HH) - 7, Number(MM), Number(SS));
}

function dateOnlyEpochWib(ddmmyyyy, endOfDay = false) {
  const m = String(ddmmyyyy || '').match(/^(\d{2})-(\d{2})-(\d{4})$/);
  if (!m) return NaN;
  const [, dd, mm, yyyy] = m;
  if (endOfDay) return Date.UTC(Number(yyyy), Number(mm) - 1, Number(dd), 16, 59, 59, 999);
  return Date.UTC(Number(yyyy), Number(mm) - 1, Number(dd), -7, 0, 0, 0);
}

function inPeriod(dateText, period) {
  if (!period?.startDate || !period?.endDate) return true;
  const ts = parseJakartaDateTime(dateText);
  if (!Number.isFinite(ts)) return false;
  const start = dateOnlyEpochWib(period.startDate, false);
  const end = dateOnlyEpochWib(period.endDate, true);
  return ts >= start && ts <= end;
}

function isDepositInfo(info) {
  return info === 'Deposit' || info === 'Deposit (PGA)';
}

function isAutoBonusAgent(by) {
  const x = String(by || '').trim().toLowerCase();
  return x === 'vkpaaautobonus' || x === 'vkpaaautobonus2';
}

/**
 * Klasifikasi transaksi deposit pada periode yang dipilih.
 *
 * Aturan:
 * - Coin 5.000 selalu tidak masuk DP valid.
 * - By vkpaaautobonus / vkpaaautobonus2 tidak masuk DP valid.
 * - Bonus 50% adalah deposit sesudah DP utama, maksimal 30 menit,
 *   nominal sekitar 50% DP utama, dan saldo tidak dimainkan di antaranya.
 * - "Saldo tidak dimainkan" diverifikasi dari kontinuitas balance:
 *   saldo sesudah DP utama ~= saldo sebelum transaksi bonus.
 */
function classifyDeposits(rows, options = {}) {
  const windowMinutes = Number.isFinite(Number(options.windowMinutes)) ? Number(options.windowMinutes) : 30;
  const amountTolerance = Number.isFinite(Number(options.amountTolerance)) ? Number(options.amountTolerance) : 1000;
  const balanceTolerance = Number.isFinite(Number(options.balanceTolerance)) ? Number(options.balanceTolerance) : 1;
  const windowMs = Math.max(0, windowMinutes) * 60 * 1000;

  const all = rows
    .filter(x => isDepositInfo(x.info))
    .map((x, index) => ({
      ...x,
      _index: index,
      ts: Number.isFinite(x.ts) ? x.ts : parseJakartaDateTime(x.date),
      coin: Number(x.coin || 0),
      lastCoin: Number(x.lastCoin || 0)
    }))
    .filter(x => Number.isFinite(x.ts))
    .sort((a, b) => a.ts - b.ts || a._index - b._index);

  const bonusIndexes = new Set();
  const bonusPairs = [];

  // Setiap transaksi diuji sebagai kandidat bonus terhadap DP utama PALING DEKAT sebelumnya.
  for (let i = 0; i < all.length; i++) {
    const candidate = all[i];
    if (candidate.coin <= 0) continue;

    for (let j = i - 1; j >= 0; j--) {
      const base = all[j];
      const diff = candidate.ts - base.ts;
      if (diff < 0) continue;
      if (diff > windowMs) break;

      // DP utama tidak boleh transaksi yang sudah menjadi bonus, nominal 5.000,
      // atau transaksi dari agent autobonus.
      if (bonusIndexes.has(j)) continue;
      if (base.coin <= 0 || base.coin === 5000 || isAutoBonusAgent(base.by)) continue;

      const expected = base.coin * 0.5;
      if (Math.abs(candidate.coin - expected) > amountTolerance) continue;

      // Saldo sebelum kandidat bonus = Last Coin kandidat - Coin kandidat.
      // Jika tidak ada gameplay/transaksi balance di antaranya, angka ini harus
      // sama (atau sangat dekat) dengan Last Coin DP utama.
      const candidateBalanceBefore = candidate.lastCoin - candidate.coin;
      const balanceContinuous = base.lastCoin > 0 && candidate.lastCoin > 0 &&
        Math.abs(candidateBalanceBefore - base.lastCoin) <= balanceTolerance;
      if (!balanceContinuous) continue;

      bonusIndexes.add(i);
      bonusPairs.push({
        ...candidate,
        baseDate: base.date,
        baseCoin: base.coin,
        minutesAfter: Math.round((diff / 60000) * 100) / 100,
        expectedBonus: expected
      });
      break;
    }
  }

  const valid = [];
  const ignored = [];
  for (let i = 0; i < all.length; i++) {
    const x = all[i];
    if (bonusIndexes.has(i)) continue;
    if (x.coin === 5000) {
      ignored.push({ ...x, reason: 'Nominal 5.000 (bonus)' });
      continue;
    }
    if (isAutoBonusAgent(x.by)) {
      ignored.push({ ...x, reason: `Agent bonus: ${x.by}` });
      continue;
    }
    valid.push(x);
  }

  return {
    valid: valid.map(stripInternal),
    bonus50: bonusPairs.map(stripInternal),
    ignored: ignored.map(stripInternal)
  };
}

function stripInternal(x) {
  const y = { ...x };
  delete y._index;
  delete y.ts;
  return y;
}

module.exports = {
  parseCoin,
  parseJakartaDateTime,
  inPeriod,
  isDepositInfo,
  isAutoBonusAgent,
  classifyDeposits
};
