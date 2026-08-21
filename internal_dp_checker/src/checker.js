const { chromium } = require('playwright');
const { parseCoin, classifyDeposits, inPeriod } = require('./logic');


async function launchBrowser() {
  return chromium.launch({
    headless: true,
    args: [
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--disable-dev-shm-usage',
      '--disable-gpu',
      '--no-zygote'
    ]
  });
}

function datePartsJakarta(date = new Date()) {
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Jakarta', day: '2-digit', month: '2-digit', year: 'numeric'
  }).formatToParts(date);
  const get = t => parts.find(p => p.type === t)?.value;
  return { day: get('day'), month: get('month'), year: get('year') };
}

function todayJakarta() {
  const p = datePartsJakarta();
  return `${p.day}-${p.month}-${p.year}`;
}

function parseCookieHeader(cookieHeader, origin) {
  if (!cookieHeader) return [];
  const url = new URL(origin);
  return String(cookieHeader).split(';').map(s => s.trim()).filter(Boolean).map(pair => {
    const i = pair.indexOf('=');
    if (i < 1) return null;
    return {
      name: pair.slice(0, i).trim(),
      value: pair.slice(i + 1).trim(),
      domain: url.hostname,
      path: '/',
      httpOnly: false,
      secure: url.protocol === 'https:',
      sameSite: 'Lax'
    };
  }).filter(Boolean);
}

