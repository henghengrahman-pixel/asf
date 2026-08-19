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
    const checkbox = page.locator('input[name="usercheck"]');
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

      const userCheck = page.locator('input[name="usercheck"]');
      if (!(await userCheck.isChecked())) await userCheck.check();
      await page.locator('input[name="user"]').fill(userId);

      onStep('Klik Cari');
      await Promise.all([
        page.waitForLoadState('domcontentloaded').catch(() => {}),
        page.locator('#filter, input[name="cari"]').first().click()
      ]);

      const historyForms = page.locator('form[action="editcoinhis.php"]');
      const count = await historyForms.count();
      let targetForm = null;
      for (let i = 0; i < count; i++) {
        const f = historyForms.nth(i);
        const v = await f.locator('input[name="user"]').getAttribute('value').catch(() => null);
        if (String(v || '').trim().toLowerCase() === userId.toLowerCase()) {
          targetForm = f;
          break;
        }
      }

      if (!targetForm) {
        result.status = 'invalid';
        result.keterangan = 'ID tidak valid';
        return result;
      }

      onStep('Klik History Bank');
      let popup;
      try {
        [popup] = await Promise.all([
          context.waitForEvent('page', { timeout: timeoutMs }),
          targetForm.locator('input[value="History Bank"]').click()
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
        transactions.push({ date, info, by, coin: parseCoin(coinText) });
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
