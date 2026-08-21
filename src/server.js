const express = require('express');
const session = require('express-session');
const FileStoreFactory = require('session-file-store');
const helmet = require('helmet');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
const Store = require('./store');
const { encrypt, decrypt } = require('./security');
const { createChecker, launchBrowser, todayJakarta } = require('./checker');

const app = express();
const PORT = Number(process.env.PORT || 3000);
const DATA_DIR = process.env.DATA_DIR || path.join(__dirname, '..', 'data');
const PANEL_ID = process.env.PANEL_ID || 'admin';
const PANEL_PASSWORD = process.env.PANEL_PASSWORD || 'admin123';
const SESSION_SECRET = process.env.SESSION_SECRET || 'CHANGE_ME_DP_CHECKER_2026_32_CHARS_MIN';
const AGENT_BASE_URL = (process.env.AGENT_BASE_URL || 'https://agwl5.suksesbogil.com').replace(/\/$/, '');
const CHECK_TIMEOUT_MS = Math.max(5000, Number(process.env.CHECK_TIMEOUT_MS || 15000));
const MAX_IDS = Math.max(1, Number(process.env.MAX_IDS_PER_BATCH || 1000));
const CHECK_CONCURRENCY = Math.min(5, Math.max(1, Number(process.env.CHECK_CONCURRENCY || 3)));
const CHECK_RETRIES = Math.min(5, Math.max(0, Number(process.env.CHECK_RETRIES || 2)));

fs.mkdirSync(DATA_DIR, { recursive: true });
const store = new Store(DATA_DIR);
const FileStore = FileStoreFactory(session);

app.set('trust proxy', 1);
app.set('view engine', 'ejs');
app.set('views', path.join(__dirname, '..', 'views'));
app.use(helmet({ contentSecurityPolicy: false }));
app.use(express.urlencoded({ extended: false, limit: '2mb' }));
app.use(express.json({ limit: '2mb' }));
app.use(express.static(path.join(__dirname, '..', 'public')));
app.use(session({
  store: new FileStore({ path: path.join(DATA_DIR, 'sessions'), ttl: 60 * 60 * 12, retries: 0 }),
  secret: SESSION_SECRET,
  resave: false,
  saveUninitialized: false,
  cookie: { httpOnly: true, sameSite: 'lax', secure: process.env.NODE_ENV === 'production', maxAge: 12 * 60 * 60 * 1000 }
}));

