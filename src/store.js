const fs = require('fs');
const path = require('path');

class Store {
  constructor(dir) {
    this.dir = dir;
    fs.mkdirSync(dir, { recursive: true });
    this.historyFile = path.join(dir, 'history.json');
    this.settingsFile = path.join(dir, 'settings.json');
    this._ensure(this.historyFile, []);
    this._ensure(this.settingsFile, {});
  }

  _ensure(file, fallback) {
    if (!fs.existsSync(file)) this._write(file, fallback);
  }

  _read(file, fallback) {
    try {
      return JSON.parse(fs.readFileSync(file, 'utf8'));
    } catch {
      return fallback;
    }
  }

  _write(file, value) {
    const tmp = `${file}.${process.pid}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(value, null, 2));
    fs.renameSync(tmp, file);
  }

  getSettings() { return this._read(this.settingsFile, {}); }
  setSettings(patch) {
    const next = { ...this.getSettings(), ...patch, updatedAt: new Date().toISOString() };
    this._write(this.settingsFile, next);
    return next;
  }

  getHistory() { return this._read(this.historyFile, []); }
  addHistory(batch) {
    const all = this.getHistory();
    all.unshift(batch);
    this._write(this.historyFile, all.slice(0, 500));
  }
  getBatch(id) { return this.getHistory().find(x => x.id === id) || null; }
  clearHistory() { this._write(this.historyFile, []); }
}

module.exports = Store;
