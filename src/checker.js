const { chromium } = require('playwright');

function todayJakarta() {
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Jakarta', day: '2-digit', month: '2-digit', year: 'numeric'
  }).formatToParts(new Date());
  const get = t => parts.find(p => p.type === t)?.value;
  return `${get('day')}-${get('month')}-${get('year')}`;
}

function parseCoin(value) {
  const n = Number(String(value || '').replace(/[^0-9-]/g, ''));
  return Number.isFinite(n) ? n : 0;
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

async function createChecker({ baseUrl, cookieHeader, timeoutMs = 15000 }) {
  const browser = await chromium.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
  });
  const context = await browser.newContext({ locale: 'id-ID', timezoneId: 'Asia/Jakarta' });
  const cookies = parseCookieHeader(cookieHeader, baseUrl);
  if (cookies.length) await context.addCookies(cookies);
  const page = await context.newPage();
  page.setDefaultTimeout(timeoutMs);

  async function ensureAgentPage() {
    await page.goto(`${baseUrl}/agentplayerlist.php`, { waitUntil: 'domcontentloaded', timeout: timeoutMs });
    const checkbox = page.locator('form:has(input[name="usercheck"]) input[name="usercheck"]');
    if (await checkbox.count() === 0) {
      throw new Error('SESSION_AGEN_TIDAK_VALID');
    }
  }

  async function checkOne(userId, onStep = () => {}) {
    const result = {
      userId,
      status: 'error',
      keterangan: 'Gagal mengecek',
      jumlahDp: 0,
      totalDp: 0,
      transaksi: []
    };

    try {
      onStep('Buka Nama Pemain');
      await ensureAgentPage();

      const searchForm = page.locator('form:has(input[name="usercheck"])').first();
      const userCheck = searchForm.locator('input[name="usercheck"]');
      const userInput = searchForm.locator('input[name="user"][type="text"]');
      const searchButton = searchForm.locator('input#filter[name="cari"][type="submit"]');

      if (!(await userCheck.isChecked())) await userCheck.check({ force: true });
      if (!(await userCheck.isChecked())) throw new Error('USERID_CHECKBOX_GAGAL_DICENTANG');
      await userInput.fill(userId);

      onStep('Klik Cari');
      await Promise.all([
        page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: timeoutMs }).catch(() => {}),
        searchButton.click()
      ]);

      // Cari BARIS hasil berdasarkan UserId yang terlihat.
      // Jangan bergantung pada form[action=editcoinhis.php], karena HTML situs memakai
      // form/td yang tidak valid dan browser dapat memindahkan node form saat parsing DOM.
      const resultRows = page.locator('tr');
      const resultRowCount = await resultRows.count();
      let targetRow = null;

      for (let i = 0; i < resultRowCount; i++) {
        const row = resultRows.nth(i);
        const cells = row.locator('td');
        if (await cells.count() < 2) continue;

        const userCellText = (await cells.nth(1).innerText().catch(() => ''))
          .split(/\r?\n/)
          .map(x => x.trim())
          .filter(Boolean)[0] || '';

        if (userCellText.toLowerCase() !== userId.toLowerCase()) continue;

        const historyButton = row.locator('input[type="submit"][value="History Bank"]');
        if (await historyButton.count() > 0) {
          targetRow = row;
          break;
        }
      }

      if (!targetRow) {
        result.status = 'invalid';
        result.keterangan = 'ID tidak valid';
        return result;
      }

      onStep('Klik History Bank');
      let popup;
      try {
        const historyButton = targetRow.locator('input[type="submit"][value="History Bank"]').first();
        [popup] = await Promise.all([
          page.waitForEvent('popup', { timeout: timeoutMs }),
          historyButton.click()
        ]);
      } catch (e) {
        throw new Error(`POPUP_HISTORY_GAGAL: ${e.message}`);
      }

      await popup.waitForLoadState('domcontentloaded', { timeout: timeoutMs }).catch(() => {});
      await popup.waitForURL(/editcoinhis\.php/i, { timeout: timeoutMs }).catch(() => {});
      onStep('Baca History Bank');

      const rows = popup.locator('table tr');
      const today = todayJakarta();
      const transactions = [];
      const rowCount = await rows.count();
      for (let i = 0; i < rowCount; i++) {
        const cells = rows.nth(i).locator('td');
        const n = await cells.count();
        if (n < 6) continue;
        const date = (await cells.nth(1).innerText().catch(() => '')).trim();
        const info = (await cells.nth(2).innerText().catch(() => '')).trim();
        const by = (await cells.nth(3).innerText().catch(() => '')).trim();
        const coinText = (await cells.nth(4).innerText().catch(() => '')).trim();
        if (!date.startsWith(today)) continue;
        if (!(info === 'Deposit' || info === 'Deposit (PGA)')) continue;

        const coin = parseCoin(coinText);
        const byNormalized = by.toLowerCase();

        // Aturan DP valid:
        // 1) nominal Coin tepat 5.000 selalu dianggap bonus dan tidak dihitung,
        //    siapa pun agent/kolom By yang memprosesnya.
        // 2) transaksi yang diproses agent autobonus juga tidak dihitung.
        if (coin === 5000) continue;
        if (byNormalized === 'vkpaaautobonus' || byNormalized === 'vkpaaautobonus2') continue;

        transactions.push({ date, info, by, coin });
      }
      await popup.close().catch(() => {});

      result.transaksi = transactions;
      result.jumlahDp = transactions.length;
      result.totalDp = transactions.reduce((s, x) => s + x.coin, 0);
      result.status = transactions.length ? 'has_dp' : 'no_dp';
      result.keterangan = transactions.length ? 'Ada DP hari ini' : 'Tidak ada DP hari ini';
      return result;
    } catch (e) {
      result.status = 'error';
      result.keterangan = e.message === 'SESSION_AGEN_TIDAK_VALID'
        ? 'Session agen tidak valid / sudah logout'
        : `Error: ${e.message}`;
      return result;
    }
  }

  return {
    checkOne,
    close: () => browser.close(),
    testSession: async () => {
      await ensureAgentPage();
      return true;
    }
  };
}

module.exports = { createChecker, todayJakarta };