function auth(req, res, next) {
  if (req.session?.user === PANEL_ID) return next();
  return res.redirect('/login');
}
function money(n) { return new Intl.NumberFormat('id-ID').format(Number(n || 0)); }
function nowWib() { return new Date().toLocaleString('sv-SE', { timeZone: 'Asia/Jakarta' }).replace(' ', 'T') + '+07:00'; }
function getAgentCookie() {
  if (process.env.AGENT_COOKIE) return process.env.AGENT_COOKIE;
  const settings = store.getSettings();
  if (!settings.agentCookieEnc) return '';
  try { return decrypt(settings.agentCookieEnc, SESSION_SECRET); } catch { return ''; }
}
function summarize(results) {
  return {
    totalIds: results.length,
    valid: results.filter(x => x.status === 'has_dp' || x.status === 'no_dp').length,
    invalid: results.filter(x => x.status === 'invalid').length,
    hasDp: results.filter(x => x.status === 'has_dp').length,
    noDp: results.filter(x => x.status === 'no_dp').length,
    error: results.filter(x => x.status === 'error').length,
    totalTransaksi: results.reduce((s, x) => s + Number(x.jumlahDp || 0), 0),
    totalNominal: results.reduce((s, x) => s + Number(x.totalDp || 0), 0),
    totalBonus50Transaksi: results.reduce((s, x) => s + Number(x.jumlahBonus50 || 0), 0),
    totalBonus50: results.reduce((s, x) => s + Number(x.totalBonus50 || 0), 0)
  };
}
function isoToDmy(v) {
  const m = String(v || '').match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return '';
  return `${m[3]}-${m[2]}-${m[1]}`;
}
function dmyToIso(v) {
  const m = String(v || '').match(/^(\d{2})-(\d{2})-(\d{4})$/);
  if (!m) return '';
  return `${m[3]}-${m[2]}-${m[1]}`;
}
function jakartaIsoToday() {
  return dmyToIso(todayJakarta());
}
function offsetIsoDate(iso, deltaDays) {
  const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return '';
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]) + deltaDays));
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth()+1).padStart(2,'0')}-${String(d.getUTCDate()).padStart(2,'0')}`;
}
function resolvePeriod(body = {}) {
  const todayIso = jakartaIsoToday();
  const mode = ['today','yesterday','single','range'].includes(body.dateMode) ? body.dateMode : 'today';
  let startIso = todayIso, endIso = todayIso, label = `Hari ini (${isoToDmy(todayIso)})`;
  if (mode === 'yesterday') {
    startIso = endIso = offsetIsoDate(todayIso, -1);
    label = `Kemarin (${isoToDmy(startIso)})`;
  } else if (mode === 'single') {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(body.singleDate || '')) throw new Error('Tanggal tidak valid.');
    startIso = endIso = body.singleDate;
    label = isoToDmy(startIso);
  } else if (mode === 'range') {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(body.startDate || '') || !/^\d{4}-\d{2}-\d{2}$/.test(body.endDate || '')) throw new Error('Rentang tanggal tidak valid.');
    startIso = body.startDate; endIso = body.endDate;
    if (startIso > endIso) [startIso, endIso] = [endIso, startIso];
    label = `${isoToDmy(startIso)} s.d. ${isoToDmy(endIso)}`;
  }
  return { mode, startDate: isoToDmy(startIso), endDate: isoToDmy(endIso), startIso, endIso, label };
}

app.use((req, res, next) => {
  res.locals.money = money;
  res.locals.panelId = PANEL_ID;
  res.locals.currentPath = req.path;
  next();
});

app.get('/health', (req, res) => res.json({ ok: true, service: 'dp-checker-panel', dateWib: todayJakarta() }));
app.get('/login', (req, res) => res.render('login', { error: null }));
app.post('/login', (req, res) => {
  const digest = v => crypto.createHash('sha256').update(String(v)).digest();
  const idOk = crypto.timingSafeEqual(digest(req.body.id || ''), digest(PANEL_ID));
  const pwOk = crypto.timingSafeEqual(digest(req.body.password || ''), digest(PANEL_PASSWORD));
  if (!idOk || !pwOk) return res.status(401).render('login', { error: 'ID atau password salah.' });
  req.session.user = PANEL_ID;
  res.redirect('/');
});
app.post('/logout', auth, (req, res) => req.session.destroy(() => res.redirect('/login')));

app.get('/', auth, (req, res) => {
  const history = store.getHistory();
  res.render('dashboard', { history: history.slice(0, 10), connected: Boolean(getAgentCookie()), dateWib: todayJakarta() });
});
app.get('/check', auth, (req, res) => res.render('check', {
  maxIds: MAX_IDS,
  connected: Boolean(getAgentCookie()),
  todayIso: jakartaIsoToday(),
  yesterdayIso: offsetIsoDate(jakartaIsoToday(), -1),
  concurrency: CHECK_CONCURRENCY,
  retries: CHECK_RETRIES
}));
app.get('/session-agent', auth, (req, res) => res.render('session', { connected: Boolean(getAgentCookie()), saved: req.query.saved === '1', error: req.query.error || '' }));
app.post('/session-agent', auth, (req, res) => {
  const cookie = String(req.body.cookie || '').trim();
  if (!cookie) return res.redirect('/session-agent?error=' + encodeURIComponent('Cookie session tidak boleh kosong.'));
  store.setSettings({ agentCookieEnc: encrypt(cookie, SESSION_SECRET) });
  res.redirect('/session-agent?saved=1');
});
app.post('/session-agent/clear', auth, (req, res) => { store.setSettings({ agentCookieEnc: '' }); res.redirect('/session-agent'); });
app.post('/session-agent/test', auth, async (req, res) => {
  const cookie = getAgentCookie();
  if (!cookie) return res.status(400).json({ ok: false, error: 'Session agen belum diisi.' });
  let checker;
  try {
    checker = await createChecker({ baseUrl: AGENT_BASE_URL, cookieHeader: cookie, timeoutMs: CHECK_TIMEOUT_MS, maxRetries: 0 });
    await checker.testSession();
    res.json({ ok: true, message: 'Session agen aktif dan halaman Nama Pemain dapat dibuka.' });
  } catch (e) {
    res.status(400).json({ ok: false, error: e.message === 'SESSION_AGEN_TIDAK_VALID' ? 'Session agen tidak valid / sudah logout.' : e.message });
  } finally { if (checker) await checker.close().catch(() => {}); }
});

const jobs = new Map();
const queue = [];
let working = false;

async function processJob(job) {
  const cookie = getAgentCookie();
  if (!cookie) throw new Error('Session agen belum diisi.');
  job.status = 'running'; job.startedAt = nowWib();
  const checkers = [];
  const sharedBrowser = await launchBrowser();
  let nextIndex = 0;

  async function worker(workerNo) {
    const checker = await createChecker({
      baseUrl: AGENT_BASE_URL,
      cookieHeader: cookie,
      timeoutMs: CHECK_TIMEOUT_MS,
      maxRetries: CHECK_RETRIES,
      browser: sharedBrowser
    });
    checkers.push(checker);
    while (true) {
      const index = nextIndex++;
      if (index >= job.ids.length) break;
      const userId = job.ids[index];
      const item = job.items[index];
      item.status = 'running'; item.step = 'Mulai'; item.worker = workerNo;
      const result = await checker.checkOne(userId, job.period, (step, meta = {}) => {
        item.step = step;
        item.attempt = meta.attempt || item.attempt || 1;
        item.maxAttempts = meta.maxAttempts || CHECK_RETRIES + 1;
      });
      job.results[index] = result;
      item.status = result.status;
      item.step = 'Selesai';
      item.attempt = result.attemptUsed || item.attempt || 1;
      job.completed += 1;
      job.current = job.completed;
      job.currentId = userId;
      job.currentStep = 'Selesai';
    }
  }

  try {
    const workerCount = Math.min(CHECK_CONCURRENCY, job.ids.length);
    await Promise.all(Array.from({ length: workerCount }, (_, i) => worker(i + 1)));
  } finally {
    await Promise.all(checkers.map(c => c.close().catch(() => {})));
    await sharedBrowser.close().catch(() => {});
  }

  job.results = job.results.filter(Boolean);
  job.summary = summarize(job.results);
  job.status = 'done'; job.finishedAt = nowWib();
  store.addHistory({
    id: job.id,
    date: job.period.label,
    period: job.period,
    createdAt: job.createdAt,
    finishedAt: job.finishedAt,
    summary: job.summary,
    results: job.results
  });
  const cleanup = setTimeout(() => jobs.delete(job.id), 30 * 60 * 1000);
  if (typeof cleanup.unref === 'function') cleanup.unref();
}

async function runQueue() {
  if (working || queue.length === 0) return;
  working = true;
  while (queue.length) {
    const job = queue.shift();
    try { await processJob(job); }
    catch (e) { job.status = 'failed'; job.error = e.message; job.finishedAt = nowWib(); }
  }
  working = false;
}

app.post('/api/jobs', auth, (req, res) => {
  const raw = Array.isArray(req.body.ids) ? req.body.ids.join('\n') : String(req.body.ids || '');
  const ids = [...new Set(raw.split(/[\n,;\s]+/).map(x => x.trim()).filter(Boolean))];
  if (!ids.length) return res.status(400).json({ ok: false, error: 'Masukkan minimal 1 UserId.' });
  if (ids.length > MAX_IDS) return res.status(400).json({ ok: false, error: `Maksimal ${MAX_IDS} UserId per batch.` });
  if (!getAgentCookie()) return res.status(400).json({ ok: false, error: 'Session agen belum diisi. Buka menu Session Agen.' });
  let period;
  try { period = resolvePeriod(req.body); } catch (e) { return res.status(400).json({ ok: false, error: e.message }); }
  const id = crypto.randomUUID();
  const job = {
    id, ids, period, status: 'queued', createdAt: nowWib(), startedAt: null, finishedAt: null,
    current: 0, completed: 0, currentId: '', currentStep: '', results: new Array(ids.length), summary: null, error: null,
    items: ids.map(userId => ({ userId, status: 'queued', step: 'Menunggu', attempt: 0, maxAttempts: CHECK_RETRIES + 1, worker: null }))
  };
  jobs.set(id, job); queue.push(job); runQueue();
  res.json({ ok: true, jobId: id, period });
});
app.get('/api/jobs/:id', auth, (req, res) => {
  const job = jobs.get(req.params.id);
  if (!job) {
    const old = store.getBatch(req.params.id);
    if (!old) return res.status(404).json({ ok: false, error: 'Job tidak ditemukan.' });
    return res.json({ ok: true, job: { ...old, status: 'done', current: old.results.length, completed: old.results.length, ids: old.results.map(x => x.userId), items: old.results.map(r => ({ userId: r.userId, status: r.status, step: 'Selesai', attempt: r.attemptUsed || 1 })) } });
  }
  const safeResults = job.results.filter(Boolean);
  res.json({ ok: true, job: { ...job, results: safeResults } });
});
app.get('/result/:id', auth, (req, res) => {
  const job = jobs.get(req.params.id);
  const saved = store.getBatch(req.params.id);
  const data = saved || (job && job.status === 'done' ? { id: job.id, date: job.period.label, period: job.period, createdAt: job.createdAt, finishedAt: job.finishedAt, summary: job.summary, results: job.results } : null);
  if (!data) return res.status(404).send('Rekap tidak ditemukan atau masih diproses.');
  res.render('result', { batch: data });
});
app.get('/result/:id/user/:userId', auth, (req, res) => {
  const batch = store.getBatch(req.params.id) || (jobs.get(req.params.id)?.status === 'done' ? jobs.get(req.params.id) : null);
  if (!batch) return res.status(404).send('Rekap tidak ditemukan.');
  const result = (batch.results || []).find(r => String(r.userId).toLowerCase() === String(req.params.userId).toLowerCase());
  if (!result) return res.status(404).send('UserId tidak ditemukan pada batch ini.');
  const scope = req.query.scope === 'all' ? 'all' : 'period';
  const transactions = scope === 'all' ? (result.allTransactions || []) : (result.rawTransactions || []);
  res.render('transactions', { batch, result, transactions, scope });
});
app.get('/history', auth, (req, res) => res.render('history', { history: store.getHistory() }));
app.post('/history/clear', auth, (req, res) => { store.clearHistory(); res.redirect('/history'); });
app.get('/export/:id.csv', auth, (req, res) => {
  const batch = store.getBatch(req.params.id);
  if (!batch) return res.status(404).send('Rekap tidak ditemukan.');
  const esc = x => `"${String(x ?? '').replace(/"/g, '""')}"`;
  const lines = [['No','UserId','Status','Keterangan','Jumlah DP Valid','Total DP Valid','Jumlah Bonus 50%','Total Bonus 50%','Periode'].map(esc).join(',')];
  batch.results.forEach((r, i) => lines.push([i+1,r.userId,r.status,r.keterangan,r.jumlahDp,r.totalDp,r.jumlahBonus50 || 0,r.totalBonus50 || 0,batch.period?.label || batch.date].map(esc).join(',')));
  res.setHeader('Content-Type', 'text/csv; charset=utf-8');
  res.setHeader('Content-Disposition', `attachment; filename="rekap-dp-${batch.id.slice(0,8)}.csv"`);
  res.send('\uFEFF' + lines.join('\r\n'));
});
app.get('/export/:id/user/:userId.csv', auth, (req, res) => {
  const batch = store.getBatch(req.params.id);
  if (!batch) return res.status(404).send('Rekap tidak ditemukan.');
  const result = batch.results.find(r => String(r.userId).toLowerCase() === String(req.params.userId).toLowerCase());
  if (!result) return res.status(404).send('UserId tidak ditemukan.');
  const scope = req.query.scope === 'all' ? 'all' : 'period';
  const rows = scope === 'all' ? (result.allTransactions || []) : (result.rawTransactions || []);
  const esc = x => `"${String(x ?? '').replace(/"/g, '""')}"`;
  const lines = [['No','Date','Info','By','Coin','Last Coin'].map(esc).join(',')];
  rows.forEach((r, i) => lines.push([r.no || i+1,r.date,r.info,r.by,r.coin,r.lastCoin].map(esc).join(',')));
  res.setHeader('Content-Type', 'text/csv; charset=utf-8');
  res.setHeader('Content-Disposition', `attachment; filename="transaksi-${result.userId}-${scope}.csv"`);
  res.send('\uFEFF' + lines.join('\r\n'));
});

app.use((err, req, res, next) => { console.error(err); res.status(500).send('Internal Server Error'); });
app.listen(PORT, '0.0.0.0', () => {
  console.log(`DP Checker Panel running on port ${PORT}`);
  console.log(`Concurrency=${CHECK_CONCURRENCY}, retries=${CHECK_RETRIES}`);
  if (PANEL_PASSWORD === 'admin123') console.warn('WARNING: PANEL_PASSWORD masih default admin123. Ganti di Railway Variables.');
});
