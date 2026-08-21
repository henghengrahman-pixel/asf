const fs = require('fs');
const path = require('path');

class Store {
  constructor(dir) {
    this.dir = dir;
    this.batchesDir = path.join(dir, 'batches');
    fs.mkdirSync(dir, { recursive: true });
    fs.mkdirSync(this.batchesDir, { recursive: true });
    this.historyFile = path.join(dir, 'history.json');
    this.settingsFile = path.join(dir, 'settings.json');
    this.sitesFile = path.join(dir, 'sites.json');
    this._ensure(this.historyFile, []);
    this._ensure(this.settingsFile, {});
    this._ensure(this.sitesFile, []);
  }

  _ensure(file, fallback) { if (!fs.existsSync(file)) this._write(file, fallback); }
  _read(file, fallback) { try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return fallback; } }
  _write(file, value) {
    const tmp = `${file}.${process.pid}.${Date.now()}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(value, null, 2));
    fs.renameSync(tmp, file);
  }
  _batchFile(id) {
    const safe = String(id || '').replace(/[^a-zA-Z0-9_-]/g, '');
    return path.join(this.batchesDir, `${safe}.json`);
  }

  getSettings() { return this._read(this.settingsFile, {}); }
  setSettings(patch) {
    const next = { ...this.getSettings(), ...patch, updatedAt: new Date().toISOString() };
    this._write(this.settingsFile, next);
    return next;
  }

  getSites() { return this._read(this.sitesFile, []); }
  getSite(id) { return this.getSites().find(x => x.id === id) || null; }
  saveSite(site) {
    const all = this.getSites();
    const next = [site, ...all.filter(x => x.id !== site.id)];
    this._write(this.sitesFile, next);
    return site;
  }
  deleteSite(id) {
    const all = this.getSites();
    this._write(this.sitesFile, all.filter(x => x.id !== id));
  }

  getHistory(siteId = null) {
    const all = this._read(this.historyFile, []);
    if (!siteId) return all;
    return all.filter(x => (x.siteId || 'omtogel') === siteId);
  }
  addHistory(batch) {
    this._write(this._batchFile(batch.id), batch);
    const all = this.getHistory();
    const summaryItem = {
      id: batch.id,
      siteId: batch.siteId || 'omtogel',
      siteName: batch.siteName || '',
      date: batch.date,
      period: batch.period,
      createdAt: batch.createdAt,
      finishedAt: batch.finishedAt,
      summary: batch.summary
    };
    const next = [summaryItem, ...all.filter(x => x.id !== batch.id)];
    const keep = next.slice(0, 1000);
    const removed = next.slice(1000);
    this._write(this.historyFile, keep);
    for (const old of removed) {
      try { fs.unlinkSync(this._batchFile(old.id)); } catch {}
    }
  }
  getBatch(id) {
    const file = this._batchFile(id);
    if (!fs.existsSync(file)) return null;
    return this._read(file, null);
  }
  clearHistory(siteId = null) {
    const all = this.getHistory();
    const targets = siteId ? all.filter(x => (x.siteId || 'omtogel') === siteId) : all;
    for (const item of targets) {
      try { fs.unlinkSync(this._batchFile(item.id)); } catch {}
    }
    if (!siteId) this._write(this.historyFile, []);
    else this._write(this.historyFile, all.filter(x => (x.siteId || 'omtogel') !== siteId));
  }
}

module.exports = Store;