async function createChecker({ baseUrl, playerListUrl = '', cookieHeader, timeoutMs = 15000, maxRetries = 2, browser: sharedBrowser = null }) {
  const ownBrowser = !sharedBrowser;
  const browser = sharedBrowser || await launchBrowser();
  const context = await browser.newContext({ locale: 'id-ID', timezoneId: 'Asia/Jakarta' });
  const cookies = parseCookieHeader(cookieHeader, baseUrl);
  if (cookies.length) await context.addCookies(cookies);
  const page = await context.newPage();
  page.setDefaultTimeout(timeoutMs);

  async function ensureAgentPage() {
    await page.goto(playerListUrl || `${baseUrl}/agentplayerlist.php`, { waitUntil: 'domcontentloaded', timeout: timeoutMs });
    const checkbox = page.locator('form:has(input[name="usercheck"]) input[name="usercheck"]').first();
    const textInput = page.locator('input[name="user"][type="text"]:visible').first();
    if (await checkbox.count() === 0 || await textInput.count() === 0) throw new Error('SESSION_AGEN_TIDAK_VALID');
  }

  async function readHistoryPopup(popup) {
    await popup.waitForLoadState('domcontentloaded', { timeout: timeoutMs }).catch(() => {});
    await popup.waitForURL(/editcoinhis\.php/i, { timeout: timeoutMs }).catch(() => {});
    const rows = popup.locator('table tr');
    const all = [];
    const rowCount = await rows.count();
    for (let i = 0; i < rowCount; i++) {
      const cells = rows.nth(i).locator('td');
      if (await cells.count() < 6) continue;
      const no = (await cells.nth(0).innerText().catch(() => '')).trim();
      const date = (await cells.nth(1).innerText().catch(() => '')).trim();
      const info = (await cells.nth(2).innerText().catch(() => '')).trim();
      const by = (await cells.nth(3).innerText().catch(() => '')).trim();
      const coinText = (await cells.nth(4).innerText().catch(() => '')).trim();
      const lastCoinText = (await cells.nth(5).innerText().catch(() => '')).trim();
      if (!/^\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}:\d{2}$/.test(date)) continue;
      all.push({
        no: /^\d+$/.test(no) ? Number(no) : no,
        date, info, by,
        coin: parseCoin(coinText),
        lastCoin: parseCoin(lastCoinText),
        coinText,
        lastCoinText
      });
    }
    return all;
  }



  async function findPlayerRow(userId) {
    const resultRows = page.locator('tr');
    const resultRowCount = await resultRows.count();
    for (let i = 0; i < resultRowCount; i++) {
      const row = resultRows.nth(i);
      const cells = row.locator('td');
      if (await cells.count() < 5) continue;
      const userCellText = (await cells.nth(1).innerText().catch(() => ''))
        .split(/\r?\n/).map(x => x.trim()).filter(Boolean)[0] || '';
      if (userCellText.toLowerCase() === String(userId).toLowerCase()) return row;
    }
    return null;
  }

  async function searchPlayer(userId, onStep, attempt, maxAttempts) {
    onStep('Buka Nama Pemain', { attempt, maxAttempts });
    await ensureAgentPage();
    const searchForm = page.locator('form:has(input[name="usercheck"])').first();
    const userCheck = searchForm.locator('input[name="usercheck"]');
    const userInput = searchForm.locator('input[name="user"][type="text"]:visible').first();
    const searchButton = searchForm.locator('input#filter[name="cari"][type="submit"]');
    if (await userInput.count() !== 1) throw new Error('INPUT_USERID_TIDAK_DITEMUKAN');
    if (!(await userCheck.isChecked())) await userCheck.check({ force: true });
    if (!(await userCheck.isChecked())) throw new Error('USERID_CHECKBOX_GAGAL_DICENTANG');
    await userInput.fill(userId);
    onStep('Klik Cari', { attempt, maxAttempts });
    await Promise.all([
      page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: timeoutMs }).catch(() => {}),
      searchButton.click()
    ]);
    await page.locator('body').waitFor({ state: 'attached', timeout: timeoutMs });
    return findPlayerRow(userId);
  }

  async function checkBalanceOneAttempt(userId, onStep, attempt, maxAttempts) {
    const targetRow = await searchPlayer(userId, onStep, attempt, maxAttempts);
    onStep('Baca Saldo', { attempt, maxAttempts });
    if (!targetRow) {
      return { userId, status: 'invalid', keterangan: 'ID tidak valid', balance: 0, balanceText: '', attemptUsed: attempt };
    }
    const cells = targetRow.locator('td');
    if (await cells.count() < 5) throw new Error('KOLOM_BALANCE_TIDAK_DITEMUKAN');
    const balanceText = (await cells.nth(4).innerText().catch(() => '')).trim();
    const balance = parseCoin(balanceText);
    return { userId, status: 'valid', keterangan: 'Saldo berhasil dibaca', balance, balanceText, attemptUsed: attempt };
  }

  async function checkBalanceOne(userId, onStep = () => {}) {
    const maxAttempts = Math.max(1, Number(maxRetries) + 1);
    let lastError = null;
    for (let attempt = 1; attempt <= maxAttempts; attempt++) {
      try {
        return await checkBalanceOneAttempt(userId, onStep, attempt, maxAttempts);
      } catch (e) {
        lastError = e;
        if (e.message === 'SESSION_AGEN_TIDAK_VALID') break;
        if (attempt < maxAttempts) {
          onStep(`Retry otomatis ${attempt}/${maxAttempts - 1}`, { attempt, maxAttempts });
          await page.waitForTimeout(Math.min(1200 * attempt, 3000));
        }
      }
    }
    return {
      userId, status: 'error',
      keterangan: lastError?.message === 'SESSION_AGEN_TIDAK_VALID'
        ? 'Session agen tidak valid / sudah logout'
        : `Gagal cek saldo setelah ${maxAttempts} percobaan: ${lastError?.message || 'Unknown error'}`,
      balance: 0, balanceText: '', attemptUsed: maxAttempts
    };
  }

  async function checkOneAttempt(userId, period, onStep, attempt, maxAttempts) {
    let popup = null;
    try {
      onStep('Buka Nama Pemain', { attempt, maxAttempts });
      await ensureAgentPage();

      const searchForm = page.locator('form:has(input[name="usercheck"])').first();
      const userCheck = searchForm.locator('input[name="usercheck"]');
      const userInput = searchForm.locator('input[name="user"][type="text"]:visible').first();
      const searchButton = searchForm.locator('input#filter[name="cari"][type="submit"]');

      if (await userInput.count() !== 1) throw new Error('INPUT_USERID_TIDAK_DITEMUKAN');
      if (!(await userCheck.isChecked())) await userCheck.check({ force: true });
      if (!(await userCheck.isChecked())) throw new Error('USERID_CHECKBOX_GAGAL_DICENTANG');
      await userInput.fill(userId);

      onStep('Klik Cari', { attempt, maxAttempts });
      await Promise.all([
        page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: timeoutMs }).catch(() => {}),
        searchButton.click()
      ]);
      await page.locator('body').waitFor({ state: 'attached', timeout: timeoutMs });

      onStep('Validasi UserId', { attempt, maxAttempts });
      const resultRows = page.locator('tr');
      const resultRowCount = await resultRows.count();
      let targetRow = null;
      for (let i = 0; i < resultRowCount; i++) {
        const row = resultRows.nth(i);
        const cells = row.locator('td');
        if (await cells.count() < 2) continue;
        const userCellText = (await cells.nth(1).innerText().catch(() => ''))
          .split(/\r?\n/).map(x => x.trim()).filter(Boolean)[0] || '';
        if (userCellText.toLowerCase() !== userId.toLowerCase()) continue;
        if (await row.locator('input[type="submit"][value="History Bank"]').count() > 0) {
          targetRow = row;
          break;
        }
      }

      // Fallback untuk HTML lama/invalid yang kadang dirombak browser:
      // cari tombol History Bank yang form-nya membawa hidden user exact.
      let historyButton = null;
      if (targetRow) {
        historyButton = targetRow.locator('input[type="submit"][value="History Bank"]').first();
      } else {
        const forms = page.locator('form[action$="editcoinhis.php"]');
        const formCount = await forms.count();
        for (let i = 0; i < formCount; i++) {
          const form = forms.nth(i);
          const hidden = form.locator('input[name="user"][type="hidden"]');
          if (await hidden.count() === 0) continue;
          const value = String(await hidden.first().inputValue().catch(() => '')).trim();
          if (value.toLowerCase() !== userId.toLowerCase()) continue;
          const btn = form.locator('input[type="submit"][value="History Bank"]');
          if (await btn.count() > 0) { historyButton = btn.first(); break; }
        }
      }

      if (!historyButton) {
        return {
          userId, status: 'invalid', keterangan: 'ID tidak valid', jumlahDp: 0, totalDp: 0,
          transaksi: [], bonus50: [], jumlahBonus50: 0, totalBonus50: 0, diabaikan: [],
          rawTransactions: [], allTransactions: [], attemptUsed: attempt
        };
      }

      onStep('Klik History Bank', { attempt, maxAttempts });
      [popup] = await Promise.all([
        page.waitForEvent('popup', { timeout: timeoutMs }),
        historyButton.click()
      ]);

      onStep('Baca History Bank', { attempt, maxAttempts });
      const allTransactions = await readHistoryPopup(popup);
      await popup.close().catch(() => {});
      popup = null;

      const rawTransactions = allTransactions.filter(x => inPeriod(x.date, period));
      const rawDeposits = rawTransactions
        .filter(x => x.info === 'Deposit' || x.info === 'Deposit (PGA)')
        .map(x => ({ date: x.date, info: x.info, by: x.by, coin: x.coin, lastCoin: x.lastCoin }));

      const classified = classifyDeposits(rawDeposits, {
        windowMinutes: 30,
        amountTolerance: 1000,
        balanceTolerance: 1
      });

      const totalDp = classified.valid.reduce((s, x) => s + x.coin, 0);
      const totalBonus50 = classified.bonus50.reduce((s, x) => s + x.coin, 0);
      return {
        userId,
        status: classified.valid.length ? 'has_dp' : 'no_dp',
        keterangan: classified.valid.length ? 'Ada DP pada periode' : 'Tidak ada DP pada periode',
        jumlahDp: classified.valid.length,
        totalDp,
        transaksi: classified.valid,
        bonus50: classified.bonus50,
        jumlahBonus50: classified.bonus50.length,
        totalBonus50,
        diabaikan: classified.ignored,
        rawTransactions,
        allTransactions,
        attemptUsed: attempt
      };
    } finally {
      if (popup) await popup.close().catch(() => {});
    }
  }

  async function checkOne(userId, period, onStep = () => {}) {
    const maxAttempts = Math.max(1, Number(maxRetries) + 1);
    let lastError = null;
    for (let attempt = 1; attempt <= maxAttempts; attempt++) {
      try {
        return await checkOneAttempt(userId, period, onStep, attempt, maxAttempts);
      } catch (e) {
        lastError = e;
        if (e.message === 'SESSION_AGEN_TIDAK_VALID') break;
        if (attempt < maxAttempts) {
          onStep(`Retry otomatis ${attempt}/${maxAttempts - 1}`, { attempt, maxAttempts });
          await page.waitForTimeout(Math.min(1200 * attempt, 3000));
        }
      }
    }

    return {
      userId,
      status: 'error',
      keterangan: lastError?.message === 'SESSION_AGEN_TIDAK_VALID'
        ? 'Session agen tidak valid / sudah logout'
        : `Gagal cek setelah ${maxAttempts} percobaan: ${lastError?.message || 'Unknown error'}`,
      jumlahDp: 0, totalDp: 0, transaksi: [], bonus50: [], jumlahBonus50: 0, totalBonus50: 0,
      diabaikan: [], rawTransactions: [], allTransactions: [], attemptUsed: maxAttempts
    };
  }

  return {
    checkOne,
    checkBalanceOne,
    close: async () => {
      await context.close().catch(() => {});
      if (ownBrowser) await browser.close().catch(() => {});
    },
    testSession: async () => { await ensureAgentPage(); return true; }
  };
}

module.exports = { createChecker, launchBrowser, todayJakarta };
