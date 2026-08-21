import os, json, time, sqlite3, threading, secrets, re, uuid, shutil
from datetime import datetime, timedelta
from functools import wraps
from html import escape
from zoneinfo import ZoneInfo
import requests, base64, hmac, hashlib, struct
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, g, Response
from werkzeug.security import generate_password_hash, check_password_hash

WIB=ZoneInfo('Asia/Jakarta')
DURATIONS={'makan':20,'merokok':10,'toilet':5,'bab':15}
MAX_ACTIVE_LEAVES=5
POLICY_VERSION=os.getenv('POLICY_VERSION','2026-08-07-v1')

DEFAULT_POLICY_RULES=[
 'Dilarang keras Dugem, Judi dan narkoba.',
 'Dilarang membuka sosmed untuk kepentingan pribadi, nonton YouTube, FB pribadi, IG pribadi di komputer kantor.',
 'Telat masuk kantor denda per menit Rp50.000, masuk jam 11.00 paling lambat 11.15.',
 'Dilarang memakai headset.',
 'Wajib kumpul HP setiap change shift.',
 'Shift pagi dilarang merokok jam 19.30–22.30 dan shift malam dilarang merokok jam 23.15–01.30.',
 'Jangan pernah curang ketika IN/OUT atau akan ada denda.',
 'Mistake potong Gaji & BONUS.',
 'Jangan pernah manipulasi Doc dan bermain credit/mencuri uang kantor.',
 'Setiap staf wajib mengikuti aturan kantor.'
]

def resolve_db_path():
    raw=(os.getenv('DB_PATH') or '').strip(); data_dir=(os.getenv('DATA_DIR') or '/data').strip() or '/data'
    if not raw: raw=os.path.join(data_dir,'omtogel_staff.db')
    raw=os.path.abspath(os.path.expanduser(raw))
    if raw.endswith(os.sep) or os.path.isdir(raw): raw=os.path.join(raw,'omtogel_staff.db')
    # recover from old accidental nested DB_PATH=/data/omtogel_staff.db when it already became a folder
    if os.path.isdir(raw): raw=os.path.join(raw,'omtogel_staff.db')
    parent=os.path.dirname(raw) or '.'; os.makedirs(parent,exist_ok=True)
    return raw
DB_PATH=resolve_db_path()
BOT_TOKEN=os.getenv('BOT_TOKEN','').strip(); INOUT_CHAT_ID=os.getenv('INOUT_CHAT_ID',os.getenv('CHAT_ID','')).strip(); ATTENDANCE_CHAT_ID=os.getenv('ATTENDANCE_CHAT_ID',INOUT_CHAT_ID).strip(); ALERT_CHAT_ID=os.getenv('ALERT_CHAT_ID',os.getenv('CHAT_ID','')).strip(); WITHDRAW_CHAT_ID=os.getenv('WITHDRAW_CHAT_ID','').strip(); API_KEY=os.getenv('API_KEY','').strip()
INOUT_ADMIN_IDS=[x.strip() for x in os.getenv('INOUT_ADMIN_IDS','').split(',') if x.strip()]
LATE_MINUTES=int(os.getenv('LATE_MINUTES','5')); WITHDRAW_LATE_MINUTES=int(os.getenv('WITHDRAW_LATE_MINUTES','10')); SCAN_SECONDS=int(os.getenv('SCAN_SECONDS','5')); LEADER_TTL_SECONDS=int(os.getenv('LEADER_TTL_SECONDS','15')); MAX_DEVICES=int(os.getenv('MAX_DEVICES','0'))
RETENTION_DAYS=max(1,int(os.getenv('RETENTION_DAYS','60'))); LEGACY_PENDING_MONITOR_ENABLED=(os.getenv('ENABLE_LEGACY_PENDING_MONITOR','0').strip()=='1')
ADMIN_USERNAME=(os.getenv('ADMIN_USERNAME') or os.getenv('PANEL_ID') or 'admin').strip(); ADMIN_PASSWORD=(os.getenv('ADMIN_PASSWORD') or os.getenv('PANEL_PASSWORD') or 'admin12345'); SECRET_KEY=os.getenv('SECRET_KEY',secrets.token_hex(32))
app=Flask(__name__); app.secret_key=SECRET_KEY; app.config.update(SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Lax')
DP_INTERNAL_URL=(os.getenv('DP_INTERNAL_URL') or 'http://127.0.0.1:3001').rstrip('/')
DP_INTERNAL_TOKEN=os.getenv('DP_INTERNAL_TOKEN','crm-dp-internal-2026')
DP_DATA_DIR=os.path.join((os.getenv('DATA_DIR') or '/data').strip() or '/data','dp_checker')
ATTENDANCE_OPEN='10:15'
ATTENDANCE_ON_TIME_UNTIL='11:15:59'
ATTENDANCE_LATE_START='11:16:00'
ATTENDANCE_CLOSE='13:15:00'
ATTENDANCE_FINE_PER_MINUTE=50000


@app.template_filter('fmt_wib')
def fmt_wib(value):
    if not value:
        return '-'
    try:
        dt=datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt=dt.replace(tzinfo=WIB)
        else:
            dt=dt.astimezone(WIB)
        months=['Januari','Februari','Maret','April','Mei','Juni','Juli','Agustus','September','Oktober','November','Desember']
        return f"{dt.day:02d} {months[dt.month-1]} {dt.year} • {dt.strftime('%H:%M:%S')} WIB"
    except Exception:
        return str(value)


def parse_panel_date(value, default=None, strict=False, allow_blank=True):
    """Panel memakai DD/MM/YYYY. ISO YYYY-MM-DD hanya diterima untuk kompatibilitas internal/legacy."""
    raw=(str(value).strip() if value is not None else '')
    if not raw:
        if strict and not allow_blank:
            raise ValueError('Tanggal wajib diisi dengan format DD/MM/YYYY.')
        return default
    for fmt in ('%d/%m/%Y','%Y-%m-%d'):
        try:
            return datetime.strptime(raw,fmt).date().isoformat()
        except ValueError:
            pass
    if strict:
        raise ValueError(f'Tanggal "{raw}" tidak valid. Gunakan format DD/MM/YYYY.')
    return default

def panel_date_required(value, field='Tanggal'):
    try:
        return parse_panel_date(value, strict=True, allow_blank=False)
    except ValueError as e:
        raise ValueError(f'{field}: {e}')

def panel_date_optional(value, field='Tanggal', previous=None):
    raw=(str(value).strip() if value is not None else '')
    if not raw:
        return previous
    try:
        return parse_panel_date(raw, strict=True, allow_blank=True)
    except ValueError as e:
        raise ValueError(f'{field}: {e}')

@app.template_filter('fmt_date_id')
def fmt_date_id(value):
    if not value:
        return '-'
    raw=str(value).strip()
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}',raw):
            dt=datetime.strptime(raw,'%Y-%m-%d')
        elif re.fullmatch(r'\d{2}/\d{2}/\d{4}',raw):
            dt=datetime.strptime(raw,'%d/%m/%Y')
        else:
            dt=datetime.fromisoformat(raw.replace('Z','+00:00'))
        return dt.strftime('%d/%m/%Y')
    except Exception:
        return raw

@app.template_filter('fmt_duration')
def fmt_duration_seconds(value):
    if value is None:
        return '-'
    try:
        total=max(0,int(float(value)))
        h,rem=divmod(total,3600); m,sec=divmod(rem,60)
        if h: return f"{h} jam {m} menit {sec} detik"
        return f"{m} menit {sec} detik"
    except Exception:
        return '-'

def leave_duration_seconds(row, end_time=None):
    try:
        start=datetime.fromisoformat(row['out_at'])
        end=datetime.fromisoformat(row['in_at']) if row['in_at'] else (end_time or now())
        return max(0,int((end-start).total_seconds()))
    except Exception:
        return 0

lock=threading.RLock(); bg_started=False

def now(): return datetime.now(WIB)
def totp_code(secret, for_time=None):
    for_time = int(for_time or time.time())
    padded = secret + '=' * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(padded, casefold=True)
    msg = struct.pack('>Q', for_time // 30)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    off = digest[-1] & 0x0F
    val = (struct.unpack('>I', digest[off:off+4])[0] & 0x7fffffff) % 1000000
    return f'{val:06d}'

def totp_verify(secret, code):
    if not secret or not re.fullmatch(r'\d{6}', code or ''): return False
    t=int(time.time())
    return any(hmac.compare_digest(totp_code(secret,t+(i*30)),code) for i in (-1,0,1))

def db_conn():
    c=sqlite3.connect(DB_PATH,timeout=30,check_same_thread=False); c.row_factory=sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA foreign_keys=ON'); c.execute('PRAGMA busy_timeout=30000'); return c

def addcol(c,table,name,decl):
    cols={r['name'] for r in c.execute(f'PRAGMA table_info({table})')}
    if name not in cols: c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {decl}')

def migrate_global_shifts(c):
    """Gabungkan shift lama per-kantor menjadi satu Master Shift global.

    Kolom office_id dipertahankan untuk kompatibilitas database lama, tetapi semua
    shift versi baru selalu office_id=NULL. Referensi assignment/schedule lama
    dipindahkan ke shift global agar riwayat tidak putus.
    """
    rows=c.execute("SELECT * FROM shifts ORDER BY id").fetchall()
    groups={}
    for r in rows:
        key=(r['name'] or '').strip().casefold()
        if key:
            groups.setdefault(key,[]).append(r)
    for key,items in groups.items():
        global_items=[r for r in items if r['office_id'] is None]
        if global_items:
            canonical=global_items[0]
            canonical_id=canonical['id']
        else:
            src=next((r for r in items if (r['status'] or 'Aktif')=='Aktif'),items[0])
            canonical_id=c.execute(
                "INSERT INTO shifts(office_id,name,code,start_time,end_time,status) VALUES(NULL,?,?,?,?,?)",
                ((src['name'] or '').strip(),src['code'] or '',src['start_time'] or '00:00',src['end_time'] or '00:00',src['status'] or 'Aktif')
            ).lastrowid
        # Pindahkan semua referensi dari duplikat lama ke shift global.
        for r in items:
            if r['id']==canonical_id:
                continue
            c.execute('UPDATE shift_schedules SET shift_id=? WHERE shift_id=?',(canonical_id,r['id']))
            c.execute('UPDATE assignments SET shift_id=? WHERE shift_id=?',(canonical_id,r['id']))
            c.execute('DELETE FROM shifts WHERE id=?',(r['id'],))
        c.execute('UPDATE shifts SET office_id=NULL,name=TRIM(name) WHERE id=?',(canonical_id,))
    # Bersihkan kemungkinan duplikat global lama (NULL tidak dibatasi UNIQUE SQLite).
    globals_=c.execute("SELECT * FROM shifts WHERE office_id IS NULL ORDER BY id").fetchall()
    seen={}
    for r in globals_:
        key=(r['name'] or '').strip().casefold()
        if not key: continue
        if key not in seen:
            seen[key]=r['id']; continue
        keep=seen[key]
        c.execute('UPDATE shift_schedules SET shift_id=? WHERE shift_id=?',(keep,r['id']))
        c.execute('UPDATE assignments SET shift_id=? WHERE shift_id=?',(keep,r['id']))
        c.execute('DELETE FROM shifts WHERE id=?',(r['id'],))
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_shifts_global_name ON shifts(lower(name)) WHERE office_id IS NULL")

def init_db():
  with lock,db_conn() as c:
    c.executescript('''
    CREATE TABLE IF NOT EXISTS offices(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT UNIQUE NOT NULL,location TEXT,status TEXT DEFAULT 'Aktif');
    CREATE TABLE IF NOT EXISTS staff(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,telegram_id TEXT UNIQUE,telegram_username TEXT,email TEXT,agent_code TEXT,cs_name TEXT,office_id INTEGER,position TEXT DEFAULT 'CS',status TEXT DEFAULT 'Aktif',join_date TEXT,exit_date TEXT,exit_reason TEXT,notes TEXT,FOREIGN KEY(office_id) REFERENCES offices(id));
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,role TEXT DEFAULT 'staff',staff_id INTEGER UNIQUE,office_id INTEGER,is_active INTEGER DEFAULT 1,must_change_password INTEGER DEFAULT 1,allowed_menus TEXT DEFAULT '["my_dashboard","attendance","inout","nawala","account"]',device_token TEXT,last_login TEXT,twofa_secret TEXT,twofa_enabled INTEGER DEFAULT 0,FOREIGN KEY(staff_id) REFERENCES staff(id),FOREIGN KEY(office_id) REFERENCES offices(id));
    CREATE TABLE IF NOT EXISTS shifts(id INTEGER PRIMARY KEY AUTOINCREMENT,office_id INTEGER,name TEXT NOT NULL,code TEXT,start_time TEXT,end_time TEXT,status TEXT DEFAULT 'Aktif',FOREIGN KEY(office_id) REFERENCES offices(id));
    CREATE TABLE IF NOT EXISTS shift_schedules(id INTEGER PRIMARY KEY AUTOINCREMENT,work_date TEXT NOT NULL,staff_id INTEGER NOT NULL,shift_id INTEGER NOT NULL,office_id INTEGER NOT NULL,UNIQUE(work_date,staff_id),FOREIGN KEY(staff_id) REFERENCES staff(id),FOREIGN KEY(shift_id) REFERENCES shifts(id),FOREIGN KEY(office_id) REFERENCES offices(id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY AUTOINCREMENT,office_id INTEGER,name TEXT NOT NULL,category TEXT NOT NULL,aliases TEXT DEFAULT '',status TEXT DEFAULT 'Aktif',UNIQUE(office_id,name),FOREIGN KEY(office_id) REFERENCES offices(id));
    CREATE TABLE IF NOT EXISTS assignments(id INTEGER PRIMARY KEY AUTOINCREMENT,assignment_batch_id TEXT,work_date TEXT NOT NULL,office_id INTEGER,shift_id INTEGER,staff_id INTEGER,channel_id INTEGER,category TEXT,target TEXT,start_time TEXT,end_time TEXT,is_active INTEGER DEFAULT 1,FOREIGN KEY(office_id) REFERENCES offices(id),FOREIGN KEY(shift_id) REFERENCES shifts(id),FOREIGN KEY(staff_id) REFERENCES staff(id),FOREIGN KEY(channel_id) REFERENCES channels(id));
    CREATE TABLE IF NOT EXISTS offdays(id INTEGER PRIMARY KEY AUTOINCREMENT,staff_id INTEGER,off_date TEXT,notes TEXT,created_at TEXT,UNIQUE(staff_id,off_date),FOREIGN KEY(staff_id) REFERENCES staff(id));
    CREATE TABLE IF NOT EXISTS leaves(id INTEGER PRIMARY KEY AUTOINCREMENT,staff_id INTEGER NOT NULL,reason TEXT,out_at TEXT,expected_at TEXT,in_at TEXT,status TEXT DEFAULT 'OUT',late_minutes INTEGER DEFAULT 0,fine INTEGER DEFAULT 0,source TEXT,notified_overdue INTEGER DEFAULT 0,assignment_snapshot TEXT,FOREIGN KEY(staff_id) REFERENCES staff(id));
    CREATE TABLE IF NOT EXISTS attendance_records(id INTEGER PRIMARY KEY AUTOINCREMENT,staff_id INTEGER NOT NULL,work_date TEXT NOT NULL,clock_in TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'TEPAT WAKTU',late_minutes INTEGER NOT NULL DEFAULT 0,fine INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,created_by INTEGER,UNIQUE(staff_id,work_date),FOREIGN KEY(staff_id) REFERENCES staff(id),FOREIGN KEY(created_by) REFERENCES users(id));
    CREATE TABLE IF NOT EXISTS warnings(id INTEGER PRIMARY KEY AUTOINCREMENT,staff_id INTEGER,type TEXT,warning_date TEXT,reason TEXT,fine INTEGER DEFAULT 0,notes TEXT,created_by INTEGER,created_at TEXT,FOREIGN KEY(staff_id) REFERENCES staff(id));
    CREATE TABLE IF NOT EXISTS memos(id INTEGER PRIMARY KEY AUTOINCREMENT,staff_id INTEGER,title TEXT,category TEXT,body TEXT,priority TEXT DEFAULT 'Normal',status TEXT DEFAULT 'Baru',leader_reply TEXT,created_at TEXT,updated_at TEXT,FOREIGN KEY(staff_id) REFERENCES staff(id));
    CREATE TABLE IF NOT EXISTS devices(device_id TEXT PRIMARY KEY,device_name TEXT,office_id INTEGER,last_seen INTEGER,page_url TEXT,form_count INTEGER DEFAULT 0,late_count INTEGER DEFAULT 0,FOREIGN KEY(office_id) REFERENCES offices(id));
    CREATE TABLE IF NOT EXISTS deposit_forms(id INTEGER PRIMARY KEY AUTOINCREMENT,form_id TEXT UNIQUE,device_id TEXT,office_id INTEGER,username TEXT,game_id TEXT,destination TEXT,destination_account TEXT,destination_owner TEXT,form_time TEXT,amount TEXT,bank TEXT,first_seen INTEGER,last_seen INTEGER,status TEXT DEFAULT 'pending',alert_sent INTEGER DEFAULT 0,staff_id INTEGER,assignment_id INTEGER,staff_status TEXT,processed_at TEXT,staff_name_snapshot TEXT,cs_name_snapshot TEXT,jobdesk_snapshot TEXT,office_snapshot TEXT,age_at_alert INTEGER DEFAULT 0,alerted_at TEXT,mapping_status TEXT,FOREIGN KEY(staff_id) REFERENCES staff(id));
    CREATE TABLE IF NOT EXISTS deposit_alert_staff(id INTEGER PRIMARY KEY AUTOINCREMENT,deposit_form_id INTEGER NOT NULL,staff_id INTEGER NOT NULL,assignment_id INTEGER,staff_name_snapshot TEXT,cs_name_snapshot TEXT,office_snapshot TEXT,jobdesk_snapshot TEXT,shift_id_snapshot INTEGER,shift_name_snapshot TEXT,created_at TEXT,UNIQUE(deposit_form_id,staff_id),FOREIGN KEY(deposit_form_id) REFERENCES deposit_forms(id) ON DELETE CASCADE,FOREIGN KEY(staff_id) REFERENCES staff(id));
    CREATE TABLE IF NOT EXISTS withdraw_forms(id INTEGER PRIMARY KEY AUTOINCREMENT,form_id TEXT UNIQUE,device_id TEXT,username TEXT,form_time TEXT,amount TEXT,bank TEXT,bank_account TEXT,bank_owner TEXT,first_seen INTEGER,last_seen INTEGER,status TEXT DEFAULT 'pending',alert_sent INTEGER DEFAULT 0,age_at_alert INTEGER DEFAULT 0,alerted_at TEXT,mapping_status TEXT,jobdesk_snapshot TEXT);
    CREATE TABLE IF NOT EXISTS withdraw_alert_staff(id INTEGER PRIMARY KEY AUTOINCREMENT,withdraw_form_id INTEGER NOT NULL,staff_id INTEGER NOT NULL,assignment_id INTEGER,staff_name_snapshot TEXT,cs_name_snapshot TEXT,office_snapshot TEXT,jobdesk_snapshot TEXT,shift_id_snapshot INTEGER,shift_name_snapshot TEXT,created_at TEXT,UNIQUE(withdraw_form_id,staff_id),FOREIGN KEY(withdraw_form_id) REFERENCES withdraw_forms(id) ON DELETE CASCADE,FOREIGN KEY(staff_id) REFERENCES staff(id));
    CREATE TABLE IF NOT EXISTS audit_logs(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,action TEXT,detail TEXT,created_at TEXT,before_data TEXT,after_data TEXT,target_type TEXT,target_id TEXT,ip_address TEXT,user_agent TEXT);
    CREATE TABLE IF NOT EXISTS jobdesk_history(id INTEGER PRIMARY KEY AUTOINCREMENT,staff_id INTEGER NOT NULL,channel_id INTEGER,jobdesk_name TEXT NOT NULL,category TEXT,event TEXT DEFAULT 'ASSIGNED',started_at TEXT NOT NULL,ended_at TEXT,changed_by INTEGER,FOREIGN KEY(staff_id) REFERENCES staff(id),FOREIGN KEY(channel_id) REFERENCES channels(id));
    CREATE TABLE IF NOT EXISTS login_logs(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,staff_id INTEGER,event TEXT,ip_address TEXT,user_agent TEXT,detail TEXT,created_at TEXT,FOREIGN KEY(user_id) REFERENCES users(id),FOREIGN KEY(staff_id) REFERENCES staff(id));
    CREATE TABLE IF NOT EXISTS mistake_ledger(id INTEGER PRIMARY KEY AUTOINCREMENT,staff_id INTEGER NOT NULL,entry_date TEXT NOT NULL,entry_type TEXT NOT NULL,amount INTEGER NOT NULL DEFAULT 0,title TEXT,notes TEXT,staff_note TEXT,created_by INTEGER,created_at TEXT,updated_at TEXT,FOREIGN KEY(staff_id) REFERENCES staff(id));
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS rules_acceptances(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,staff_id INTEGER,decision TEXT NOT NULL,accepted_at TEXT,ip_address TEXT,user_agent TEXT,FOREIGN KEY(user_id) REFERENCES users(id),FOREIGN KEY(staff_id) REFERENCES staff(id));
    CREATE TABLE IF NOT EXISTS policy_acceptances(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,policy_version TEXT NOT NULL,decision TEXT NOT NULL,decided_at TEXT NOT NULL,ip_address TEXT,user_agent TEXT,UNIQUE(user_id,policy_version),FOREIGN KEY(user_id) REFERENCES users(id));
    CREATE TABLE IF NOT EXISTS rule_acceptances(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,decision TEXT NOT NULL,rules_version TEXT NOT NULL,created_at TEXT NOT NULL,ip_address TEXT,user_agent TEXT,FOREIGN KEY(user_id) REFERENCES users(id));
    ''')
    # Idempotent migrations for databases created by older releases.
    # CREATE TABLE IF NOT EXISTS does not add new columns to an existing table,
    # therefore every column used by the current application is checked here.
    migrations = {
      'offices': [('location','TEXT'),('status',"TEXT DEFAULT 'Aktif'")],
      'staff': [('gender',"TEXT DEFAULT 'Pria'"),('telegram_id','TEXT'),('telegram_username','TEXT'),('email','TEXT'),('agent_code','TEXT'),('cs_name','TEXT'),('office_id','INTEGER'),('position',"TEXT DEFAULT 'CS'"),('status',"TEXT DEFAULT 'Aktif'"),('join_date','TEXT'),('exit_date','TEXT'),('exit_reason','TEXT'),('notes','TEXT')],
      'users': [('role',"TEXT DEFAULT 'staff'"),('staff_id','INTEGER'),('office_id','INTEGER'),('is_active','INTEGER DEFAULT 1'),('must_change_password','INTEGER DEFAULT 1'),('allowed_menus',"TEXT DEFAULT '[\"my_dashboard\",\"inout\",\"mistakes\",\"history\",\"account\"]'"),('device_token','TEXT'),('last_login','TEXT'),('twofa_secret','TEXT'),('twofa_enabled','INTEGER DEFAULT 0')],
      'shifts': [('office_id','INTEGER'),('name','TEXT'),('code','TEXT'),('start_time','TEXT'),('end_time','TEXT'),('status',"TEXT DEFAULT 'Aktif'")],
      'shift_schedules': [('work_date','TEXT'),('staff_id','INTEGER'),('shift_id','INTEGER'),('office_id','INTEGER')],
      'channels': [('office_id','INTEGER'),('name','TEXT'),('category','TEXT'),('aliases',"TEXT DEFAULT ''"),('status',"TEXT DEFAULT 'Aktif'")],
      'assignments': [('assignment_batch_id','TEXT'),('work_date','TEXT'),('office_id','INTEGER'),('shift_id','INTEGER'),('staff_id','INTEGER'),('channel_id','INTEGER'),('category','TEXT'),('target','TEXT'),('start_time','TEXT'),('end_time','TEXT'),('is_active','INTEGER DEFAULT 1')],
      'offdays': [('staff_id','INTEGER'),('off_date','TEXT'),('notes','TEXT'),('created_at','TEXT')],
      'leaves': [('staff_id','INTEGER'),('reason','TEXT'),('out_at','TEXT'),('expected_at','TEXT'),('in_at','TEXT'),('status',"TEXT DEFAULT 'OUT'"),('late_minutes','INTEGER DEFAULT 0'),('fine','INTEGER DEFAULT 0'),('source','TEXT'),('notified_overdue','INTEGER DEFAULT 0'),('assignment_snapshot','TEXT'),('auto_in','INTEGER DEFAULT 0'),('device_token','TEXT')],
      'warnings': [('staff_id','INTEGER'),('type','TEXT'),('warning_date','TEXT'),('reason','TEXT'),('fine','INTEGER DEFAULT 0'),('notes','TEXT'),('created_by','INTEGER'),('created_at','TEXT')],
      'memos': [('staff_id','INTEGER'),('title','TEXT'),('category','TEXT'),('body','TEXT'),('priority',"TEXT DEFAULT 'Normal'"),('status',"TEXT DEFAULT 'Baru'"),('leader_reply','TEXT'),('created_at','TEXT'),('updated_at','TEXT')],
      'devices': [('device_name','TEXT'),('office_id','INTEGER'),('last_seen','INTEGER'),('page_url','TEXT'),('form_count','INTEGER DEFAULT 0'),('late_count','INTEGER DEFAULT 0')],
      'deposit_forms': [('device_id','TEXT'),('office_id','INTEGER'),('username','TEXT'),('game_id','TEXT'),('destination','TEXT'),('destination_account','TEXT'),('destination_owner','TEXT'),('form_time','TEXT'),('amount','TEXT'),('bank','TEXT'),('first_seen','INTEGER'),('last_seen','INTEGER'),('status',"TEXT DEFAULT 'pending'"),('alert_sent','INTEGER DEFAULT 0'),('staff_id','INTEGER'),('assignment_id','INTEGER'),('staff_status','TEXT'),('processed_at','TEXT'),('staff_name_snapshot','TEXT'),('cs_name_snapshot','TEXT'),('jobdesk_snapshot','TEXT'),('office_snapshot','TEXT'),('age_at_alert','INTEGER DEFAULT 0'),('alerted_at','TEXT'),('mapping_status','TEXT'),('balance_group','TEXT')],
      'deposit_alert_staff': [('shift_id_snapshot','INTEGER'),('shift_name_snapshot','TEXT')],
      'withdraw_alert_staff': [('shift_id_snapshot','INTEGER'),('shift_name_snapshot','TEXT')],
      'audit_logs': [('user_id','INTEGER'),('action','TEXT'),('detail','TEXT'),('created_at','TEXT'),('before_data','TEXT'),('after_data','TEXT'),('target_type','TEXT'),('target_id','TEXT'),('ip_address','TEXT'),('user_agent','TEXT')],
      'jobdesk_history': [('staff_id','INTEGER'),('channel_id','INTEGER'),('jobdesk_name','TEXT'),('category','TEXT'),('event',"TEXT DEFAULT 'ASSIGNED'"),('started_at','TEXT'),('ended_at','TEXT'),('changed_by','INTEGER')],
    }
    for table, columns in migrations.items():
      for name, decl in columns:
        addcol(c, table, name, decl)
    # Fill safe defaults for rows originating from legacy schemas.
    c.execute("UPDATE shifts SET code=COALESCE(NULLIF(code,''), 'SHIFT-' || id), status=COALESCE(NULLIF(status,''),'Aktif')")
    migrate_global_shifts(c)
    c.execute("UPDATE offices SET status=COALESCE(NULLIF(status,''),'Aktif')")
    c.execute("DELETE FROM deposit_forms WHERE form_id IS NOT NULL AND form_id<>'' AND id NOT IN (SELECT MIN(id) FROM deposit_forms WHERE form_id IS NOT NULL AND form_id<>'' GROUP BY form_id)")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_deposit_forms_form_id ON deposit_forms(form_id) WHERE form_id IS NOT NULL AND form_id<>''")
    c.execute('CREATE INDEX IF NOT EXISTS idx_deposit_forms_alert ON deposit_forms(alert_sent,first_seen)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_deposit_forms_staff ON deposit_forms(staff_id,first_seen)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_deposit_alert_staff_staff ON deposit_alert_staff(staff_id,deposit_form_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_deposit_alert_staff_form ON deposit_alert_staff(deposit_form_id)')
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_withdraw_forms_form_id ON withdraw_forms(form_id) WHERE form_id IS NOT NULL AND form_id<>''")
    c.execute('CREATE INDEX IF NOT EXISTS idx_withdraw_forms_alert ON withdraw_forms(alert_sent,first_seen)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_withdraw_alert_staff_staff ON withdraw_alert_staff(staff_id,withdraw_form_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_withdraw_alert_staff_form ON withdraw_alert_staff(withdraw_form_id)')
    c.execute("""INSERT OR IGNORE INTO deposit_alert_staff(deposit_form_id,staff_id,assignment_id,staff_name_snapshot,cs_name_snapshot,office_snapshot,jobdesk_snapshot,created_at) SELECT id,staff_id,assignment_id,staff_name_snapshot,cs_name_snapshot,office_snapshot,jobdesk_snapshot,COALESCE(alerted_at,datetime('now')) FROM deposit_forms WHERE alert_sent=1 AND staff_id IS NOT NULL""")
    c.execute('CREATE INDEX IF NOT EXISTS idx_assignments_current ON assignments(work_date,office_id,is_active,staff_id,channel_id)')
    if not c.execute("SELECT 1 FROM offices WHERE name='Kantor Utama' LIMIT 1").fetchone(): c.execute("INSERT INTO offices(name,location,status) VALUES('Kantor Utama','-','Aktif')")
    oid=c.execute('SELECT id FROM offices ORDER BY id LIMIT 1').fetchone()[0]
    # Jangan bergantung pada UNIQUE constraint database lama; cek manual supaya startup tidak membuat shift duplikat.
    if not c.execute("SELECT 1 FROM shifts WHERE office_id IS NULL AND lower(name)=lower(?) LIMIT 1",('Pagi',)).fetchone(): c.execute('INSERT INTO shifts(office_id,name,code,start_time,end_time,status) VALUES(NULL,?,?,?,?,?)',('Pagi','P1','06:00','18:00','Aktif'))
    if not c.execute("SELECT 1 FROM shifts WHERE office_id IS NULL AND lower(name)=lower(?) LIMIT 1",('Malam',)).fetchone(): c.execute('INSERT INTO shifts(office_id,name,code,start_time,end_time,status) VALUES(NULL,?,?,?,?,?)',('Malam','M1','18:00','06:00','Aktif'))
    for k,v in {'late_minutes':str(LATE_MINUTES),'scan_seconds':str(SCAN_SECONDS)}.items(): c.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)',(k,v))
    env_admin=c.execute('SELECT * FROM users WHERE username=?',(ADMIN_USERNAME,)).fetchone()
    if not env_admin:
      c.execute('INSERT INTO users(username,password_hash,role,office_id,is_active,must_change_password,allowed_menus) VALUES(?,?,?,?,1,0,?)',(ADMIN_USERNAME,generate_password_hash(ADMIN_PASSWORD),'superadmin',oid,json.dumps(['*'])))
    else:
      # Railway ENV adalah sumber login Master. Sinkronkan setiap startup agar Volume lama tidak mengunci password lama.
      c.execute("UPDATE users SET password_hash=?,role='superadmin',is_active=1,must_change_password=0,allowed_menus=? WHERE id=?",(generate_password_hash(ADMIN_PASSWORD),json.dumps(['*']),env_admin['id']))
    # Migrasi sekali dari penugasan versi harian lama ke penugasan aktif CURRENT.
    for st in c.execute("SELECT id FROM staff WHERE status='Aktif'").fetchall():
      sid=st['id']
      if c.execute("SELECT 1 FROM shift_schedules WHERE work_date='CURRENT' AND staff_id=?",(sid,)).fetchone(): continue
      legacy=c.execute("SELECT * FROM shift_schedules WHERE staff_id=? AND work_date!='CURRENT' ORDER BY id DESC LIMIT 1",(sid,)).fetchone()
      if not legacy: continue
      c.execute("INSERT OR IGNORE INTO shift_schedules(work_date,staff_id,shift_id,office_id) VALUES('CURRENT',?,?,?)",(sid,legacy['shift_id'],legacy['office_id']))
      jobs=c.execute("SELECT * FROM assignments WHERE staff_id=? AND work_date=? AND is_active=1 ORDER BY id",(sid,legacy['work_date'])).fetchall()
      if jobs:
       batch=uuid.uuid4().hex
       for a in jobs:
        c.execute("INSERT INTO assignments(assignment_batch_id,work_date,office_id,shift_id,staff_id,channel_id,category,target,start_time,end_time,is_active) VALUES(?,?,?,?,?,?,?,?,?,?,1)",(batch,'CURRENT',a['office_id'],a['shift_id'],sid,a['channel_id'],a['category'],a['target'],a['start_time'],a['end_time']))
    # Backfill penugasan CURRENT agar Detail Staf langsung punya riwayat aktif setelah upgrade.
    for a in c.execute("""SELECT a.staff_id,a.channel_id,COALESCE(ch.name,a.target,'-') jobdesk_name,COALESCE(ch.category,a.category,'Lainnya') category FROM assignments a LEFT JOIN channels ch ON ch.id=a.channel_id WHERE a.work_date='CURRENT' AND a.is_active=1""").fetchall():
      if not c.execute('SELECT 1 FROM jobdesk_history WHERE staff_id=? AND channel_id=? AND ended_at IS NULL',(a['staff_id'],a['channel_id'])).fetchone():
       c.execute('INSERT INTO jobdesk_history(staff_id,channel_id,jobdesk_name,category,event,started_at,changed_by) VALUES(?,?,?,?,?,?,NULL)',(a['staff_id'],a['channel_id'],a['jobdesk_name'],a['category'],'ASSIGNED',now().isoformat()))
    c.commit()

def audit(c,action,detail='',before=None,after=None,target_type='',target_id=''):
 uid=g.user['id'] if getattr(g,'user',None) else None
 ip=(request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip() if request else ''
 ua=(request.headers.get('User-Agent','')[:500] if request else '')
 def dump(v):
  if v is None:return ''
  if isinstance(v,sqlite3.Row):v=dict(v)
  try:return json.dumps(v,ensure_ascii=False,default=str,sort_keys=True)
  except Exception:return str(v)
 c.execute('''INSERT INTO audit_logs(user_id,action,detail,created_at,before_data,after_data,target_type,target_id,ip_address,user_agent) VALUES(?,?,?,?,?,?,?,?,?,?)''',(uid,action,str(detail or ''),now().isoformat(),dump(before),dump(after),str(target_type or ''),str(target_id or ''),ip,ua))
 if uid and not action.startswith('policy.'):
  c.execute('INSERT INTO login_logs(user_id,staff_id,event,ip_address,user_agent,detail,created_at) VALUES(?,?,?,?,?,?,?)',(uid,g.user['staff_id'], 'EDIT', ip, ua, action+' · '+str(detail)[:500], now().isoformat()))
def tg_send(chat_id,text):
  if not BOT_TOKEN or not chat_id:return False
  try:return requests.post(f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage',json={'chat_id':chat_id,'text':text,'parse_mode':'HTML'},timeout=15).ok
  except Exception:return False

def tg_send_inout(text):
  targets=[]
  if INOUT_CHAT_ID: targets.append(str(INOUT_CHAT_ID))
  targets.extend(str(x) for x in INOUT_ADMIN_IDS)
  sent=False
  for chat_id in dict.fromkeys(targets):
   sent = tg_send(chat_id,text) or sent
  return sent

def tg_send_attendance(text):
  if not ATTENDANCE_CHAT_ID:return False
  return tg_send(ATTENDANCE_CHAT_ID,text)

def tg_send_inout_admins(text):
  """Khusus notifikasi sensitif (AUTO IN): hanya ke ADMIN_IDS, bukan grup IN/OUT."""
  sent=False
  for chat_id in dict.fromkeys(str(x) for x in INOUT_ADMIN_IDS if str(x).strip()):
   sent = tg_send(chat_id,text) or sent
  return sent

def process_auto_in_overdue(c, current=None):
    """Close OUT sessions that are >=10 full minutes past expected_at.

    This is deliberately callable from both the background worker and request paths
    so AUTO IN does not depend on a single daemon thread staying healthy.
    Returns Telegram notification texts for rows actually closed by this call.
    """
    current=current or now()
    events=[]
    rows=c.execute("SELECT l.*,s.name,s.cs_name FROM leaves l JOIN staff s ON s.id=l.staff_id WHERE l.status='OUT'").fetchall()
    for l in rows:
        try:
            exp=datetime.fromisoformat(str(l['expected_at']))
            if exp.tzinfo is None:
                exp=exp.replace(tzinfo=WIB)
            else:
                exp=exp.astimezone(WIB)
            overdue_sec=(current-exp).total_seconds()
            if overdue_sec < 600:
                continue
            duration_sec=leave_duration_seconds(l,current)
            late=max(10,int(overdue_sec//60))
            cur=c.execute("UPDATE leaves SET in_at=?,status='AUTO_IN',late_minutes=?,fine=500000,auto_in=1 WHERE id=? AND status='OUT'",(current.isoformat(),late,l['id']))
            if cur.rowcount:
                dm,ds=divmod(duration_sec,60); dh,dm=divmod(dm,60)
                duration_text=(f"{dh} jam {dm} menit {ds} detik" if dh else f"{dm} menit {ds} detik")
                events.append(f"⚠️ <b>AUTO IN</b>\n👤 {l['name']} — {l['cs_name'] or '-'}\n📝 {str(l['reason']).title()}\n⏱ Durasi keluar: {duration_text}\n💸 Denda otomatis: Rp500.000\n✅ Slot OUT telah dibebaskan.")
        except Exception as row_error:
            print('[auto-in-row]', l['id'], row_error, flush=True)
    return events

def flush_auto_in_notifications(events):
    for text in events:
        try:
            tg_send_inout_admins(text)
        except Exception as notify_error:
            print('[auto-in-notify]', notify_error, flush=True)

@app.before_request
def before():
 g.user=None; g.policy_pending=False; g.theme=session.get('theme','dark'); g.policy_config=None
 # Auto logout setelah 5 jam tanpa aktivitas untuk semua akun (Master/Leader/SPV/Staf).
 # Timestamp disimpan di session browser, jadi aktivitas di device lain tidak memperpanjang sesi ini.
 if session.get('uid'):
  try:
   last_activity=float(session.get('last_activity_ts') or 0)
  except Exception:
   last_activity=0
  current_ts=time.time()
  if last_activity and current_ts-last_activity >= 5*60*60:
   expired_uid=session.get('uid')
   try:
    with db_conn() as lc:
     u0=lc.execute('SELECT id,staff_id FROM users WHERE id=?',(expired_uid,)).fetchone()
     if u0:
      lc.execute('INSERT INTO login_logs(user_id,staff_id,event,ip_address,user_agent,detail,created_at) VALUES(?,?,?,?,?,?,?)',(u0['id'],u0['staff_id'],'AUTO_LOGOUT',request.headers.get('X-Forwarded-For',request.remote_addr),request.headers.get('User-Agent','')[:500],'Tidak ada aktivitas selama 5 jam',now().isoformat()))
      lc.commit()
   except Exception as idle_log_error:
    print('[idle-logout-log]',idle_log_error,flush=True)
   session.clear()
   flash('Sesi berakhir karena tidak ada aktivitas selama 5 jam. Silakan login kembali.','danger')
   return redirect(url_for('login'))
  # Polling realtime /api/* dan asset static tidak dihitung sebagai aktivitas user.
  if not request.path.startswith('/api/') and request.endpoint!='static':
   session['last_activity_ts']=current_ts
  with db_conn() as c:
   g.user=c.execute('SELECT * FROM users WHERE id=? AND is_active=1',(session['uid'],)).fetchone()
   g.allowed_menus=[]
   if g.user:
    try:g.allowed_menus=json.loads(g.user['allowed_menus'] or '[]')
    except Exception:g.allowed_menus=[]
   if g.user:
    # Peraturan disimpan global di settings dan hanya dapat diedit Super Admin.
    def gs(k,default=''):
     r=c.execute('SELECT value FROM settings WHERE key=?',(k,)).fetchone(); return r['value'] if r else default
    version=gs('policy.version',POLICY_VERSION)
    try: rules=json.loads(gs('policy.rules','[]'))
    except Exception: rules=[]
    if not rules: rules=list(DEFAULT_POLICY_RULES)
    g.policy_config={'version':version,'title':gs('policy.title','PERATURAN KANTOR'),'subtitle':gs('policy.subtitle','WAJIB DIBACA DAN DIPATUHI'),'warning':gs('policy.warning','Jangan langgar aturan yang sudah ditetapkan atau akan ada konsekuensinya!'),'message':gs('policy.message','Selamat Bekerja Tetap Fokus ☺♥'),'enabled':gs('policy.enabled','1')=='1','rules':rules}
    g.policy_pending=bool(session.get('policy_pending')) and g.policy_config['enabled']
   # Anti double-device saat staf sedang OUT. Session lama pada device lain ikut ditolak.
   if g.user and g.user['role']=='staff' and g.user['staff_id'] and request.endpoint not in ('login','logout','static'):
    active_leave=c.execute("SELECT device_token FROM leaves WHERE staff_id=? AND status='OUT' ORDER BY id DESC LIMIT 1",(g.user['staff_id'],)).fetchone()
    if active_leave:
     expected=(active_leave['device_token'] or g.user['device_token'] or '').strip()
     # Device yang membuat OUT tetap boleh terus memakai dashboard untuk menekan IN.
     # Token session diprioritaskan supaya POST IZIN OUT tidak membuat browser yang sama dianggap device baru.
     current=(session.get('browser_device_token') or request.cookies.get('om_device_id') or '').strip()
     if not expected or current!=expected:
      session.clear(); flash('Tidak bisa beda device login','danger'); return redirect(url_for('login'))


def dp_sites():
    path=os.path.join(DP_DATA_DIR,'sites.json')
    try:
        with open(path,'r',encoding='utf-8') as f:
            rows=json.load(f)
        if not isinstance(rows,list): return []
        return [dict(x) for x in rows if isinstance(x,dict)]
    except Exception:
        return []

def attendance_window(dt=None):
    dt=dt or now()
    d=dt.date()
    def at(hms):
        h,m,sec=[int(x) for x in hms.split(':')]
        return datetime(d.year,d.month,d.day,h,m,sec,tzinfo=WIB)
    return {
        'open': at(ATTENDANCE_OPEN+':00'),
        'late_start': at(ATTENDANCE_LATE_START),
        'close': at(ATTENDANCE_CLOSE),
        'label_open': ATTENDANCE_OPEN,
        'label_on_time': ATTENDANCE_ON_TIME_UNTIL[:5],
        'label_close': ATTENDANCE_CLOSE[:5],
    }

def attendance_status_for(dt=None):
    dt=dt or now(); w=attendance_window(dt)
    if dt < w['open']: return ('BELUM DIBUKA',0,0)
    if dt >= w['close']: return ('DITUTUP',0,0)
    if dt < w['late_start']: return ('TEPAT WAKTU',0,0)
    late=max(1,int((dt-w['late_start']).total_seconds()//60)+1)
    return ('TELAT',late,late*ATTENDANCE_FINE_PER_MINUTE)

@app.context_processor
def inject_crm_globals():
    role=(g.user['role'] if getattr(g,'user',None) else '')
    websites=[x for x in dp_sites() if x.get('active',True)] if role in ('superadmin','supervisor','leader') else []
    return {'crm_websites':websites,'attendance_open':ATTENDANCE_OPEN,'attendance_close':ATTENDANCE_CLOSE}

def login_required(fn):
 @wraps(fn)
 def w(*a,**k): return redirect(url_for('login')) if not g.user else fn(*a,**k)
 return w

def roles(*allowed):
 def deco(fn):
  @wraps(fn)
  def w(*a,**k):
   if not g.user:return redirect(url_for('login'))
   if g.user['role'] not in allowed:return ('Akses ditolak',403)
   return fn(*a,**k)
  return w
 return deco

def menu_allowed(menu):
 if not g.user:return False
 if g.user['role']!='staff':return True
 return '*' in getattr(g,'allowed_menus',[]) or menu in getattr(g,'allowed_menus',[])

def active_shift(c,office_id,when=None):
 when=when or now(); hm=when.strftime('%H:%M')
 for s in c.execute("SELECT * FROM shifts WHERE office_id IS NULL AND status='Aktif' ORDER BY start_time,name"):
  st=s['start_time']; en=s['end_time']; ok=(st<=hm<en) if st<en else (hm>=st or hm<en)
  if ok:return s
 return None


def staff_active_assignments(c,staff_id,when=None):
 when=when or now(); hm=when.strftime('%H:%M')
 rows=c.execute("""SELECT a.*,ch.name channel_name FROM assignments a LEFT JOIN channels ch ON ch.id=a.channel_id WHERE a.staff_id=? AND a.work_date='CURRENT' AND a.is_active=1 ORDER BY ch.category,ch.name""",(staff_id,)).fetchall()
 active=[]
 for r in rows:
  st=r['start_time'] or '00:00'; en=r['end_time'] or '23:59'; ok=(st<=hm<en) if st<en else (hm>=st or hm<en)
  if ok: active.append(r)
 return active

def find_assignment(c,office_id,target,when=None):
 when=when or now(); hm=when.strftime('%H:%M'); target_norm=re.sub(r'[^a-z0-9]','',target.lower())
 rows=c.execute('''SELECT a.*,s.name staff_name,s.cs_name,s.telegram_id,s.agent_code,o.name office_name,o.location,sh.name shift_name,ch.name channel_name,ch.aliases
 FROM assignments a JOIN staff s ON s.id=a.staff_id LEFT JOIN offices o ON o.id=a.office_id LEFT JOIN shifts sh ON sh.id=a.shift_id LEFT JOIN channels ch ON ch.id=a.channel_id
 WHERE a.office_id=? AND a.work_date='CURRENT' AND a.is_active=1 AND s.status='Aktif' ''',(office_id,)).fetchall()
 for r in rows:
  st=r['start_time'] or '00:00'; en=r['end_time'] or '23:59'; ok=(st<=hm<en) if st<en else (hm>=st or hm<en)
  names=[r['channel_name'] or '',r['target'] or '']+[x.strip() for x in (r['aliases'] or '').split(',')]
  if ok and any(re.sub(r'[^a-z0-9]','',x.lower()) in target_norm or target_norm in re.sub(r'[^a-z0-9]','',x.lower()) for x in names if x): return r
 return None


def _norm_key(value):
 return re.sub(r'[^a-z0-9]+','',str(value or '').lower())

BANK_ALIASES=(
 ('DANAMON',('DANAMON','BANK DANAMON')),
 ('DANA',('DANA',)),
 ('BCA',('BCA','BANK BCA')),
 ('BRI',('BRI','BANK BRI')),
 ('BNI',('BNI','BANK BNI')),
 ('MANDIRI',('MANDIRI','BANK MANDIRI')),
 ('SEABANK',('SEABANK','SEA BANK','BANK SEABANK')),
 ('QRIS',('QRIS',)),
 ('OVO',('OVO',)),
 ('GOPAY',('GOPAY','GO PAY')),
 ('CIMB',('CIMB','CIMB NIAGA','BANK CIMB')),
)

def _bank_text(value):
 """Normalisasi teks bank tanpa membuat DANA cocok ke DANAMON."""
 return re.sub(r'\\s+',' ',re.sub(r'[^A-Z0-9]+',' ',str(value or '').upper())).strip()

def _has_bank_alias(text,alias):
 text=_bank_text(text); alias=_bank_text(alias)
 if not text or not alias:return False
 # Token/phrase boundary: DANA tidak akan match DANAMON.
 return re.search(r'(?<![A-Z0-9])'+re.escape(alias).replace(r'\\ ',r'\\s+')+r'(?![A-Z0-9])',text) is not None

def _bank_tokens(value):
 text=_bank_text(value)
 found=set()
 for canon,aliases in BANK_ALIASES:
  if any(_has_bank_alias(text,a) for a in aliases):
   found.add(canon)
 return found

def _source_bank_name(value):
 """Normalisasi bank/channel untuk jobdesk. DANA dan DANAMON wajib berbeda."""
 raw=str(value or '').strip()
 text=_bank_text(raw)
 if not text:return ''
 # Alias spesifik diletakkan lebih dulu; matcher tetap exact token/phrase.
 for canon,aliases in BANK_ALIASES:
  if any(_has_bank_alias(text,a) for a in aliases):
   return canon
 # Channel yang belum ada di daftar (mis. TELKOMSEL/SCAN BARCODE) tetap dipertahankan.
 return text

def _parse_info_to(value):
 """Fallback server: ambil tujuan dari Info -> To: tanpa membatasi jenis bank/channel."""
 raw=str(value or '').replace('\r',' ')
 m=re.search(r'(?:^|\s)To\s*:\s*(.+)$',raw,re.I|re.S)
 if not m:return {'raw':'','bank':'','account':'','owner':''}
 to=re.sub(r'\s+(?:Select|Approve|Reject|Action|Profil)\b.*$','',m.group(1).strip(),flags=re.I|re.S).strip()
 parts=[x.strip() for x in to.split(',')]
 bank=parts[0] if parts else ''
 account=parts[1] if len(parts)>1 and parts[1] else '-'
 owner=', '.join(x for x in parts[2:] if x).strip() if len(parts)>2 else ''
 return {'raw':to,'bank':bank or '-','account':account or '-','owner':owner or '-'}

def find_deposit_assignment_global(c,data,when=None):
 """Cari SEMUA staf aktif pemegang jobdesk Gx-BANK.
 Gx = kolom Balance pada baris form yang sama.
 BANK = Info -> To: pada baris form yang sama.
 """
 when=when or now(); hm=when.strftime('%H:%M')
 raw_group=' '.join(str(data.get(k) or '') for k in ('balanceGroup','group','balanceText'))
 gm=re.search(r'\bG\s*([0-9]+)\b',raw_group,re.I)
 group=f"G{gm.group(1)}" if gm else ''
 target_bank=_source_bank_name(data.get('targetBank') or data.get('destination') or data.get('bank') or '')
 expected=f"{group}-{target_bank}" if group and target_bank else ''
 expected_norm=_norm_key(expected)
 if not expected_norm:return [],'NO_GROUP_OR_BANK',expected
 rows=c.execute("""SELECT a.*,s.name staff_name,s.cs_name,s.telegram_id,s.agent_code,
 o.name office_name,o.location,sh.name shift_name,ch.name channel_name,ch.aliases,ch.category
 FROM assignments a JOIN staff s ON s.id=a.staff_id
 LEFT JOIN offices o ON o.id=s.office_id LEFT JOIN shifts sh ON sh.id=a.shift_id
 LEFT JOIN channels ch ON ch.id=a.channel_id
 WHERE a.work_date='CURRENT' AND a.is_active=1 AND s.status='Aktif'""").fetchall()
 matches=[]
 for r in rows:
  st=r['start_time'] or '00:00'; en=r['end_time'] or '23:59'
  active=(st<=hm<en) if st<en else (hm>=st or hm<en)
  if not active:continue
  names=[r['channel_name'] or '',r['target'] or '']+[x.strip() for x in (r['aliases'] or '').split(',') if x.strip()]
  exact=any(_norm_key(x)==expected_norm for x in names if x)
  if not exact:
   label=' '.join(names); lg=re.search(r'\bG\s*([0-9]+)\b',label,re.I)
   exact=bool(lg and f"G{lg.group(1)}"==group and target_bank in _bank_tokens(label))
  if exact:matches.append(r)
 # satu staf hanya dihitung sekali walau punya data assignment duplikat
 by_staff={}
 for r in matches:
  sid=int(r['staff_id'])
  if sid not in by_staff or int(r['id'] or 0)>int(by_staff[sid]['id'] or 0):by_staff[sid]=r
 result=list(by_staff.values())
 result.sort(key=lambda r:((r['cs_name'] or r['staff_name'] or '').lower(),int(r['staff_id'])))
 return result,('MATCHED_MULTI' if len(result)>1 else 'MATCHED' if result else 'NO_MATCH'),expected


def find_withdraw_assignment_global(c,data,when=None):
 """Cari semua staf aktif pemegang jobdesk WD BANK berdasarkan Bank Asal form Withdraw."""
 when=when or now(); hm=when.strftime('%H:%M')
 bank=_source_bank_name(data.get('bank') or data.get('bankOrigin') or data.get('sourceBank') or '')
 expected=f"WD-{bank}" if bank else ''
 expected_norm=_norm_key(expected)
 if not expected_norm:return [],'NO_BANK',expected
 rows=c.execute("SELECT a.*,s.name staff_name,s.cs_name,s.telegram_id,s.agent_code,o.name office_name,o.location,sh.name shift_name,ch.name channel_name,ch.aliases,ch.category FROM assignments a JOIN staff s ON s.id=a.staff_id LEFT JOIN offices o ON o.id=s.office_id LEFT JOIN shifts sh ON sh.id=a.shift_id LEFT JOIN channels ch ON ch.id=a.channel_id WHERE a.work_date='CURRENT' AND a.is_active=1 AND s.status='Aktif'").fetchall()
 matches=[]
 for r in rows:
  st=r['start_time'] or '00:00'; en=r['end_time'] or '23:59'
  active=(st<=hm<en) if st<en else (hm>=st or hm<en)
  if not active:continue
  names=[r['channel_name'] or '',r['target'] or '']+[x.strip() for x in (r['aliases'] or '').split(',') if x.strip()]
  exact=any(_norm_key(x)==expected_norm for x in names if x)
  if not exact:
   label=' '.join(names).upper(); exact=('WD' in label and bank in _bank_tokens(label))
  if exact:matches.append(r)
 by_staff={}
 for r in matches:
  sid=int(r['staff_id'])
  if sid not in by_staff or int(r['id'] or 0)>int(by_staff[sid]['id'] or 0):by_staff[sid]=r
 result=list(by_staff.values()); result.sort(key=lambda r:((r['cs_name'] or r['staff_name'] or '').lower(),int(r['staff_id'])))
 return result,('MATCHED_MULTI' if len(result)>1 else 'MATCHED' if result else 'NO_MATCH'),expected

@app.route('/login',methods=['GET','POST'])
def login():
 if request.method=='POST':
  with db_conn() as c:
   u=c.execute('SELECT * FROM users WHERE username=? AND is_active=1',(request.form['username'].strip(),)).fetchone()
   if not u or not check_password_hash(u['password_hash'],request.form['password']): flash('ID atau password salah.','danger'); return render_template('login.html')
   if u['role']=='staff' and any(x in request.headers.get('User-Agent','').lower() for x in ['android','iphone','ipad','mobile']): flash('Akun staf hanya dapat login dari PC.','danger'); return render_template('login.html')
   device_cookie=(request.cookies.get('om_device_id') or '').strip()
   device_token=device_cookie or secrets.token_urlsafe(24)
   if u['role']=='staff' and u['staff_id']:
    active_leave=c.execute("SELECT * FROM leaves WHERE staff_id=? AND status='OUT' ORDER BY id DESC LIMIT 1",(u['staff_id'],)).fetchone()
    if active_leave:
     expected_token=(active_leave['device_token'] or u['device_token'] or '').strip()
     # Saat sedang OUT, hanya browser/device yang membuat OUT tersebut yang boleh login ulang.
     if not device_cookie or not expected_token or device_cookie!=expected_token:
      flash('Tidak bisa beda device login','danger'); return render_template('login.html')
     device_token=expected_token
    else:
     c.execute('UPDATE users SET device_token=? WHERE id=?',(device_token,u['id']))
     c.commit()
   if u['twofa_enabled']:
    session['pending_uid']=u['id']; session['pending_device_token']=device_token
    resp=redirect(url_for('twofa_verify')); resp.set_cookie('om_device_id',device_token,max_age=31536000,httponly=True,samesite='Lax',secure=request.is_secure); return resp
   if u['role']=='superadmin':
    session['uid']=u['id']; session['policy_pending']=True; session['browser_device_token']=device_token; session['last_activity_ts']=time.time()
    c.execute('UPDATE users SET last_login=? WHERE id=?',(now().isoformat(),u['id'])); c.commit(); flash('Master wajib mengaktifkan 2FA sebelum melanjutkan.','danger')
    resp=redirect(url_for('twofa_setup')); resp.set_cookie('om_device_id',device_token,max_age=31536000,httponly=True,samesite='Lax',secure=request.is_secure); return resp
   session['uid']=u['id']; session['policy_pending']=True; session['browser_device_token']=device_token; session['last_activity_ts']=time.time()
   c.execute('UPDATE users SET last_login=? WHERE id=?',(now().isoformat(),u['id'])); c.execute('INSERT INTO login_logs(user_id,staff_id,event,ip_address,user_agent,detail,created_at) VALUES(?,?,?,?,?,?,?)',(u['id'],u['staff_id'],'LOGIN',request.headers.get('X-Forwarded-For',request.remote_addr),request.headers.get('User-Agent','')[:500],'Login berhasil',now().isoformat())); c.commit()
   resp=redirect(url_for('dashboard')); resp.set_cookie('om_device_id',device_token,max_age=31536000,httponly=True,samesite='Lax',secure=request.is_secure); return resp
 return render_template('login.html')
@app.route('/2fa/verify',methods=['GET','POST'])
def twofa_verify():
 uid=session.get('pending_uid')
 if not uid:return redirect(url_for('login'))
 with db_conn() as c:u=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
 if request.method=='POST':
  if totp_verify(u['twofa_secret'],request.form['code'].strip()):
   session.pop('pending_uid',None); session['uid']=uid; session['policy_pending']=True; session['last_activity_ts']=time.time()
   device_token=(session.pop('pending_device_token',None) or request.cookies.get('om_device_id') or '').strip(); session['browser_device_token']=device_token
   with db_conn() as c2: c2.execute('INSERT INTO login_logs(user_id,staff_id,event,ip_address,user_agent,detail,created_at) VALUES(?,?,?,?,?,?,?)',(u['id'],u['staff_id'],'LOGIN',request.headers.get('X-Forwarded-For',request.remote_addr),request.headers.get('User-Agent','')[:500],'Login 2FA berhasil',now().isoformat())); c2.commit()
   resp=redirect(url_for('dashboard'))
   if device_token: resp.set_cookie('om_device_id',device_token,max_age=31536000,httponly=True,samesite='Lax',secure=request.is_secure)
   return resp
  flash('Kode 2FA salah.','danger')
 return render_template('twofa.html')
@app.post('/policy/decision')
@login_required
def policy_decision():
 decision=(request.form.get('decision') or '').strip().lower()
 if decision not in ('setuju','tidak_setuju'): return ('Keputusan tidak valid',400)
 with db_conn() as c:
  version=(g.policy_config or {}).get('version',POLICY_VERSION); existing=c.execute('SELECT id FROM policy_acceptances WHERE user_id=? AND policy_version=?',(g.user['id'],version)).fetchone()
  values=(decision,now().isoformat(),request.headers.get('X-Forwarded-For',request.remote_addr),request.headers.get('User-Agent','')[:500])
  if existing: c.execute('UPDATE policy_acceptances SET decision=?,decided_at=?,ip_address=?,user_agent=? WHERE id=?',values+(existing['id'],))
  else: c.execute('INSERT INTO policy_acceptances(user_id,policy_version,decision,decided_at,ip_address,user_agent) VALUES(?,?,?,?,?,?)',(g.user['id'],version)+values)
  audit(c,'policy.'+decision,version,target_type='policy',target_id=version); c.commit()
 if decision=='tidak_setuju':
  session.clear(); return redirect(url_for('login',policy='declined'))
 session.pop('policy_pending',None)
 return redirect(request.form.get('next') or url_for('dashboard'))

@app.get('/logout')
def logout():
 uid=session.get('uid')
 if uid:
  try:
   with db_conn() as c:
    u=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); c.execute('INSERT INTO login_logs(user_id,staff_id,event,ip_address,user_agent,detail,created_at) VALUES(?,?,?,?,?,?,?)',(uid,u['staff_id'] if u else None,'LOGOUT',request.headers.get('X-Forwarded-For',request.remote_addr),request.headers.get('User-Agent','')[:500],'Logout',now().isoformat())); c.commit()
  except Exception: pass
 session.clear();return redirect(url_for('login'))

@app.get('/')
@login_required
def dashboard():
 if g.user['role']=='staff' and not menu_allowed('my_dashboard'): return ('Akses dashboard dinonaktifkan oleh Master.',403)
 with db_conn() as c:
  today=now().date().isoformat(); office_id=request.args.get('office_id',type=int); shift_id=request.args.get('shift_id',type=int)
  if g.user['role']=='staff': office_id=g.user['office_id']
  offices=c.execute("SELECT * FROM offices WHERE status='Aktif' ORDER BY name COLLATE NOCASE").fetchall(); shifts=c.execute("SELECT id,name,start_time,end_time FROM shifts WHERE office_id IS NULL AND status='Aktif' ORDER BY start_time,name COLLATE NOCASE").fetchall(); params=[]; where=' WHERE 1=1 '
  if office_id:where+=' AND s.office_id=?';params.append(office_id)
  if shift_id:where+=" AND COALESCE((SELECT a0.shift_id FROM assignments a0 WHERE a0.staff_id=s.id AND a0.work_date='CURRENT' AND a0.is_active=1 ORDER BY a0.id DESC LIMIT 1),(SELECT ss0.shift_id FROM shift_schedules ss0 WHERE ss0.staff_id=s.id AND ss0.work_date='CURRENT' ORDER BY ss0.id DESC LIMIT 1))=?";params.append(shift_id)
  staff=c.execute('SELECT s.*,o.name office_name FROM staff s LEFT JOIN offices o ON o.id=s.office_id'+where+' ORDER BY s.name',params).fetchall()
  asql='''SELECT a.*,s.name staff_name,s.cs_name,o.name office_name,sh.name shift_name,ch.name channel_name,ch.category category FROM assignments a JOIN staff s ON s.id=a.staff_id LEFT JOIN offices o ON o.id=a.office_id LEFT JOIN shifts sh ON sh.id=a.shift_id LEFT JOIN channels ch ON ch.id=a.channel_id WHERE a.work_date='CURRENT' '''; ap=[]
  if office_id: asql+=' AND a.office_id=?'; ap.append(office_id)
  if shift_id: asql+=' AND a.shift_id=?'; ap.append(shift_id)
  asql+=' ORDER BY o.name,sh.start_time,ch.category,ch.name'; assignments=c.execute(asql,ap).fetchall()
  lsql="SELECT l.*,s.name,s.cs_name,o.name office_name FROM leaves l JOIN staff s ON s.id=l.staff_id LEFT JOIN offices o ON o.id=s.office_id WHERE l.status='OUT'"; lp=[]
  if office_id: lsql+=' AND s.office_id=?'; lp.append(office_id)
  if shift_id: lsql+=" AND COALESCE((SELECT a0.shift_id FROM assignments a0 WHERE a0.staff_id=s.id AND a0.work_date='CURRENT' AND a0.is_active=1 ORDER BY a0.id DESC LIMIT 1),(SELECT ss0.shift_id FROM shift_schedules ss0 WHERE ss0.staff_id=s.id AND ss0.work_date='CURRENT' ORDER BY ss0.id DESC LIMIT 1))=?"; lp.append(shift_id)
  leaves=c.execute(lsql,lp).fetchall()
  # Pending DP/WD legacy dinonaktifkan untuk Marketing 8008. Dashboard hanya memakai staf, absensi, dan IN/OUT.
  stats={'staff':sum(1 for x in staff if x['status']=='Aktif'),'out':len(leaves),'ex':sum(1 for x in staff if x['status']=='Ex Karyawan')}
 boards={}
 for r in assignments:
  key=f"{r['category'] or 'LAINNYA'}|{r['shift_name'] or '-'}"
  boards.setdefault(key,{'title':f"{(r['category'] or 'LAINNYA').upper()} {str(r['shift_name'] or '').upper()}",'items':[]})['items'].append(r)
 return render_template('dashboard.html',offices=offices,shifts=shifts,office_id=office_id,shift_id=shift_id,staff=staff,assignments=assignments,boards=list(boards.values()),leaves=leaves,stats=stats,today=today)

@app.route('/offices',methods=['GET','POST'])
@roles('superadmin','supervisor')
def offices_page():
 with db_conn() as c:
  if request.method=='POST':
   f=request.form; oid=f.get('id',type=int); vals=(f['name'].strip(),f.get('location','').strip(),f.get('status','Aktif'))
   if oid: c.execute('UPDATE offices SET name=?,location=?,status=? WHERE id=?',vals+(oid,)); audit(c,'office.update',f'id={oid}')
   else: c.execute('INSERT INTO offices(name,location,status) VALUES(?,?,?)',vals); audit(c,'office.create',vals[0])
   c.commit(); flash('Master kantor berhasil disimpan.','success'); return redirect(url_for('offices_page'))
  rows=c.execute('SELECT * FROM offices ORDER BY name').fetchall()
 return render_template('offices.html',rows=rows)

@app.post('/offices/<int:oid>/delete')
@roles('superadmin','supervisor')
def office_delete(oid):
 with db_conn() as c:
  office=c.execute('SELECT * FROM offices WHERE id=?',(oid,)).fetchone()
  if not office:
   flash('Kantor tidak ditemukan.','error'); return redirect(url_for('offices_page'))
  main=c.execute("SELECT * FROM offices WHERE name='Kantor Utama' ORDER BY id LIMIT 1").fetchone()
  if not main:
   c.execute("INSERT INTO offices(name,location,status) VALUES('Kantor Utama','-','Aktif')")
   main=c.execute("SELECT * FROM offices WHERE name='Kantor Utama' ORDER BY id LIMIT 1").fetchone()
  main_id=int(main['id'])
  if int(oid)==main_id:
   flash('Kantor Utama tidak dapat dihapus karena menjadi kantor fallback untuk data staf.','error'); return redirect(url_for('offices_page'))
  try:
   c.execute('BEGIN IMMEDIATE')
   moved_staff=c.execute('SELECT COUNT(*) n FROM staff WHERE office_id=?',(oid,)).fetchone()['n']
   # Staf dan akun yang masih berada di kantor yang dihapus otomatis dipindahkan ke Kantor Utama.
   c.execute('UPDATE staff SET office_id=? WHERE office_id=?',(main_id,oid))
   c.execute('UPDATE users SET office_id=? WHERE office_id=?',(main_id,oid))
   c.execute('UPDATE users SET office_id=? WHERE staff_id IN (SELECT id FROM staff WHERE office_id=?)',(main_id,main_id))
   # Penugasan/history tetap dipertahankan, tetapi referensi kantor diarahkan ke Kantor Utama agar FK tidak putus.
   c.execute('UPDATE assignments SET office_id=? WHERE office_id=?',(main_id,oid))
   c.execute('UPDATE shift_schedules SET office_id=? WHERE office_id=?',(main_id,oid))
   c.execute('UPDATE devices SET office_id=? WHERE office_id=?',(main_id,oid))
   c.execute('UPDATE deposit_forms SET office_id=? WHERE office_id=?',(main_id,oid))
   # Jobdesk bersifat global; lepaskan ikatan kantor lama.
   c.execute('UPDATE channels SET office_id=NULL WHERE office_id=?',(oid,))
   # Shift adalah GLOBAL. Jika masih ada sisa shift legacy per-kantor, gabungkan sekarang
   # agar penghapusan kantor tidak pernah membuat master shift baru per kantor.
   migrate_global_shifts(c)
   c.execute('DELETE FROM offices WHERE id=?',(oid,))
   audit(c,'office.delete',f"id={oid}; name={office['name']}; staff_moved={moved_staff}; fallback=Kantor Utama")
   c.commit()
   flash(f"Kantor {office['name']} berhasil dihapus. {moved_staff} staf dipindahkan otomatis ke Kantor Utama.",'success')
  except Exception:
   c.rollback(); raise
 return redirect(url_for('offices_page'))

@app.get('/offices/<int:oid>')
@roles('superadmin','supervisor','leader')
def office_detail(oid):
 with db_conn() as c:
  office=c.execute('SELECT * FROM offices WHERE id=?',(oid,)).fetchone()
  if not office:return ('Kantor tidak ditemukan',404)
  staff=c.execute('''SELECT s.*,u.username login_id,
    (SELECT COUNT(*) FROM warnings w WHERE w.staff_id=s.id) sp_count,
    (SELECT w.type FROM warnings w WHERE w.staff_id=s.id ORDER BY w.warning_date DESC,w.id DESC LIMIT 1) last_sp
    FROM staff s LEFT JOIN users u ON u.staff_id=s.id WHERE s.office_id=? ORDER BY s.name COLLATE NOCASE ASC''',(oid,)).fetchall()
  today=now().date().isoformat(); jobs=c.execute('''SELECT a.staff_id,ch.name,ch.category,sh.name shift_name,a.start_time,a.end_time FROM assignments a LEFT JOIN channels ch ON ch.id=a.channel_id LEFT JOIN shifts sh ON sh.id=a.shift_id WHERE a.office_id=? AND a.work_date='CURRENT' AND a.is_active=1 ORDER BY a.staff_id,ch.category,ch.name''',(oid,)).fetchall()
  jobmap={}
  for j in jobs:jobmap.setdefault(j['staff_id'],{'jobs':[],'shift':j['shift_name'],'hours':f"{j['start_time']}–{j['end_time']}"})['jobs'].append(j['name'] or '-')
  counts={'total':len(staff),'active':sum(1 for x in staff if x['status']=='Aktif'),'off':sum(1 for x in staff if x['status'] in ('Off','Off Day')),'ex':sum(1 for x in staff if x['status']=='Ex Karyawan')}
 return render_template('office_detail.html',office=office,staff=staff,jobmap=jobmap,counts=counts,today=today)

@app.route('/shifts',methods=['GET','POST'])
@roles('superadmin','supervisor','leader')
def shifts_page():
 return redirect(url_for('operations_page'))

@app.post('/shifts/<int:sid>/delete')
@roles('superadmin','supervisor','leader')
def shift_delete(sid):
 with db_conn() as c:
  used=c.execute('SELECT 1 FROM assignments WHERE shift_id=? LIMIT 1',(sid,)).fetchone() or c.execute('SELECT 1 FROM shift_schedules WHERE shift_id=? LIMIT 1',(sid,)).fetchone()
  if used:
   c.execute("UPDATE shifts SET status='Nonaktif' WHERE id=?",(sid,)); flash('Shift pernah dipakai, jadi dinonaktifkan agar riwayat tetap aman.','success')
  else:
   c.execute('DELETE FROM shifts WHERE id=?',(sid,)); flash('Shift berhasil dihapus.','success')
  audit(c,'shift.delete_or_disable',str(sid)); c.commit()
 return redirect(url_for('shifts_page'))

@app.route('/channels',methods=['GET','POST'])
@roles('superadmin','supervisor','leader')
def channels_page():
 # Master Jobdesk Marketing 8008: NAMA WEBSITE diisi manual, independen dari Master Website DP.
 allowed_categories={'CRM','ADS','TELEMARKETING','LEADER'}
 with db_conn() as c:
  if request.method=='POST':
   f=request.form; cid=f.get('id',type=int); name=f.get('name','').strip(); category=f.get('category','CRM').strip().upper(); status=f.get('status','Aktif')
   if not name:
    flash('Nama Website wajib diisi.','danger'); return redirect(url_for('channels_page'))
   if len(name)>80:
    flash('Nama Website maksimal 80 karakter.','danger'); return redirect(url_for('channels_page'))
   if category not in allowed_categories:
    flash('Kategori tidak valid.','danger'); return redirect(url_for('channels_page'))
   if cid:
    c.execute('UPDATE channels SET office_id=NULL,name=?,category=?,aliases=?,status=? WHERE id=?',(name,category,'',status,cid)); audit(c,'jobdesk.update',f'id={cid} · {name} · {category}')
   else:
    dup=c.execute('SELECT id FROM channels WHERE lower(name)=lower(?) AND lower(category)=lower(?) AND status!=? LIMIT 1',(name,category,'Dihapus')).fetchone()
    if dup: flash('Website dan kategori tersebut sudah ada. Gunakan Edit.','danger'); return redirect(url_for('channels_page'))
    c.execute('INSERT INTO channels(office_id,name,category,aliases,status) VALUES(NULL,?,?,?,?)',(name,category,'',status)); audit(c,'jobdesk.create',f'{name} · {category}')
   c.commit(); flash('Master Jobdesk berhasil disimpan.','success'); return redirect(url_for('channels_page'))
  rows=c.execute("SELECT ch.*,o.name office_name FROM channels ch LEFT JOIN offices o ON o.id=ch.office_id WHERE upper(ch.category) IN ('CRM','ADS','TELEMARKETING','LEADER') ORDER BY ch.name COLLATE NOCASE,ch.category").fetchall()
 return render_template('channels.html',rows=rows)

@app.post('/channels/<int:cid>/delete')
@roles('superadmin','supervisor','leader')
def channel_delete(cid):
 with db_conn() as c:
  used=c.execute('SELECT 1 FROM assignments WHERE channel_id=? LIMIT 1',(cid,)).fetchone()
  if used: c.execute("UPDATE channels SET status='Nonaktif' WHERE id=?",(cid,)); flash('Channel sudah memiliki riwayat, jadi dinonaktifkan.','success')
  else: c.execute('DELETE FROM channels WHERE id=?',(cid,)); flash('Channel berhasil dihapus.','success')
  c.commit()
 return redirect(url_for('channels_page'))

@app.route('/staff',methods=['GET','POST'])
@roles('superadmin','supervisor','leader')
def staff_page():
 with db_conn() as c:
  if request.method=='POST':
   f=request.form; sid=f.get('id',type=int)
   old_staff=c.execute('SELECT * FROM staff WHERE id=?',(sid,)).fetchone() if sid else None
   if sid and not old_staff: flash('Data staf tidak ditemukan.','danger'); return redirect(url_for('staff_page'))
   try:
    join_date=panel_date_optional(f.get('join_date'),'Tanggal Masuk',old_staff['join_date'] if old_staff else None)
   except ValueError as e:
    flash(str(e),'danger'); return redirect(request.referrer or url_for('staff_page'))
   vals=(f['name'].strip(),f.get('gender','Pria'),None,f.get('telegram_username','').strip(),f.get('email','').strip(),'',f.get('cs_name','').strip(),f.get('office_id',type=int),f.get('position','CRM'),f.get('status','Aktif'),join_date,f.get('notes','').strip())
   if sid:
    c.execute('UPDATE staff SET name=?,gender=?,telegram_id=?,telegram_username=?,email=?,agent_code=?,cs_name=?,office_id=?,position=?,status=?,join_date=?,notes=? WHERE id=?',vals+(sid,))
   else:
    sid=c.execute('INSERT INTO staff(name,gender,telegram_id,telegram_username,email,agent_code,cs_name,office_id,position,status,join_date,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',vals).lastrowid
   if f.get('status','Aktif')=='Aktif':
    c.execute('UPDATE staff SET exit_date=NULL,exit_reason=NULL WHERE id=?',(sid,))
   elif f.get('status')=='Ex Karyawan':
    c.execute("UPDATE staff SET exit_date=COALESCE(exit_date,?),exit_reason=COALESCE(NULLIF(exit_reason,''),'Diubah dari Edit Data Staf') WHERE id=?",(now().date().isoformat(),sid))
   if f.get('login_id'):
    account_active=0 if f.get('status','Aktif')=='Ex Karyawan' else 1
    login_id=f['login_id'].strip(); existing_user=c.execute('SELECT id FROM users WHERE staff_id=?',(sid,)).fetchone(); username_owner=c.execute('SELECT id,staff_id FROM users WHERE username=?',(login_id,)).fetchone()
    if username_owner and (not existing_user or username_owner['id']!=existing_user['id']):
     c.rollback(); flash('ID login sudah dipakai akun lain.','danger'); return redirect(url_for('staff_page'))
    if existing_user:
     if f.get('password'): c.execute('UPDATE users SET username=?,password_hash=?,office_id=?,is_active=?,must_change_password=1 WHERE id=?',(login_id,generate_password_hash(f['password']),f.get('office_id',type=int),account_active,existing_user['id']))
     else: c.execute('UPDATE users SET username=?,office_id=?,is_active=? WHERE id=?',(login_id,f.get('office_id',type=int),account_active,existing_user['id']))
    else:
     pw=f.get('password') or secrets.token_urlsafe(6); c.execute('INSERT INTO users(username,password_hash,role,staff_id,office_id,is_active,must_change_password,allowed_menus) VALUES(?,?,?,?,?,?,1,?)',(login_id,generate_password_hash(pw),'staff',sid,f.get('office_id',type=int),account_active,json.dumps(['my_dashboard','attendance','inout','nawala','account'])))
   new_staff=c.execute('SELECT * FROM staff WHERE id=?',(sid,)).fetchone(); audit(c,'staff.update' if old_staff else 'staff.create',f"Staf: {f['name'].strip()} · Kantor ID: {f.get('office_id',type=int) or '-'} · Jabatan: {f.get('position','CRM')}",before=dict(old_staff) if old_staff else None,after=dict(new_staff) if new_staff else None,target_type='staff',target_id=sid); c.commit(); flash('Data staf tersimpan.','success'); return redirect(url_for('staff_page'))
  report_date=parse_panel_date(request.args.get('date'), now().date().isoformat())
  rows=c.execute('''WITH current_assignment AS (
      SELECT staff_id, MIN(shift_id) AS shift_id FROM assignments
      WHERE work_date='CURRENT' AND is_active=1 GROUP BY staff_id
    )
    SELECT s.*,o.name office_name,u.username login_id,u.is_active account_active,
      COALESCE(ca.shift_id,ss.shift_id) shift_id,sh.name shift_name,sh.start_time shift_start,sh.end_time shift_end
    FROM staff s LEFT JOIN offices o ON o.id=s.office_id LEFT JOIN users u ON u.staff_id=s.id
    LEFT JOIN current_assignment ca ON ca.staff_id=s.id
    LEFT JOIN shift_schedules ss ON ss.staff_id=s.id AND ss.work_date='CURRENT'
    LEFT JOIN shifts sh ON sh.id=COALESCE(ca.shift_id,ss.shift_id)
    WHERE s.status NOT IN ('Ex Karyawan','Resign')
    ORDER BY s.name COLLATE NOCASE ASC''').fetchall()
  offices=c.execute('SELECT * FROM offices ORDER BY name').fetchall()
  filter_shifts=c.execute("SELECT id,name,start_time,end_time FROM shifts WHERE office_id IS NULL AND status='Aktif' ORDER BY start_time,name").fetchall()
  active_rows=[r for r in rows if r['status']=='Aktif']
  office_counts=[]
  for o in offices:
   members=[r for r in active_rows if r['office_id']==o['id']]
   all_members=[r for r in rows if r['office_id']==o['id']]
   office_counts.append({'id':o['id'],'name':o['name'],'location':o['location'],'count':len(members),'all_count':len(all_members)})
  # Jumlah staf per shift dihitung dari sumber shift yang sama dengan tabel /staff, agar angka dan filter selalu sinkron.
  shift_map={}
  for r in active_rows:
   if not r['shift_name']: continue
   key=(r['shift_name'],r['shift_start'] or '',r['shift_end'] or '')
   if key not in shift_map: shift_map[key]={'id':r['shift_id'],'shift_name':r['shift_name'],'name':r['shift_name'],'start_time':r['shift_start'],'end_time':r['shift_end'],'total':0}
   shift_map[key]['total']+=1
  shift_counts=sorted(shift_map.values(),key=lambda x:(x['start_time'] or '',x['shift_name']))
  ex_count=c.execute("SELECT COUNT(*) n FROM staff WHERE status IN ('Ex Karyawan','Resign')").fetchone()['n']; totals={'all':len(rows)+ex_count,'active':len(active_rows),'ex':ex_count,'scheduled':sum(int(x['total']) for x in shift_counts)}
 return render_template('staff.html',rows=rows,offices=offices,office_counts=office_counts,shift_counts=shift_counts,filter_shifts=filter_shifts,totals=totals,report_date=report_date)

@app.post('/staff/<int:sid>/delete')
@roles('superadmin','supervisor')
def staff_delete(sid):
 with db_conn() as c:
  st=c.execute('SELECT * FROM staff WHERE id=?',(sid,)).fetchone()
  if not st: flash('Staf tidak ditemukan.','danger'); return redirect(url_for('staff_page'))
  used=any(c.execute(q,(sid,)).fetchone() for q in [
   'SELECT 1 FROM assignments WHERE staff_id=? LIMIT 1','SELECT 1 FROM shift_schedules WHERE staff_id=? LIMIT 1','SELECT 1 FROM offdays WHERE staff_id=? LIMIT 1','SELECT 1 FROM leaves WHERE staff_id=? LIMIT 1','SELECT 1 FROM warnings WHERE staff_id=? LIMIT 1','SELECT 1 FROM deposit_alert_staff WHERE staff_id=? LIMIT 1','SELECT 1 FROM deposit_forms WHERE staff_id=? LIMIT 1','SELECT 1 FROM memos WHERE staff_id=? LIMIT 1'])
  if used:
   c.execute("UPDATE staff SET status='Ex Karyawan',exit_date=COALESCE(exit_date,?) WHERE id=?",(now().date().isoformat(),sid)); c.execute('UPDATE users SET is_active=0 WHERE staff_id=?',(sid,)); flash('Staf memiliki riwayat. Data dipindahkan ke Ex Karyawan agar histori tetap aman.','success')
  else:
   c.execute('DELETE FROM users WHERE staff_id=?',(sid,)); c.execute('DELETE FROM staff WHERE id=?',(sid,)); flash('Data staf berhasil dihapus.','success')
  audit(c,'staff.delete_or_archive',f"Staf: {st['name']} · Status akhir: {c.execute('SELECT status FROM staff WHERE id=?',(sid,)).fetchone()['status'] if c.execute('SELECT 1 FROM staff WHERE id=?',(sid,)).fetchone() else 'Dihapus permanen'}"); c.commit()
 return redirect(url_for('staff_page'))

@app.get('/staff/<int:sid>/detail')
@roles('superadmin','supervisor','leader')
def staff_detail(sid):
 with db_conn() as c:
  st=c.execute('SELECT s.*,o.name office_name,o.location,u.username login_id,u.is_active account_active FROM staff s LEFT JOIN offices o ON o.id=s.office_id LEFT JOIN users u ON u.staff_id=s.id WHERE s.id=?',(sid,)).fetchone()
  if not st:return ('Staf tidak ditemukan',404)
  latest_shift=c.execute('''SELECT sh.name,sh.start_time,sh.end_time,ss.work_date FROM shift_schedules ss JOIN shifts sh ON sh.id=ss.shift_id WHERE ss.staff_id=? ORDER BY CASE WHEN ss.work_date='CURRENT' THEN 0 ELSE 1 END,ss.id DESC LIMIT 1''',(sid,)).fetchone()
  today=now().date().isoformat(); jobs=c.execute('''SELECT ch.name,ch.category,sh.name shift_name,a.start_time,a.end_time FROM assignments a LEFT JOIN channels ch ON ch.id=a.channel_id LEFT JOIN shifts sh ON sh.id=a.shift_id WHERE a.staff_id=? AND a.work_date='CURRENT' AND a.is_active=1 ORDER BY ch.category,ch.name''',(sid,)).fetchall()
  sp=c.execute('SELECT * FROM warnings WHERE staff_id=? ORDER BY warning_date DESC,id DESC',(sid,)).fetchall(); offices=c.execute("SELECT * FROM offices WHERE status='Aktif' ORDER BY name").fetchall(); cutoff=now()-timedelta(days=RETENTION_DAYS); pending_count=c.execute('''SELECT COUNT(DISTINCT f.id) n FROM deposit_alert_staff das JOIN deposit_forms f ON f.id=das.deposit_form_id WHERE das.staff_id=? AND f.alert_sent=1 AND f.first_seen>=?''',(sid,int(cutoff.timestamp()))).fetchone()['n']; leaves_count=c.execute('SELECT COUNT(*) n FROM leaves WHERE staff_id=? AND out_at>=?',(sid,cutoff.isoformat())).fetchone()['n']; mistake_rows=c.execute('SELECT entry_type,amount FROM mistake_ledger WHERE staff_id=?',(sid,)).fetchall(); mistake_total=sum(x['amount'] for x in mistake_rows if x['entry_type']=='MISTAKE'); mistake_cut=sum(x['amount'] for x in mistake_rows if x['entry_type']=='POTONGAN')
  job_history=c.execute('''SELECT h.*,u.username changed_by_name FROM jobdesk_history h LEFT JOIN users u ON u.id=h.changed_by WHERE h.staff_id=? ORDER BY h.started_at DESC,h.id DESC''',(sid,)).fetchall()
  staff_audit=c.execute("""SELECT a.*,u.username FROM audit_logs a LEFT JOIN users u ON u.id=a.user_id WHERE (a.target_type='staff' AND a.target_id=?) OR a.detail LIKE ? ORDER BY a.id DESC LIMIT 100""",(str(sid),f'%{st["name"]}%')).fetchall()
 return render_template('staff_detail.html',st=st,latest_shift=latest_shift,jobs=jobs,sp=sp,today=today,offices=offices,pending_count=pending_count,leaves_count=leaves_count,mistake_total=mistake_total,mistake_remaining=max(0,mistake_total-mistake_cut),job_history=job_history,staff_audit=staff_audit)

@app.post('/staff/<int:sid>/reset-password')
@roles('superadmin','supervisor','leader')
def reset_password(sid):
 pw=request.form.get('password') or secrets.token_urlsafe(7)
 with db_conn() as c:c.execute('UPDATE users SET password_hash=?,must_change_password=1 WHERE staff_id=?',(generate_password_hash(pw),sid));c.commit()
 flash('Password direset. Password baru: '+pw,'success');return redirect(url_for('staff_page'))

@app.route('/offdays',methods=['GET','POST'])
@roles('superadmin','supervisor','leader')
def offdays_page():
 flash('Menu Off Day sudah dihapus. Pengaturan staf harian sekarang dipusatkan di Penugasan Kerja.','success')
 return redirect(url_for('operations_page'))

@app.post('/offdays/<int:oid>/delete')
@roles('superadmin','supervisor','leader')
def offday_delete(oid):
 return redirect(url_for('operations_page'))

@app.get('/audit')
@roles('superadmin','supervisor','leader')
def audit_page():
 with db_conn() as c:
  rows=c.execute('''SELECT a.*,u.username FROM audit_logs a LEFT JOIN users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT 500''').fetchall()
 return render_template('audit.html',rows=rows)

@app.route('/users',methods=['GET','POST'])
@roles('superadmin','supervisor','leader')
def users_page():
 with db_conn() as c:
  if request.method=='POST':
   f=request.form; staff_id=f.get('staff_id',type=int); st=c.execute('SELECT * FROM staff WHERE id=?',(staff_id,)).fetchone()
   if not st:flash('Staf tidak ditemukan.','danger');return redirect(url_for('users_page'))
   role=f.get('role','staff')
   if g.user['role']=='leader' and role not in ('staff','leader'): role='staff'
   menus=f.getlist('menus'); username=f['username'].strip(); existing=c.execute('SELECT * FROM users WHERE staff_id=?',(staff_id,)).fetchone(); owner=c.execute('SELECT * FROM users WHERE username=?',(username,)).fetchone()
   if owner and (not existing or owner['id']!=existing['id']):flash('Username sudah dipakai akun lain.','danger');return redirect(url_for('users_page'))
   active=1 if f.get('is_active') else 0
   before_access=dict(existing) if existing else None
   if existing:
    if f.get('password'):c.execute('UPDATE users SET username=?,password_hash=?,role=?,office_id=?,is_active=?,allowed_menus=?,must_change_password=1 WHERE id=?',(username,generate_password_hash(f['password']),role,st['office_id'],active,json.dumps(menus),existing['id']))
    else:c.execute('UPDATE users SET username=?,role=?,office_id=?,is_active=?,allowed_menus=? WHERE id=?',(username,role,st['office_id'],active,json.dumps(menus),existing['id']))
   else:
    pw=f.get('password') or secrets.token_urlsafe(7);c.execute('INSERT INTO users(username,password_hash,role,staff_id,office_id,is_active,must_change_password,allowed_menus) VALUES(?,?,?,?,?,?,1,?)',(username,generate_password_hash(pw),role,staff_id,st['office_id'],active,json.dumps(menus)))
   new_access=c.execute('SELECT * FROM users WHERE staff_id=?',(staff_id,)).fetchone();audit(c,'user.access.save',f'staff={staff_id} role={role} menus={menus}',before=before_access,after=dict(new_access) if new_access else None,target_type='staff',target_id=staff_id);c.commit();flash('Akses akun berhasil disimpan.','success');return redirect(url_for('users_page'))
  rows=c.execute('SELECT u.*,s.name staff_name,o.name office_name FROM users u LEFT JOIN staff s ON s.id=u.staff_id LEFT JOIN offices o ON o.id=u.office_id WHERE u.staff_id IS NOT NULL ORDER BY s.name').fetchall();staff=c.execute("SELECT * FROM staff WHERE status='Aktif' ORDER BY name").fetchall()
 return render_template('users.html',rows=rows,staff=staff)

@app.post('/users/reset-device/<int:uid>')
@roles('superadmin','supervisor','leader')
def user_reset_device(uid):
 with db_conn() as c:c.execute('UPDATE users SET device_token=NULL WHERE id=?',(uid,));audit(c,'user.device.reset',f'id={uid}');c.commit()
 flash('Ikatan PC berhasil direset.','success');return redirect(url_for('users_page'))

@app.route('/account',methods=['GET','POST'])
@login_required
def account_page():
 if request.method=='POST':
  with db_conn() as c:
   u=c.execute('SELECT * FROM users WHERE id=?',(g.user['id'],)).fetchone()
   if not check_password_hash(u['password_hash'],request.form['old_password']):flash('Password lama salah.','danger')
   elif len(request.form['new_password'])<8:flash('Password baru minimal 8 karakter.','danger')
   else:c.execute('UPDATE users SET password_hash=?,must_change_password=0 WHERE id=?',(generate_password_hash(request.form['new_password']),u['id']));c.commit();flash('Password berhasil diganti.','success')
 return render_template('account.html')

@app.route('/2fa/setup',methods=['GET','POST'])
@roles('superadmin','supervisor','leader')
def twofa_setup():
 with db_conn() as c:
  u=c.execute('SELECT * FROM users WHERE id=?',(g.user['id'],)).fetchone(); secret=u['twofa_secret'] or base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')
  if request.method=='POST':
   if totp_verify(secret,request.form['code'].strip()):c.execute('UPDATE users SET twofa_secret=?,twofa_enabled=1 WHERE id=?',(secret,u['id']));c.commit();flash('2FA aktif.','success');return redirect(url_for('dashboard'))
   flash('Kode salah.','danger')
  uri=f"otpauth://totp/OMTOGEL%20Staff:{u['username']}?secret={secret}&issuer=OMTOGEL%20Staff"
 return render_template('twofa_setup.html',secret=secret,uri=uri)

@app.route('/operations',methods=['GET','POST'])
@roles('superadmin','supervisor','leader')
def operations_page():
 """Penugasan Kerja GLOBAL.

 Tidak ada filter kantor untuk menambah/mengedit penugasan. Kantor selalu mengikuti
 profil staf. Shift dan Master Jobdesk juga global. Board menampilkan seluruh staf
 aktif yang mempunyai penugasan CURRENT dari semua kantor.
 """
 with db_conn() as c:
  work_date='CURRENT'
  if request.method=='POST':
   f=request.form; action=(f.get('action') or '').strip()
   try:
    if action=='shift_save':
     sid=f.get('shift_id',type=int)
     vals=((f.get('name') or '').strip(),(f.get('code') or '').strip(),f.get('start_time') or '',f.get('end_time') or '',f.get('status') or 'Aktif')
     if not vals[0] or not vals[2] or not vals[3]: raise ValueError('Nama shift, jam mulai, dan jam selesai wajib diisi.')
     dup=c.execute("SELECT id FROM shifts WHERE office_id IS NULL AND lower(name)=lower(?) AND id!=COALESCE(?,0) LIMIT 1",(vals[0],sid)).fetchone()
     if dup: raise ValueError('Nama shift global sudah digunakan. Gunakan Edit pada shift tersebut.')
     if sid:
      before=c.execute('SELECT * FROM shifts WHERE id=?',(sid,)).fetchone()
      if not before: raise ValueError('Shift tidak ditemukan.')
      c.execute('UPDATE shifts SET office_id=NULL,name=?,code=?,start_time=?,end_time=?,status=? WHERE id=?',vals+(sid,))
      audit(c,'operations.shift.update',f"Edit Shift Global: {before['name']} → {vals[0]} · Jam {before['start_time']}–{before['end_time']} → {vals[2]}–{vals[3]} · Status {before['status']} → {vals[4]}")
     else:
      c.execute('INSERT INTO shifts(office_id,name,code,start_time,end_time,status) VALUES(NULL,?,?,?,?,?)',vals)
      audit(c,'operations.shift.create',f"Tambah Shift Global: {vals[0]} · {vals[2]}–{vals[3]}")
     c.commit(); flash('Shift global berhasil disimpan.','success')

    elif action=='shift_delete':
     sid=f.get('shift_id',type=int)
     if not sid: raise ValueError('Shift tidak valid.')
     sh=c.execute('SELECT * FROM shifts WHERE id=?',(sid,)).fetchone()
     if not sh: raise ValueError('Shift tidak ditemukan.')
     used=c.execute('SELECT 1 FROM assignments WHERE shift_id=? LIMIT 1',(sid,)).fetchone() or c.execute('SELECT 1 FROM shift_schedules WHERE shift_id=? LIMIT 1',(sid,)).fetchone()
     if used:
      c.execute("UPDATE shifts SET status='Nonaktif' WHERE id=?",(sid,))
      audit(c,'operations.shift.disable',f"Nonaktifkan Shift Global: {sh['name']}")
      flash('Shift sudah pernah dipakai, jadi dinonaktifkan agar riwayat tetap aman.','success')
     else:
      c.execute('DELETE FROM shifts WHERE id=?',(sid,))
      audit(c,'operations.shift.delete',f"Hapus Shift Global: {sh['name']}")
      flash('Shift berhasil dihapus.','success')
     c.commit()

    elif action=='assignment_delete':
     staff_id=f.get('staff_id',type=int)
     if not staff_id: raise ValueError('Staf tidak valid.')
     st=c.execute('SELECT name,cs_name FROM staff WHERE id=?',(staff_id,)).fetchone()
     jobs=c.execute("""SELECT a.channel_id,COALESCE(ch.name,a.target,'-') name,ch.category FROM assignments a
                       LEFT JOIN channels ch ON ch.id=a.channel_id
                       WHERE a.work_date=? AND a.staff_id=? AND a.is_active=1 ORDER BY a.id""",(work_date,staff_id)).fetchall()
     before_jobs=[x['name'] for x in jobs]
     ended=now().isoformat()
     for j in jobs:
      c.execute('UPDATE jobdesk_history SET ended_at=? WHERE staff_id=? AND channel_id=? AND ended_at IS NULL',(ended,staff_id,j['channel_id']))
     c.execute('DELETE FROM assignments WHERE work_date=? AND staff_id=?',(work_date,staff_id))
     c.execute('DELETE FROM shift_schedules WHERE work_date=? AND staff_id=?',(work_date,staff_id))
     audit(c,'operations.assignment.delete',f"Hapus Penugasan: {st['name'] if st else staff_id} · Jobdesk: {', '.join(before_jobs) or '-'}",before={'jobdesk':before_jobs},after={'jobdesk':[]},target_type='staff',target_id=staff_id)
     c.commit(); flash('Penugasan staf berhasil dihapus.','success')

    elif action=='assignment_save':
     staff_id=f.get('staff_id',type=int); shift_id=f.get('shift_id',type=int)
     channel_ids=[]
     # Tambah Penugasan memakai satu pilihan Jobdesk dari Master Jobdesk Global.
     # Tetap menerima getlist agar kompatibel dengan data lama, tetapi hanya ID unik valid.
     for raw in f.getlist('channel_ids'):
      try:
       cid=int(raw)
       if cid not in channel_ids: channel_ids.append(cid)
      except (TypeError,ValueError): pass
     if not staff_id: raise ValueError('Pilih staf terlebih dahulu.')
     if not shift_id: raise ValueError('Pilih shift.')
     if not channel_ids: raise ValueError('Pilih minimal 1 jobdesk.')
     st=c.execute("SELECT * FROM staff WHERE id=? AND status='Aktif'",(staff_id,)).fetchone()
     sh=c.execute("SELECT * FROM shifts WHERE id=? AND office_id IS NULL AND status='Aktif'",(shift_id,)).fetchone()
     if not st: raise ValueError('Staf tidak ditemukan atau tidak aktif.')
     if not sh: raise ValueError('Shift global tidak valid atau tidak aktif.')
     office_id=st['office_id']
     if not office_id: raise ValueError('Kantor staf belum diatur di Data Staf.')
     valid=[]
     for cid in channel_ids:
      ch=c.execute("SELECT * FROM channels WHERE id=? AND status='Aktif'",(cid,)).fetchone()
      if ch: valid.append(ch)
     if len(valid)!=len(channel_ids): raise ValueError('Ada jobdesk yang sudah nonaktif atau tidak ditemukan.')


     old=c.execute('''SELECT sh.name shift_name,GROUP_CONCAT(COALESCE(ch.name,a.target), ', ') jobs
                      FROM assignments a LEFT JOIN shifts sh ON sh.id=a.shift_id LEFT JOIN channels ch ON ch.id=a.channel_id
                      WHERE a.work_date=? AND a.staff_id=? AND a.is_active=1 GROUP BY a.staff_id''',(work_date,staff_id)).fetchone()
     old_rows=c.execute('''SELECT a.channel_id,COALESCE(ch.name,a.target,'-') name,ch.category FROM assignments a LEFT JOIN channels ch ON ch.id=a.channel_id WHERE a.work_date=? AND a.staff_id=? AND a.is_active=1''',(work_date,staff_id)).fetchall()
     old_ids={int(x['channel_id']) for x in old_rows if x['channel_id'] is not None}
     new_ids={int(x['id']) for x in valid}
     changed_at=now().isoformat(); changer=g.user['id'] if g.user else None
     for x in old_rows:
      if x['channel_id'] is not None and int(x['channel_id']) not in new_ids:
       c.execute('UPDATE jobdesk_history SET ended_at=? WHERE staff_id=? AND channel_id=? AND ended_at IS NULL',(changed_at,staff_id,int(x['channel_id'])))
     for ch in valid:
      if int(ch['id']) not in old_ids:
       c.execute('INSERT INTO jobdesk_history(staff_id,channel_id,jobdesk_name,category,event,started_at,changed_by) VALUES(?,?,?,?,?,?,?)',(staff_id,ch['id'],ch['name'],ch['category'],'ASSIGNED',changed_at,changer))
     sched=c.execute('SELECT id FROM shift_schedules WHERE work_date=? AND staff_id=?',(work_date,staff_id)).fetchone()
     if sched: c.execute('UPDATE shift_schedules SET shift_id=?,office_id=? WHERE id=?',(shift_id,office_id,sched['id']))
     else: c.execute('INSERT INTO shift_schedules(work_date,staff_id,shift_id,office_id) VALUES(?,?,?,?)',(work_date,staff_id,shift_id,office_id))
     c.execute('DELETE FROM assignments WHERE work_date=? AND staff_id=?',(work_date,staff_id))
     batch_id=uuid.uuid4().hex
     # Jam penugasan selalu mengikuti Shift Global; tidak boleh dioverride dari form.
     start_time=sh['start_time']; end_time=sh['end_time']
     for ch in valid:
      c.execute('''INSERT INTO assignments(assignment_batch_id,work_date,office_id,shift_id,staff_id,channel_id,category,target,start_time,end_time,is_active)
                   VALUES(?,?,?,?,?,?,?,?,?,?,1)''',(batch_id,work_date,office_id,shift_id,staff_id,ch['id'],ch['category'],ch['name'],start_time,end_time))
     new_jobs=', '.join(x['name'] for x in valid)
     before_payload={'shift':old['shift_name'] if old else None,'jobdesk':[x['name'] for x in old_rows]}
     after_payload={'shift':sh['name'],'jobdesk':[x['name'] for x in valid]}
     if old:
      audit(c,'operations.assignment.update',f"Edit Penugasan: {st['name']} · Shift {old['shift_name'] or '-'} → {sh['name']} · Jobdesk {old['jobs'] or '-'} → {new_jobs}",before=before_payload,after=after_payload,target_type='staff',target_id=staff_id)
     else:
      audit(c,'operations.assignment.create',f"Tambah Penugasan: {st['name']} · Shift {sh['name']} · Jobdesk {new_jobs}",before=before_payload,after=after_payload,target_type='staff',target_id=staff_id)
     c.commit(); flash('Penugasan staf berhasil disimpan.','success')

    else:
     raise ValueError('Aksi tidak dikenal.')
   except sqlite3.IntegrityError:
    c.rollback(); flash('Data bentrok dengan data yang sudah ada. Periksa kembali shift dan penugasan staf.','danger')
   except ValueError as e:
    c.rollback(); flash(str(e),'danger')
   return redirect(url_for('operations_page'))

  c.execute('''UPDATE assignments SET office_id=(SELECT s.office_id FROM staff s WHERE s.id=assignments.staff_id)
               WHERE work_date='CURRENT' AND staff_id IS NOT NULL
                 AND COALESCE(office_id,0) != COALESCE((SELECT s.office_id FROM staff s WHERE s.id=assignments.staff_id),0)''')
  c.execute('''UPDATE shift_schedules SET office_id=(SELECT s.office_id FROM staff s WHERE s.id=shift_schedules.staff_id)
               WHERE work_date='CURRENT' AND staff_id IS NOT NULL
                 AND COALESCE(office_id,0) != COALESCE((SELECT s.office_id FROM staff s WHERE s.id=shift_schedules.staff_id),0)''')
  c.commit()

  shifts=c.execute("SELECT * FROM shifts WHERE office_id IS NULL ORDER BY CASE WHEN status='Aktif' THEN 0 ELSE 1 END,start_time,name").fetchall()
  staff=c.execute("""SELECT s.*,o.name office_name FROM staff s LEFT JOIN offices o ON o.id=s.office_id
                     WHERE s.status='Aktif' ORDER BY s.name""").fetchall()
  channels=c.execute("""SELECT * FROM channels WHERE status='Aktif'
                        ORDER BY CASE category WHEN 'Deposit' THEN 1 WHEN 'Withdraw' THEN 2 WHEN 'Livechat' THEN 3 WHEN 'Pulsa' THEN 4 WHEN 'QRIS' THEN 5 ELSE 6 END,name""").fetchall()
  raw=c.execute('''SELECT a.*,s.name staff_name,s.cs_name,s.email,s.agent_code,s.office_id staff_office_id,
                          o.name office_name,sh.name shift_name,sh.code shift_code,sh.start_time shift_start,sh.end_time shift_end,
                          ch.name channel_name,ch.category channel_category
                   FROM assignments a JOIN staff s ON s.id=a.staff_id
                   LEFT JOIN offices o ON o.id=s.office_id
                   JOIN shifts sh ON sh.id=a.shift_id LEFT JOIN channels ch ON ch.id=a.channel_id
                   WHERE a.work_date=? AND a.is_active=1 AND s.status='Aktif'
                   ORDER BY sh.start_time,s.name,ch.category,ch.name''',(work_date,)).fetchall()
  groups={}
  for r in raw:
   gd=groups.setdefault(r['staff_id'],{
    'staff_id':r['staff_id'],'staff_name':r['staff_name'],'cs_name':r['cs_name'],'email':r['email'],'agent_code':r['agent_code'],
    'office_id':r['staff_office_id'],'office_name':r['office_name'],'shift_id':r['shift_id'],'shift_name':r['shift_name'],
    'shift_code':r['shift_code'],'start_time':r['start_time'],'end_time':r['end_time'],'channel_ids':[],'channels':[]})
   if r['channel_id'] not in gd['channel_ids']: gd['channel_ids'].append(r['channel_id'])
   gd['channels'].append({'id':r['channel_id'],'name':r['channel_name'] or r['target'] or 'Belum ada jobdesk','category':r['channel_category'] or r['category'] or 'Lainnya'})

  by_shift={}
  for sh in shifts:
   if sh['status']=='Aktif': by_shift[sh['id']]={'shift':sh,'staff':[]}
  for gd in groups.values():
   sh=c.execute('SELECT * FROM shifts WHERE id=?',(gd['shift_id'],)).fetchone()
   if sh: by_shift.setdefault(gd['shift_id'],{'shift':sh,'staff':[]})['staff'].append(gd)
  assigned_ids=set(groups.keys())
  unassigned=[x for x in staff if x['id'] not in assigned_ids]

  assigned_rows=list(groups.values())

  edit_staff=request.args.get('edit_staff',type=int)
  edit_row=groups.get(edit_staff)
  totals={'active_staff':len(staff),'assigned':len(groups),'unassigned':len(unassigned),'active_shifts':sum(1 for x in shifts if x['status']=='Aktif')}
 return render_template('operations.html',shifts=shifts,staff=staff,channels=channels,by_shift=list(by_shift.values()),assigned_rows=assigned_rows,unassigned=unassigned,edit_row=edit_row,totals=totals)

@app.route('/assignments',methods=['GET','POST'])
@roles('superadmin','supervisor','leader')
def assignments_page():
 return redirect(url_for('operations_page'))

@app.route('/inout',methods=['GET','POST'])
@login_required
def inout_page():
 if g.user['role']=='staff' and not menu_allowed('inout'): return ('Akses IN/OUT dinonaktifkan oleh Master.',403)
 if not g.user['staff_id']: return ('Akun ini tidak terhubung ke Data Staf.',400)
 sid=g.user['staff_id']
 with db_conn() as c:
  auto_events=process_auto_in_overdue(c); c.commit(); flush_auto_in_notifications(auto_events)
  staff=c.execute('SELECT s.*,o.name office_name FROM staff s LEFT JOIN offices o ON o.id=s.office_id WHERE s.id=?',(sid,)).fetchone()
  ass_rows=staff_active_assignments(c,sid,now()); ass=ass_rows[0] if ass_rows else None; jobdesk_text=', '.join((r['channel_name'] or r['target'] or '-') for r in ass_rows) or '-'
  active=c.execute("SELECT * FROM leaves WHERE staff_id=? AND status='OUT' ORDER BY id DESC LIMIT 1",(sid,)).fetchone()
  if request.method=='POST':
   action=request.form.get('action','')
   if action=='out':
    reason=request.form.get('reason','')
    if reason not in DURATIONS:
     flash('Jenis izin tidak valid.','danger'); return redirect(url_for('inout_page'))
    # Transaksi eksklusif pendek agar batas 5 orang tidak bisa terlewati walau beberapa staf klik bersamaan.
    with lock:
     try:
      c.execute('BEGIN IMMEDIATE')
      active_now=c.execute("SELECT * FROM leaves WHERE staff_id=? AND status='OUT' ORDER BY id DESC LIMIT 1",(sid,)).fetchone()
      active_count=c.execute("SELECT COUNT(*) n FROM leaves WHERE status='OUT'").fetchone()['n']
      if active_now:
       c.rollback(); flash('Kamu masih dalam status izin keluar. Tekan IN KEMBALI setelah kembali.','danger')
      elif active_count>=MAX_ACTIVE_LEAVES:
       c.rollback(); flash('Silakan dicoba beberapa saat lagi karena OUT sudah mencapai jumlah maksimal 5 orang, atau hubungi Leader.','danger')
      else:
       out=now(); exp=out+timedelta(minutes=DURATIONS[reason]); snap=json.dumps({'jobdesk':jobdesk_text,'cs':staff['cs_name'],'office':staff['office_name']})
       device_token=(request.cookies.get('om_device_id') or session.get('browser_device_token') or g.user['device_token'] or '').strip()
       if not device_token: device_token=secrets.token_urlsafe(24)
       session['browser_device_token']=device_token
       session['last_activity_ts']=time.time()
       c.execute('UPDATE users SET device_token=? WHERE id=?',(device_token,g.user['id']))
       c.execute('INSERT INTO leaves(staff_id,reason,out_at,expected_at,status,source,assignment_snapshot,device_token) VALUES(?,?,?,?,?,?,?,?)',(sid,reason,out.isoformat(),exp.isoformat(),'OUT','dashboard',snap,device_token)); c.commit()
       tg_send_inout(f"🚪 <b>IZIN KELUAR</b>\n👤 {staff['name']} — {staff['cs_name'] or '-'}\n💼 {jobdesk_text}\n📝 {reason.title()}\n⏳ Estimasi kembali: {exp.strftime('%H:%M')} WIB")
     except Exception:
      c.rollback(); raise
   elif action=='in':
    active_now=c.execute("SELECT * FROM leaves WHERE staff_id=? AND status='OUT' ORDER BY id DESC LIMIT 1",(sid,)).fetchone()
    if not active_now:
     flash('Tidak ada izin aktif untuk akun ini.','danger')
    else:
     current_device=(request.cookies.get('om_device_id') or session.get('browser_device_token') or '').strip()
     leave_device=(active_now['device_token'] or g.user['device_token'] or '').strip()
     if leave_device and current_device!=leave_device:
      flash('Tidak bisa beda device login','danger'); return redirect(url_for('inout_page'))
     t=now(); exp=datetime.fromisoformat(active_now['expected_at'])
     # Denda hanya dihitung setelah lewat satu menit penuh. Contoh: lewat 30 detik = Rp0, lewat 1:00 = Rp50.000.
     late=max(0,int((t-exp).total_seconds()//60)); fine=late*50000 if 1<=late<=9 else (500000 if late>=10 else 0)
     duration_sec=leave_duration_seconds(active_now,t)
     c.execute("UPDATE leaves SET in_at=?,status='IN',late_minutes=?,fine=?,auto_in=0 WHERE id=?",(t.isoformat(),late,fine,active_now['id'])); c.commit()
     dm,ds=divmod(duration_sec,60); dh,dm=divmod(dm,60); duration_text=(f"{dh} jam {dm} menit {ds} detik" if dh else f"{dm} menit {ds} detik")
     tg_send_inout(f"✅ <b>SUDAH KEMBALI</b>\n👤 {staff['name']}\n⏱ Durasi keluar: {duration_text}\n⏱ Terlambat: {late} menit\n💸 Denda: Rp{fine:,}")
   return redirect(url_for('inout_page'))
  active=c.execute("SELECT * FROM leaves WHERE staff_id=? AND status='OUT' ORDER BY id DESC LIMIT 1",(sid,)).fetchone()
  active_count=c.execute("SELECT COUNT(*) n FROM leaves WHERE status='OUT'").fetchone()['n']
  month_start=now().replace(day=1,hour=0,minute=0,second=0,microsecond=0); history_raw=c.execute('SELECT * FROM leaves WHERE staff_id=? AND out_at>=? ORDER BY id DESC LIMIT 100',(sid,month_start.isoformat())).fetchall()
  history=[]
  for r in history_raw:
   d=dict(r); d['duration_seconds']=leave_duration_seconds(r); history.append(d)
 return render_template('inout.html',staff=staff,active=active,history=history,assignment=ass,assignments=ass_rows,jobdesk_text=jobdesk_text,durations=DURATIONS,active_count=active_count,max_active=MAX_ACTIVE_LEAVES,retention_days=RETENTION_DAYS)

@app.get('/api/inout/active')
@login_required
def api_inout_active():
 with db_conn() as c:
  auto_events=process_auto_in_overdue(c); c.commit(); flush_auto_in_notifications(auto_events)
  rows=c.execute("""SELECT l.id,l.staff_id,l.reason,l.out_at,l.expected_at,s.name,s.cs_name,o.name office_name
                    FROM leaves l JOIN staff s ON s.id=l.staff_id LEFT JOIN offices o ON o.id=s.office_id
                    WHERE l.status='OUT' ORDER BY l.out_at ASC""").fetchall()
 data=[]
 for r in rows:
  data.append({'id':r['id'],'staff_id':r['staff_id'],'name':r['name'],'cs_name':r['cs_name'] or '-', 'office_name':r['office_name'] or '-', 'reason':r['reason'], 'out_at':r['out_at'], 'expected_at':r['expected_at']})
 return jsonify({'ok':True,'count':len(data),'max':MAX_ACTIVE_LEAVES,'is_full':len(data)>=MAX_ACTIVE_LEAVES,'items':data,'server_time':now().isoformat()})


@app.get('/inout-live')
@roles('superadmin','supervisor','leader')
def inout_live_page():
 with db_conn() as c:
  auto_events=process_auto_in_overdue(c); c.commit(); flush_auto_in_notifications(auto_events)
  today_start=now().replace(hour=0,minute=0,second=0,microsecond=0).isoformat()
  recent=c.execute('''SELECT l.*,s.name,s.cs_name,o.name office_name FROM leaves l JOIN staff s ON s.id=l.staff_id LEFT JOIN offices o ON o.id=s.office_id WHERE l.out_at>=? ORDER BY l.id DESC LIMIT 100''',(today_start,)).fetchall()
 return render_template('inout_live.html',recent=recent,max_active=MAX_ACTIVE_LEAVES)

@app.route('/warnings',methods=['GET','POST'])
@roles('superadmin','supervisor','leader')
def warnings_page():
 with db_conn() as c:
  if request.method=='POST':
   f=request.form; wid=f.get('id',type=int)
   try: warning_date=panel_date_required(f.get('warning_date'),'Tanggal SP')
   except ValueError as e: flash(str(e),'danger'); return redirect(url_for('warnings_page'))
   vals=(f.get('staff_id',type=int),f['type'],warning_date,f['reason'].strip(),f.get('fine',type=int) or 0,f.get('notes','').strip())
   if wid:
    oldw=c.execute('SELECT * FROM warnings WHERE id=?',(wid,)).fetchone(); c.execute('UPDATE warnings SET staff_id=?,type=?,warning_date=?,reason=?,fine=?,notes=? WHERE id=?',vals+(wid,)); stn=c.execute('SELECT name FROM staff WHERE id=?',(vals[0],)).fetchone(); neww=c.execute('SELECT * FROM warnings WHERE id=?',(wid,)).fetchone(); audit(c,'warning.update',f"Staf: {stn['name'] if stn else vals[0]} · {vals[1]} · {vals[2]} · {vals[3]}",before=dict(oldw) if oldw else None,after=dict(neww) if neww else None,target_type='staff',target_id=vals[0])
   else:
    nwid=c.execute('INSERT INTO warnings(staff_id,type,warning_date,reason,fine,notes,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)',vals+(g.user['id'],now().isoformat())).lastrowid; stn=c.execute('SELECT name FROM staff WHERE id=?',(vals[0],)).fetchone(); neww=c.execute('SELECT * FROM warnings WHERE id=?',(nwid,)).fetchone(); audit(c,'warning.create',f"Staf: {stn['name'] if stn else vals[0]} · {vals[1]} · {vals[2]} · {vals[3]}",after=dict(neww) if neww else None,target_type='staff',target_id=vals[0])
   c.commit(); flash('Data SP berhasil disimpan.','success'); return redirect(url_for('warnings_page'))
  rows=c.execute('SELECT w.*,s.name,s.cs_name,o.name office_name FROM warnings w JOIN staff s ON s.id=w.staff_id LEFT JOIN offices o ON o.id=s.office_id ORDER BY w.warning_date DESC,w.id DESC').fetchall(); staff=c.execute("SELECT * FROM staff WHERE status!='Ex Karyawan' ORDER BY name").fetchall()
 return render_template('warnings.html',rows=rows,staff=staff)

@app.post('/warnings/<int:wid>/delete')
@roles('superadmin','supervisor')
def warning_delete(wid):
 with db_conn() as c:
  old=c.execute('SELECT * FROM warnings WHERE id=?',(wid,)).fetchone()
  if old: audit(c,'warning.delete',json.dumps(dict(old),default=str)); c.execute('DELETE FROM warnings WHERE id=?',(wid,)); c.commit(); flash('SP berhasil dihapus.','success')
 return redirect(url_for('warnings_page'))

@app.route('/former',methods=['GET','POST'])
@roles('superadmin','supervisor','leader')
def former_page():
 with db_conn() as c:
  if request.method=='POST':
   sid=request.form.get('staff_id',type=int)
   try: exit_date=panel_date_required(request.form.get('exit_date') or now().strftime('%d/%m/%Y'),'Tanggal Keluar')
   except ValueError as e: flash(str(e),'danger'); return redirect(url_for('former_page'))
   exit_reason=(request.form.get('exit_reason') or '').strip() or 'Dipindahkan ke Ex Karyawan'
   st=c.execute('SELECT name FROM staff WHERE id=?',(sid,)).fetchone()
   if not st: flash('Data staf tidak ditemukan.','danger'); return redirect(url_for('former_page'))
   c.execute("UPDATE staff SET status='Ex Karyawan',exit_date=?,exit_reason=? WHERE id=?",(exit_date,exit_reason,sid)); c.execute('UPDATE users SET is_active=0 WHERE staff_id=?',(sid,)); audit(c,'former.move',f"Staf: {st['name']} · Tanggal keluar: {exit_date} · Alasan: {exit_reason}"); c.commit(); flash('Staf berhasil dipindahkan ke Ex Karyawan.','success'); return redirect(url_for('former_page'))
  rows=c.execute("SELECT s.*,o.name office_name,u.username login_id FROM staff s LEFT JOIN offices o ON o.id=s.office_id LEFT JOIN users u ON u.staff_id=s.id WHERE s.status='Ex Karyawan' ORDER BY s.name").fetchall(); active=c.execute("SELECT * FROM staff WHERE status='Aktif'").fetchall()
 return render_template('former.html',rows=rows,active=active)

@app.post('/former/<int:sid>/reactivate')
@roles('superadmin','supervisor','leader')
def former_reactivate(sid):
 with db_conn() as c:
  st=c.execute("SELECT * FROM staff WHERE id=? AND status='Ex Karyawan'",(sid,)).fetchone()
  if not st: flash('Ex Karyawan tidak ditemukan.','danger'); return redirect(url_for('former_page'))
  c.execute("UPDATE staff SET status='Aktif',exit_date=NULL,exit_reason=NULL WHERE id=?",(sid,))
  # Aktifkan kembali akun yang sudah ada dan reset ikatan device agar staf dapat login lagi.
  c.execute('UPDATE users SET is_active=1,device_token=NULL WHERE staff_id=?',(sid,))
  audit(c,'former.reactivate',f"Staf: {st['name']} · Dikembalikan menjadi Karyawan Aktif"); c.commit(); flash(f"{st['name']} berhasil dikembalikan ke Karyawan Aktif.",'success')
 return redirect(url_for('former_page'))

@app.route('/memos',methods=['GET','POST'])
@login_required
def memos_page():
 return redirect(url_for('dashboard'))

@app.get('/monitor')
@roles('superadmin','supervisor','leader')
def monitor_page():
 return redirect(url_for('reports_page'))

@app.get('/reports')
@roles('superadmin','supervisor','leader')
def reports_page():
 return redirect(url_for('leader_reports_page'))

@app.get('/staff-assignments')
@roles('superadmin','supervisor','leader')
def staff_assignments_page():
 return redirect(url_for('operations_page'))

# Deposit Monitor Sync PRO v4.2 compatible API

def authorized(): return bool(API_KEY) and request.headers.get('X-API-Key','')==API_KEY

def _monitor_settings():
 return {'enabled':True,'lateMinutes':LATE_MINUTES,'withdrawLateMinutes':WITHDRAW_LATE_MINUTES,'scanSeconds':SCAN_SECONDS,'leaderTtlSeconds':LEADER_TTL_SECONDS,'maxDevices':MAX_DEVICES}

def _choose_monitor_leader(c,office_id=None,ts=None):
 # Deposit Monitor V24 bersifat GLOBAL: hanya satu leader scanner untuk seluruh kantor.
 ts=int(ts or time.time()); cutoff=ts-LEADER_TTL_SECONDS
 row=c.execute('SELECT device_id FROM devices WHERE last_seen>=? ORDER BY device_id LIMIT 1',(cutoff,)).fetchone()
 return row['device_id'] if row else None

@app.get('/api/health')
def api_health():
 return jsonify(status='ok',service='omtogel-staff-deposit-withdraw-sync-v33',db=DB_PATH,lateMinutes=LATE_MINUTES,scanSeconds=SCAN_SECONDS)

@app.get('/api/offices')
def api_offices():
 if not authorized(): return jsonify(ok=False,error='API key tidak valid'),401
 with db_conn() as c:
  rows=c.execute("SELECT id,name,location FROM offices WHERE status='Aktif' ORDER BY name").fetchall()
 return jsonify(ok=True,offices=[dict(x) for x in rows])

@app.post('/api/test-telegram')
def api_test_telegram():
 if not authorized(): return jsonify(ok=False,error='API key tidak valid'),401
 if not BOT_TOKEN or not ALERT_CHAT_ID: return jsonify(ok=False,error='BOT_TOKEN / ALERT_CHAT_ID belum diatur'),400
 ok=tg_send(ALERT_CHAT_ID,'✅ <b> Monitor Kenzo </b>\nServer Kenzo Tiger8008 dan Telegram terhubung.')
 return jsonify(ok=True) if ok else (jsonify(ok=False,error='Telegram gagal mengirim pesan'),502)

@app.route('/api/heartbeat',methods=['POST'])
def heartbeat():
 if not authorized():return jsonify(ok=False,error='API key tidak valid'),401
 d=request.get_json(silent=True) or {}; did=str(d.get('deviceId','')).strip(); name=str(d.get('deviceName') or 'Perangkat').strip()
 if not did:return jsonify(ok=False,error='deviceId wajib'),400
 office_id=None  # V24: extension global, kantor ditentukan dari Penugasan Kerja yang cocok.
 ts=int(time.time())
 with lock,db_conn() as c:
  old=c.execute('SELECT * FROM devices WHERE device_id=?',(did,)).fetchone()
  if not old and MAX_DEVICES>0:
   active_total=c.execute('SELECT COUNT(*) n FROM devices WHERE last_seen>=?',(ts-LEADER_TTL_SECONDS,)).fetchone()['n']
   if active_total>=MAX_DEVICES:return jsonify(ok=False,error=f'Batas perangkat online tercapai ({MAX_DEVICES})'),403
  if old:
   c.execute('UPDATE devices SET device_name=?,office_id=?,last_seen=?,page_url=?,form_count=?,late_count=? WHERE device_id=?',(name,office_id,ts,str(d.get('pageUrl') or ''),int(d.get('formCount') or 0),int(d.get('lateCount') or 0),did))
  else:
   c.execute('INSERT INTO devices(device_id,device_name,office_id,last_seen,page_url,form_count,late_count) VALUES(?,?,?,?,?,?,?)',(did,name,office_id,ts,str(d.get('pageUrl') or ''),int(d.get('formCount') or 0),int(d.get('lateCount') or 0)))
  c.execute('DELETE FROM devices WHERE last_seen<?',(ts-7*24*3600,)); c.commit(); leader=_choose_monitor_leader(c,office_id,ts)
 return jsonify(ok=True,isLeader=(leader==did),leaderDeviceId=leader,settings=_monitor_settings(),serverTimeWib=now().strftime('%Y-%m-%d %H:%M:%S'),officeId=office_id)

@app.route('/api/forms',methods=['POST'])
@app.route('/api/form-alert',methods=['POST'])
def form_alert():
 if not LEGACY_PENDING_MONITOR_ENABLED:return jsonify(ok=False,error='Pending DP legacy dinonaktifkan untuk Marketing 8008'),410
 if not authorized():return jsonify(ok=False,error='API key tidak valid'),401
 d=request.get_json(silent=True) or {}; client_form_id=str(d.get('formId') or d.get('id') or '').strip(); device_id=str(d.get('deviceId') or '').strip()
 if not client_form_id:return jsonify(ok=False,error='formId wajib'),400
 if not device_id:return jsonify(ok=False,error='deviceId wajib'),400
 fp='|'.join([str(d.get('username') or '').strip().lower(),str(d.get('formTime') or '').strip(),str(d.get('amount') or '').replace(',','').strip()])
 form_id=hashlib.sha1(fp.encode('utf-8')).hexdigest() if all(fp.split('|')) else client_form_id
 status=str(d.get('status') or 'pending').lower().strip(); last=int(time.time())
 try:age=max(0,int(float(d.get('ageMinutes') or 0)))
 except (TypeError,ValueError):age=0
 try:first=int(d.get('firstSeen') or (last-age*60))
 except (TypeError,ValueError):first=last-age*60
 done=status in ('done','processed','completed','success','approved','selesai')
 info_to=_parse_info_to(d.get('info') or d.get('rowText') or '')
 destination=str(d.get('destination') or info_to.get('bank') or d.get('targetBank') or '').strip()
 dest_account=str(d.get('destinationAccount') or info_to.get('account') or '').strip()
 dest_owner=str(d.get('destinationOwner') or info_to.get('owner') or '').strip()
 if not d.get('targetBank') and info_to.get('bank'):
  d['targetBank']=_source_bank_name(info_to.get('bank'))
 with lock,db_conn() as c:
  assignments,map_status,expected_jobdesk=find_deposit_assignment_global(c,d,now())
  primary=assignments[0] if assignments else None
  sid=primary['staff_id'] if primary else None; office_id=primary['office_id'] if primary else None
  existing=c.execute('SELECT * FROM deposit_forms WHERE form_id=?',(form_id,)).fetchone()
  if not existing:
   existing=c.execute("SELECT * FROM deposit_forms WHERE lower(COALESCE(username,''))=lower(?) AND COALESCE(form_time,'')=? AND REPLACE(COALESCE(amount,''),',','')=? ORDER BY id DESC LIMIT 1",(str(d.get('username') or '').strip(),str(d.get('formTime') or '').strip(),str(d.get('amount') or '').replace(',','').strip())).fetchone()
   if existing and existing['form_id']!=form_id:
    try:
     c.execute('UPDATE deposit_forms SET form_id=? WHERE id=?',(form_id,existing['id']))
     existing=c.execute('SELECT * FROM deposit_forms WHERE id=?',(existing['id'],)).fetchone()
    except sqlite3.IntegrityError:
     existing=c.execute('SELECT * FROM deposit_forms WHERE form_id=?',(form_id,)).fetchone() or existing
  if (not destination or destination=='-') and existing:destination=existing['destination'] or ''
  if (not dest_account or dest_account=='-') and existing:dest_account=existing['destination_account'] or ''
  if (not dest_owner or dest_owner=='-') and existing:dest_owner=existing['destination_owner'] or ''
  processed_at=now().isoformat() if done else (existing['processed_at'] if existing else None)
  primary_staff=primary['staff_name'] if primary else None; primary_cs=primary['cs_name'] if primary else None
  primary_job=expected_jobdesk or (primary['channel_name'] if primary else None); primary_office=primary['office_name'] if primary else None
  leave=c.execute("SELECT id FROM leaves WHERE staff_id=? AND status='OUT' ORDER BY id DESC LIMIT 1",(sid,)).fetchone() if sid else None
  staff_status='OUT' if leave else 'Aktif'
  if existing:
   c.execute("UPDATE deposit_forms SET device_id=?,office_id=COALESCE(?,office_id),username=?,game_id=?,destination=?,destination_account=?,destination_owner=?,form_time=?,amount=?,bank=?,first_seen=CASE WHEN first_seen IS NULL OR first_seen=0 OR first_seen>? THEN ? ELSE first_seen END,last_seen=?,status=?,staff_id=COALESCE(?,staff_id),assignment_id=COALESCE(?,assignment_id),staff_status=?,processed_at=?,staff_name_snapshot=COALESCE(?,staff_name_snapshot),cs_name_snapshot=COALESCE(?,cs_name_snapshot),jobdesk_snapshot=COALESCE(?,jobdesk_snapshot),office_snapshot=COALESCE(?,office_snapshot),mapping_status=?,balance_group=? WHERE id=?",(device_id,office_id,d.get('username'),d.get('gameId'),destination,dest_account,dest_owner,d.get('formTime'),str(d.get('amount','')),d.get('targetBank') or d.get('bank'),first,first,last,status,sid,primary['id'] if primary else None,staff_status,processed_at,primary_staff,primary_cs,primary_job,primary_office,map_status,d.get('balanceGroup'),existing['id']))
  else:
   c.execute("INSERT INTO deposit_forms(form_id,device_id,office_id,username,game_id,destination,destination_account,destination_owner,form_time,amount,bank,first_seen,last_seen,status,alert_sent,staff_id,assignment_id,staff_status,processed_at,staff_name_snapshot,cs_name_snapshot,jobdesk_snapshot,office_snapshot,age_at_alert,mapping_status,balance_group) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?,?,?,?,?)",(form_id,device_id,office_id,d.get('username'),d.get('gameId'),destination,dest_account,dest_owner,d.get('formTime'),str(d.get('amount','')),d.get('targetBank') or d.get('bank'),first,last,status,sid,primary['id'] if primary else None,staff_status,processed_at,primary_staff,primary_cs,primary_job,primary_office,0,map_status,d.get('balanceGroup')))
  row=c.execute('SELECT * FROM deposit_forms WHERE form_id=?',(form_id,)).fetchone()
  if done:c.commit();return jsonify(ok=True,sent=False,reason='Form sudah selesai',formId=form_id)
  if age<LATE_MINUTES:c.commit();return jsonify(ok=True,sent=False,reason='Belum lewat batas waktu',formId=form_id,ageMinutes=age)
  for a in assignments:
   c.execute('INSERT OR IGNORE INTO deposit_alert_staff(deposit_form_id,staff_id,assignment_id,staff_name_snapshot,cs_name_snapshot,office_snapshot,jobdesk_snapshot,shift_id_snapshot,shift_name_snapshot,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(row['id'],a['staff_id'],a['id'],a['staff_name'],a['cs_name'],a['office_name'],expected_jobdesk or a['channel_name'],a['shift_id'],a['shift_name'],now().isoformat()))
  followup=False
  if row['alert_sent']:
   last_alert=None
   try:
    last_alert=datetime.fromisoformat(str(row['alerted_at'])) if row['alerted_at'] else None
    if last_alert and last_alert.tzinfo is None: last_alert=last_alert.replace(tzinfo=WIB)
   except Exception: last_alert=None
   if last_alert and (now()-last_alert).total_seconds()<120:
    c.commit(); linked=c.execute('SELECT COUNT(*) n FROM deposit_alert_staff WHERE deposit_form_id=?',(row['id'],)).fetchone()['n']
    return jsonify(ok=True,sent=False,reason='Sudah pernah dikirim',formId=form_id,ageMinutes=age,staffCount=linked,jobdesk=expected_jobdesk,mappingStatus=map_status)
   followup=True
  c.execute('UPDATE deposit_forms SET alert_sent=1,age_at_alert=?,alerted_at=?,mapping_status=?,jobdesk_snapshot=COALESCE(?,jobdesk_snapshot) WHERE id=?',(age,now().isoformat(),map_status,expected_jobdesk,row['id']));c.commit()
  linked=c.execute("SELECT das.*,s.name staff_name,s.cs_name,o.name office_name FROM deposit_alert_staff das JOIN staff s ON s.id=das.staff_id LEFT JOIN offices o ON o.id=s.office_id WHERE das.deposit_form_id=? ORDER BY COALESCE(NULLIF(das.cs_name_snapshot,''),s.cs_name,s.name)",(row['id'],)).fetchall()
  cs_names=[]; offices=[]
  for x in linked:
   cs=(x['cs_name_snapshot'] or x['cs_name'] or x['staff_name_snapshot'] or x['staff_name'] or '').strip(); off=(x['office_snapshot'] or x['office_name'] or '').strip()
   if cs and cs not in cs_names:cs_names.append(cs)
   if off and off not in offices:offices.append(off)
  cs_text=', '.join(cs_names) if cs_names else 'BELUM DISET'; office_text=', '.join(offices) if offices else '-'; jobdesk=expected_jobdesk or row['jobdesk_snapshot'] or '-'
  dest_bank=destination if destination and destination!='-' else (row['destination'] or '-')
  dest_acc=dest_account if dest_account and dest_account!='-' else (row['destination_account'] or '-')
  dest_own=dest_owner if dest_owner and dest_owner!='-' else (row['destination_owner'] or '-')
 title='🔁 <b>FOLLOW UP FORM DEPOSIT TERLAMBAT</b>' if followup else '⚠️ <b>FORM DEPOSIT TERLAMBAT</b>'
 msg=(title+"\n\n"+f"👥 CS: <b>{escape(str(cs_text))}</b>\n"+f"🏢 Kantor: {escape(str(office_text))}\n"+f"💼 Jobdesk: {escape(str(jobdesk))}\n\n"+f"🆔 Member: <b>{escape(str(d.get('username') or '-'))}</b>\n"+f"🕒 Waktu Form: {escape(str(d.get('formTime') or '-'))}\n"+f"⏳ Umur Form: <b>{age} menit</b>\n"+f"💰 Amount: {escape(str(d.get('amount') or '-'))}\n"+f"🎯 Tujuan: {escape(str(dest_bank))} - {escape(str(dest_acc))} - {escape(str(dest_own))}")
 sent=tg_send(ALERT_CHAT_ID,msg)
 if not sent:
  with lock,db_conn() as c:
   c.execute('UPDATE deposit_forms SET alert_sent=0,alerted_at=NULL WHERE form_id=?',(form_id,));c.commit()
  return jsonify(ok=False,error='Telegram gagal mengirim alert'),502
 return jsonify(ok=True,sent=True,followup=followup,reason=('Follow-up terkirim' if followup else 'Terkirim'),formId=form_id,ageMinutes=age,staffCount=len(linked),csNames=cs_names,jobdesk=jobdesk,mappingStatus=map_status)


@app.route('/api/withdraw-alert',methods=['POST'])
def withdraw_alert():
 if not LEGACY_PENDING_MONITOR_ENABLED:return jsonify(ok=False,error='Pending WD legacy dinonaktifkan untuk Marketing 8008'),410
 if not authorized():return jsonify(ok=False,error='API key tidak valid'),401
 d=request.get_json(silent=True) or {}; device_id=str(d.get('deviceId') or '').strip()
 if not device_id:return jsonify(ok=False,error='deviceId wajib'),400
 username=str(d.get('username') or '').strip(); form_time=str(d.get('formTime') or '').strip(); amount=str(d.get('amount') or '').strip()
 bank=_source_bank_name(d.get('bank') or d.get('bankOrigin') or d.get('sourceBank') or '')
 bank_account=str(d.get('bankAccount') or '').strip(); bank_owner=str(d.get('bankOwner') or '').strip()
 fp='WD|'+('|'.join([username.lower(),form_time,amount.replace(',','').strip(),bank])); form_id=hashlib.sha1(fp.encode('utf-8')).hexdigest()
 try: age=max(0,int(float(d.get('ageMinutes') or 0)))
 except (TypeError,ValueError): age=0
 last=int(time.time()); first=last-age*60; status=str(d.get('status') or 'pending').lower().strip(); done=status in ('done','processed','completed','success','approved','selesai')
 with lock,db_conn() as c:
  assignments,map_status,expected_jobdesk=find_withdraw_assignment_global(c,d,now())
  existing=c.execute('SELECT * FROM withdraw_forms WHERE form_id=?',(form_id,)).fetchone()
  if existing:
   c.execute('UPDATE withdraw_forms SET device_id=?,username=?,form_time=?,amount=?,bank=?,bank_account=?,bank_owner=?,first_seen=CASE WHEN first_seen IS NULL OR first_seen=0 OR first_seen>? THEN ? ELSE first_seen END,last_seen=?,status=?,mapping_status=?,jobdesk_snapshot=COALESCE(?,jobdesk_snapshot) WHERE id=?',(device_id,username,form_time,amount,bank,bank_account,bank_owner,first,first,last,status,map_status,expected_jobdesk,existing['id']))
  else:
   c.execute('INSERT INTO withdraw_forms(form_id,device_id,username,form_time,amount,bank,bank_account,bank_owner,first_seen,last_seen,status,alert_sent,age_at_alert,mapping_status,jobdesk_snapshot) VALUES(?,?,?,?,?,?,?,?,?,?,?,0,0,?,?)',(form_id,device_id,username,form_time,amount,bank,bank_account,bank_owner,first,last,status,map_status,expected_jobdesk))
  row=c.execute('SELECT * FROM withdraw_forms WHERE form_id=?',(form_id,)).fetchone()
  if done:c.commit();return jsonify(ok=True,sent=False,reason='Form sudah selesai',formId=form_id)
  if age<WITHDRAW_LATE_MINUTES:c.commit();return jsonify(ok=True,sent=False,reason='Belum lewat batas waktu',formId=form_id,ageMinutes=age)
  for a in assignments:
   c.execute('INSERT OR IGNORE INTO withdraw_alert_staff(withdraw_form_id,staff_id,assignment_id,staff_name_snapshot,cs_name_snapshot,office_snapshot,jobdesk_snapshot,shift_id_snapshot,shift_name_snapshot,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(row['id'],a['staff_id'],a['id'],a['staff_name'],a['cs_name'],a['office_name'],expected_jobdesk or a['channel_name'],a['shift_id'],a['shift_name'],now().isoformat()))
  followup=False
  if row['alert_sent']:
   last_alert=None
   try:
    last_alert=datetime.fromisoformat(str(row['alerted_at'])) if row['alerted_at'] else None
    if last_alert and last_alert.tzinfo is None: last_alert=last_alert.replace(tzinfo=WIB)
   except Exception: last_alert=None
   if last_alert and (now()-last_alert).total_seconds()<120:
    c.commit(); linked=c.execute('SELECT COUNT(*) n FROM withdraw_alert_staff WHERE withdraw_form_id=?',(row['id'],)).fetchone()['n']
    return jsonify(ok=True,sent=False,reason='Sudah pernah dikirim',formId=form_id,ageMinutes=age,staffCount=linked,jobdesk=expected_jobdesk,mappingStatus=map_status)
   followup=True
  c.execute('UPDATE withdraw_forms SET alert_sent=1,age_at_alert=?,alerted_at=?,mapping_status=?,jobdesk_snapshot=COALESCE(?,jobdesk_snapshot) WHERE id=?',(age,now().isoformat(),map_status,expected_jobdesk,row['id']));c.commit()
  linked=c.execute("SELECT was.*,s.name staff_name,s.cs_name,o.name office_name FROM withdraw_alert_staff was JOIN staff s ON s.id=was.staff_id LEFT JOIN offices o ON o.id=s.office_id WHERE was.withdraw_form_id=? ORDER BY COALESCE(NULLIF(was.cs_name_snapshot,''),s.cs_name,s.name)",(row['id'],)).fetchall()
  cs_names=[]; offices=[]
  for x in linked:
   cs=(x['cs_name_snapshot'] or x['cs_name'] or x['staff_name_snapshot'] or x['staff_name'] or '').strip(); off=(x['office_snapshot'] or x['office_name'] or '').strip()
   if cs and cs not in cs_names:cs_names.append(cs)
   if off and off not in offices:offices.append(off)
  cs_text=', '.join(cs_names) if cs_names else 'BELUM DISET'; office_text=', '.join(offices) if offices else '-'; jobdesk=expected_jobdesk or '-'
 title='🔁 <b>FOLLOW UP FORM WITHDRAW TERLAMBAT</b>' if followup else '⚠️ <b>FORM WITHDRAW TERLAMBAT</b>'
 msg=(title+"\n\n"+f"👥 CS: <b>{escape(str(cs_text))}</b>\n"+f"🏢 Kantor: {escape(str(office_text))}\n"+f"💼 Jobdesk: {escape(str(jobdesk))}\n\n"+f"🆔 Member: <b>{escape(username or '-')}</b>\n"+f"🕒 Waktu Form: {escape(form_time or '-')}\n"+f"⏳ Umur Form: <b>{age} menit</b>\n"+f"💰 Amount: {escape(amount or '-')}\n"+f"🏦 Bank Asal: {escape(bank or '-')} - {escape(bank_account or '-')} - {escape(bank_owner or '-')}")
 chat_id=WITHDRAW_CHAT_ID or ALERT_CHAT_ID; sent=tg_send(chat_id,msg) if chat_id else False
 if not sent:
  with lock,db_conn() as c:
   c.execute('UPDATE withdraw_forms SET alert_sent=0,alerted_at=NULL WHERE form_id=?',(form_id,));c.commit()
  return jsonify(ok=False,error='Telegram WD gagal mengirim alert'),502
 return jsonify(ok=True,sent=True,followup=followup,reason=('Follow-up terkirim' if followup else 'Terkirim'),formId=form_id,ageMinutes=age,staffCount=len(linked),csNames=cs_names,jobdesk=jobdesk,mappingStatus=map_status)

@app.get('/api/status')
def api_status():
 if not authorized():return jsonify(ok=False,error='API key tidak valid'),401
 ts=int(time.time())
 with db_conn() as c:
  rows=c.execute("""SELECT d.*,o.name office_name FROM devices d LEFT JOIN offices o ON o.id=d.office_id ORDER BY d.last_seen DESC""").fetchall(); sent_count=c.execute('SELECT COUNT(*) n FROM deposit_forms WHERE alert_sent=1').fetchone()['n']; sent_wd=c.execute('SELECT COUNT(*) n FROM withdraw_forms WHERE alert_sent=1').fetchone()['n']; devices=[]; global_leader=_choose_monitor_leader(c,None,ts)
  for r in rows:
   item=dict(r); item['online']=bool((r['last_seen'] or 0)>=ts-LEADER_TTL_SECONDS); item['isLeader']=item['online'] and global_leader==r['device_id']; devices.append(item)
 return jsonify(ok=True,devices=devices,sentForms=sent_count,sentWithdraw=sent_wd,settings=_monitor_settings(),lateMinutes=LATE_MINUTES,withdrawLateMinutes=WITHDRAW_LATE_MINUTES,scanSeconds=SCAN_SECONDS,serverTimeWib=now().strftime('%Y-%m-%d %H:%M:%S'))


@app.get('/staff/add')
@roles('superadmin','supervisor','leader')
def staff_add_page():
 with db_conn() as c: offices=c.execute("SELECT * FROM offices WHERE status='Aktif' ORDER BY name").fetchall()
 return render_template('staff_add.html',offices=offices)

@app.get('/leader-reports')
@roles('superadmin','supervisor','leader')
def leader_reports_page():
 # Default laporan selalu tanggal hari ini. Filter hanya bisa diganti lewat date picker readonly.
 today=now().date(); yesterday=today-timedelta(days=1)
 default_start=today.isoformat(); default_end=today.isoformat()
 office_id=request.args.get('office_id',type=int); staff_id=request.args.get('staff_id',type=int); shift_id=request.args.get('shift_id',type=int)
 raw_start=(request.args.get('start') or '').strip(); raw_end=(request.args.get('end') or '').strip()
 try:
  start=panel_date_required(raw_start,'Tanggal Awal') if raw_start else default_start
  end=panel_date_required(raw_end,'Tanggal Akhir') if raw_end else default_end
  start_dt=datetime.fromisoformat(start+'T00:00:00').replace(tzinfo=WIB); end_dt=datetime.fromisoformat(end+'T23:59:59').replace(tzinfo=WIB)
  if end_dt<start_dt: raise ValueError('Tanggal Akhir tidak boleh lebih kecil dari Tanggal Awal.')
 except ValueError as e:
  flash(str(e),'danger')
  start=default_start; end=default_end
  start_dt=datetime.fromisoformat(start+'T00:00:00').replace(tzinfo=WIB); end_dt=datetime.fromisoformat(end+'T23:59:59').replace(tzinfo=WIB)
 start_display=fmt_date_id(start); end_display=fmt_date_id(end)
 start_ts=int(start_dt.timestamp()); end_ts=int(end_dt.timestamp())+1
 with db_conn() as c:
  offices=c.execute("SELECT * FROM offices WHERE status='Aktif' ORDER BY name COLLATE NOCASE").fetchall()
  shifts=c.execute("SELECT id,name,start_time,end_time FROM shifts WHERE office_id IS NULL AND status='Aktif' ORDER BY start_time,name COLLATE NOCASE").fetchall()
  staff=c.execute("SELECT id,name,office_id FROM staff WHERE status='Aktif' ORDER BY name COLLATE NOCASE").fetchall()
  # Pending DP/WD tidak termasuk Laporan Leader Marketing 8008.
  lp=[start_dt.isoformat(),end_dt.isoformat()]; lclauses=[]
  if office_id:lclauses.append('s.office_id=?');lp.append(office_id)
  if staff_id:lclauses.append('s.id=?');lp.append(staff_id)
  if shift_id:lclauses.append("COALESCE((SELECT a0.shift_id FROM assignments a0 WHERE a0.staff_id=s.id AND a0.work_date='CURRENT' AND a0.is_active=1 ORDER BY a0.id DESC LIMIT 1),(SELECT ss0.shift_id FROM shift_schedules ss0 WHERE ss0.staff_id=s.id AND ss0.work_date='CURRENT' ORDER BY ss0.id DESC LIMIT 1))=?");lp.append(shift_id)
  lw=(' AND '+' AND '.join(lclauses)) if lclauses else ''
  inout=c.execute('''SELECT s.id,s.name,s.cs_name,o.name office_name,COUNT(l.id) total_out,SUM(CASE WHEN l.late_minutes>0 THEN 1 ELSE 0 END) late_count,SUM(l.late_minutes) late_minutes,SUM(l.fine) total_fine,SUM(CASE WHEN l.status='OUT' THEN 1 ELSE 0 END) not_in_count
    FROM leaves l JOIN staff s ON s.id=l.staff_id LEFT JOIN offices o ON o.id=s.office_id WHERE l.out_at>=? AND l.out_at<=?'''+lw+''' GROUP BY s.id ORDER BY s.name COLLATE NOCASE''',lp).fetchall()
  detail_sql='''SELECT l.*,s.name,s.cs_name,o.name office_name FROM leaves l JOIN staff s ON s.id=l.staff_id LEFT JOIN offices o ON o.id=s.office_id WHERE l.out_at>=? AND l.out_at<=? AND (l.late_minutes>0 OR l.status='AUTO_IN' OR l.auto_in=1)'''
  detail_params=[start+'T00:00:00+07:00',end+'T23:59:59+07:00']
  if office_id: detail_sql+=' AND s.office_id=?'; detail_params.append(office_id)
  if staff_id: detail_sql+=' AND s.id=?'; detail_params.append(staff_id)
  if shift_id: detail_sql+=" AND COALESCE((SELECT a0.shift_id FROM assignments a0 WHERE a0.staff_id=s.id AND a0.work_date='CURRENT' AND a0.is_active=1 ORDER BY a0.id DESC LIMIT 1),(SELECT ss0.shift_id FROM shift_schedules ss0 WHERE ss0.staff_id=s.id AND ss0.work_date='CURRENT' ORDER BY ss0.id DESC LIMIT 1))=?"; detail_params.append(shift_id)
  detail_sql+=' ORDER BY s.name COLLATE NOCASE,l.out_at DESC'; inout_details=c.execute(detail_sql,detail_params).fetchall()
  inout_rank=sorted(inout,key=lambda x:(-(x['total_out'] or 0),-(x['late_count'] or 0),(x['name'] or '').lower()))
  # Staf tanpa aktivitas OUT sama sekali per tanggal pada rentang filter.
  active_for_missing=[]
  for x in staff:
   if office_id and x['office_id']!=office_id: continue
   if staff_id and x['id']!=staff_id: continue
   if shift_id:
    sr=c.execute("SELECT COALESCE((SELECT a0.shift_id FROM assignments a0 WHERE a0.staff_id=? AND a0.work_date='CURRENT' AND a0.is_active=1 ORDER BY a0.id DESC LIMIT 1),(SELECT ss0.shift_id FROM shift_schedules ss0 WHERE ss0.staff_id=? AND ss0.work_date='CURRENT' ORDER BY ss0.id DESC LIMIT 1)) sid",(x['id'],x['id'])).fetchone()
    if not sr or sr['sid']!=shift_id: continue
   active_for_missing.append(x)
  seen_rows=c.execute('''SELECT staff_id,substr(out_at,1,10) d FROM leaves WHERE out_at>=? AND out_at<=? GROUP BY staff_id,substr(out_at,1,10)''',(start_dt.isoformat(),end_dt.isoformat())).fetchall()
  seen={(int(x['staff_id']),x['d']) for x in seen_rows}
  missing_activity=[]; dcur=start_dt.date()
  while dcur<=end_dt.date():
   ds=dcur.isoformat()
   for st0 in active_for_missing:
    if (int(st0['id']),ds) not in seen: missing_activity.append({'date':ds,'id':st0['id'],'name':st0['name'],'office_id':st0['office_id']})
   dcur+=timedelta(days=1)
  # Absensi satu shift untuk laporan leader pada periode yang sama.
  att_sql='''SELECT a.*,s.name,s.cs_name,o.name office_name FROM attendance_records a JOIN staff s ON s.id=a.staff_id LEFT JOIN offices o ON o.id=s.office_id WHERE a.work_date>=? AND a.work_date<=?'''
  att_params=[start,end]
  if office_id: att_sql+=' AND s.office_id=?'; att_params.append(office_id)
  if staff_id: att_sql+=' AND s.id=?'; att_params.append(staff_id)
  att_sql+=' ORDER BY a.work_date DESC,s.name COLLATE NOCASE'
  attendance_rows=c.execute(att_sql,att_params).fetchall()
  attendance_by_staff={}
  for a in attendance_rows:
   key=a['staff_id']
   if key not in attendance_by_staff: attendance_by_staff[key]={'staff_id':key,'name':a['name'],'office_name':a['office_name'],'present':0,'late':0,'late_minutes':0,'fine':0}
   x=attendance_by_staff[key]; x['present']+=1; x['late']+=1 if a['status']=='TELAT' else 0; x['late_minutes']+=int(a['late_minutes'] or 0); x['fine']+=int(a['fine'] or 0)
  attendance_summary=sorted(attendance_by_staff.values(),key=lambda x:(-x['late'],x['name'].lower()))
  totals={'out':sum((x['total_out'] or 0) for x in inout),'late':sum((x['late_count'] or 0) for x in inout),'fine':sum((x['total_fine'] or 0) for x in inout),
          'attendance':len(attendance_rows),'attendance_late':sum(1 for x in attendance_rows if x['status']=='TELAT'),'attendance_fine':sum(int(x['fine'] or 0) for x in attendance_rows)}
 return render_template('leader_reports.html',inout=inout,inout_rank=inout_rank,inout_details=inout_details,missing_activity=missing_activity,attendance_rows=attendance_rows,attendance_summary=attendance_summary,totals=totals,offices=offices,shifts=shifts,staff=staff,start=start,end=end,start_display=start_display,end_display=end_display,today=today.isoformat(),yesterday=yesterday.isoformat(),office_id=office_id,staff_id=staff_id,shift_id=shift_id,retention_days=RETENTION_DAYS)

@app.route('/nawala',methods=['GET','POST'])
@login_required
def nawala_page():
 results=[]; raw=''
 if request.method=='POST':
  raw=request.form.get('domains',''); domains=[]
  for item in re.split(r'[\s,;]+',raw):
   d=clean_domain(item)
   if d and looks_like_domain(d) and d not in domains: domains.append(d)
   if len(domains)>=50: break
  if not domains: flash('Tidak ada domain valid.','danger')
  else:
   try:
    results=check_nawala(domains)
    if any(status=='ERROR' for _,status in results):
     flash('Sebagian hasil tidak dapat dibaca dari Nawala. Status ERROR tidak dianggap AMAN; silakan cek ulang.','danger')
   except Exception as e:
    print('[nawala]',repr(e),flush=True); results=[(d,'ERROR') for d in domains]; flash('Nawala tidak dapat dihubungi saat ini. Tidak ada domain yang dianggap AMAN tanpa hasil resmi.','danger')
 return render_template('nawala.html',results=results,raw=raw,checked_at=now() if results else None)

def clean_domain(value):
 d=(value or '').strip().lower()
 d=re.sub(r'^https?://','',d).split('/')[0].split('?')[0].split('#')[0]
 if d.startswith('www.'): d=d[4:]
 if ':' in d: d=d.split(':',1)[0]
 return d.strip('.')

def looks_like_domain(value):
 return bool(re.fullmatch(r'(?!-)[a-z0-9-]{1,63}(?<!-)(?:\.(?!-)[a-z0-9-]{1,63}(?<!-))+',clean_domain(value)))

def _nawala_status(text):
 """Konversi teks status resmi Nawala. Tidak pernah menebak AMAN bila status tidak ditemukan."""
 t=re.sub(r'\s+',' ',(text or '').strip().lower())
 if not t: return 'ERROR'
 # Frasa negatif harus dicek lebih dulu.
 if re.search(r'\b(tidak\s+ada|not\s+blocked|tidak\s+diblokir|aman|safe|clean)\b',t): return 'AMAN'
 if re.search(r'\b(blocked|diblokir|blokir|terblokir)\b',t): return 'BLOKIR'
 # UI Nawala lama menggunakan "Ada" = tercantum/terblokir dan "Tidak Ada" = aman.
 if re.fullmatch(r'.*\bada\b.*',t) and 'tidak ada' not in t: return 'BLOKIR'
 return 'ERROR'

def _parse_nawala_html_for_rows(html, requested_domains):
 soup=BeautifulSoup(html,'html.parser'); found={}
 def norm(x): return re.sub(r'\s+',' ',(x or '').strip().lower())
 # 1. Prioritas tabel hasil dengan kolom Domain/Situs dan Status/Keterangan.
 for table in soup.find_all('table'):
  first=table.find('thead') or table.find('tr')
  headers=[norm(x.get_text(' ',strip=True)) for x in first.find_all(['th','td'])] if first else []
  if not headers: continue
  try: di=next(i for i,h in enumerate(headers) if any(k in h for k in ('domain','situs','website')))
  except StopIteration: continue
  try: si=next(i for i,h in enumerate(headers) if any(k in h for k in ('status','keterangan')))
  except StopIteration: continue
  for tr in table.find_all('tr'):
   cells=tr.find_all('td')
   if len(cells)<=max(di,si): continue
   dom=clean_domain(cells[di].get_text(' ',strip=True))
   if not looks_like_domain(dom): continue
   stcell=cells[si]; badge=stcell.find(class_=re.compile(r'badge|status|label',re.I)); st=(badge or stcell).get_text(' ',strip=True)
   status=_nawala_status(st)
   if status!='ERROR': found[dom]=status
 # 2. Cari container terdekat yang memuat domain, agar tidak membaca teks status domain lain.
 for d in requested_domains:
  if d in found: continue
  node=soup.find(string=re.compile(r'(?<![a-z0-9-])'+re.escape(d)+r'(?![a-z0-9-])',re.I))
  if node:
   cur=node.parent
   for _ in range(5):
    if not cur: break
    txt=cur.get_text(' ',strip=True)
    status=_nawala_status(txt)
    if status!='ERROR': found[d]=status; break
    cur=cur.parent
 # 3. Fallback regex sangat sempit di sekitar domain. Bila tidak pasti => ERROR, bukan AMAN.
 flat=' '.join(BeautifulSoup(html,'html.parser').stripped_strings)
 for d in requested_domains:
  if d in found: continue
  m=re.search(re.escape(d)+r'.{0,180}?(tidak\s+ada|not\s+blocked|aman|safe|blocked|diblokir|blokir|terblokir|\bada\b)',flat,re.I)
  if m:
   st=_nawala_status(m.group(1))
   if st!='ERROR': found[d]=st
 return [(d,found.get(d,'ERROR')) for d in requested_domains]

def check_nawala(domains):
 """Cek langsung ke nawala.in setiap request. Tidak memakai cache hasil lokal."""
 url='https://nawala.in/'
 headers={
  'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36',
  'Accept':'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
  'Accept-Language':'id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7',
  'Cache-Control':'no-cache, no-store, max-age=0','Pragma':'no-cache','Referer':url,'Origin':'https://nawala.in'
 }
 sess=requests.Session(); sess.headers.update(headers)
 r=sess.get(url,timeout=25,params={'_ts':int(time.time()*1000)}); r.raise_for_status(); soup=BeautifulSoup(r.text,'html.parser')
 hidden={}
 for inp in soup.find_all('input'):
  if (inp.get('type') or '').lower()=='hidden' and inp.get('name'): hidden[inp.get('name')]=inp.get('value') or ''
 # Ikuti action form bila Nawala mengubah endpoint.
 form=None
 for f in soup.find_all('form'):
  if f.find(attrs={'name':re.compile(r'domain',re.I)}) or 'domain' in f.get_text(' ',strip=True).lower(): form=f; break
 action=(form.get('action') if form else '') or url
 if action.startswith('/'): action='https://nawala.in'+action
 elif not action.startswith('http'): action='https://nawala.in/'+action.lstrip('./')
 # Gunakan field yang dipakai source Nawala lama; hidden token ikut dikirim.
 payload={**hidden,'domains':'\n'.join(domains)}
 p=sess.post(action,data=payload,timeout=35,allow_redirects=True,headers={**headers,'Cache-Control':'no-cache, no-store, max-age=0'}); p.raise_for_status()
 rows=_parse_nawala_html_for_rows(p.text,domains)
 # Jangan pernah mengubah ERROR menjadi AMAN. Ini mencegah false-safe saat HTML berubah/Cloudflare muncul.
 return rows

@app.route('/mistakes',methods=['GET','POST'])
@login_required
def mistakes_page():
 if g.user['role']=='staff' and not menu_allowed('mistakes'): return ('Akses Catatan Mistake dinonaktifkan oleh Master.',403)
 if not g.user['staff_id']: return ('Akun ini tidak terhubung ke Data Staf.',400)
 sid=g.user['staff_id']
 with db_conn() as c:
  if request.method=='POST':
   action=(request.form.get('action') or 'save').strip()
   mid=request.form.get('id',type=int)
   if action=='delete':
    row=c.execute('SELECT * FROM mistake_ledger WHERE id=? AND staff_id=?',(mid,sid)).fetchone()
    if row:
     c.execute('DELETE FROM mistake_ledger WHERE id=? AND staff_id=?',(mid,sid)); c.commit(); flash('Catatan berhasil dihapus.','success')
    return redirect(url_for('mistakes_page'))
   try: entry_date=panel_date_required(request.form.get('entry_date'),'Tanggal Mistake')
   except ValueError as e: flash(str(e),'danger'); return redirect(url_for('mistakes_page'))
   entry_type=(request.form.get('entry_type') or 'MISTAKE').upper().strip()
   if entry_type not in ('MISTAKE','POTONGAN'):
    flash('Jenis catatan tidak valid.','danger'); return redirect(url_for('mistakes_page'))
   amount=max(0,request.form.get('amount',type=int) or 0)
   title=(request.form.get('title') or '').strip()[:160]
   notes=(request.form.get('notes') or '').strip()[:1000]
   staff_note=(request.form.get('staff_note') or '').strip()[:1000]
   if amount<=0:
    flash('Nominal harus lebih dari Rp0.','danger'); return redirect(url_for('mistakes_page'))
   if mid:
    row=c.execute('SELECT id FROM mistake_ledger WHERE id=? AND staff_id=?',(mid,sid)).fetchone()
    if not row:
     flash('Catatan tidak ditemukan.','danger'); return redirect(url_for('mistakes_page'))
    c.execute('UPDATE mistake_ledger SET entry_date=?,entry_type=?,amount=?,title=?,notes=?,staff_note=?,updated_at=? WHERE id=? AND staff_id=?',(entry_date,entry_type,amount,title,notes,staff_note,now().isoformat(),mid,sid))
    flash('Catatan pribadi berhasil diperbarui.','success')
   else:
    c.execute('INSERT INTO mistake_ledger(staff_id,entry_date,entry_type,amount,title,notes,staff_note,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(sid,entry_date,entry_type,amount,title,notes,staff_note,g.user['id'],now().isoformat(),now().isoformat()))
    flash('Catatan pribadi berhasil ditambahkan.','success')
   c.commit(); return redirect(url_for('mistakes_page'))
  rows=c.execute('SELECT * FROM mistake_ledger WHERE staff_id=? ORDER BY entry_date DESC,id DESC',(sid,)).fetchall()
  total_mistake=sum(r['amount'] for r in rows if r['entry_type']=='MISTAKE'); total_cut=sum(r['amount'] for r in rows if r['entry_type']=='POTONGAN'); remaining=max(0,total_mistake-total_cut)
 return render_template('mistakes.html',rows=rows,total_mistake=total_mistake,total_cut=total_cut,remaining=remaining,today=now().date().isoformat())

@app.post('/mistakes/<int:mid>/delete')
@login_required
def mistake_self_delete(mid):
 if not g.user['staff_id']: return ('Akses ditolak',403)
 with db_conn() as c:
  row=c.execute('SELECT id FROM mistake_ledger WHERE id=? AND staff_id=?',(mid,g.user['staff_id'])).fetchone()
  if row:
   c.execute('DELETE FROM mistake_ledger WHERE id=? AND staff_id=?',(mid,g.user['staff_id'])); c.commit(); flash('Catatan pribadi berhasil dihapus.','success')
 return redirect(url_for('mistakes_page'))

@app.route('/mistakes/manage',methods=['GET','POST'])
@roles('superadmin','supervisor','leader')
def mistakes_manage_page():
 return ('Catatan Mistake adalah catatan pribadi masing-masing staf dan tidak dikelola Leader/Master.',403)

@app.post('/mistakes/manage/<int:mid>/delete')
@roles('superadmin','supervisor','leader')
def mistake_delete(mid):
 return ('Catatan Mistake adalah catatan pribadi masing-masing staf dan tidak dapat dihapus Leader/Master.',403)

@app.get('/history')
@login_required
def my_history_page():
 if g.user['role']=='staff' and not menu_allowed('history'): return ('Akses History Saya dinonaktifkan oleh Master.',403)
 sid=g.user['staff_id']
 if not sid:return ('Akun tidak terhubung ke staf.',400)
 cutoff=now()-timedelta(days=RETENTION_DAYS); start_ts=int(cutoff.timestamp()); month_start=now().replace(day=1,hour=0,minute=0,second=0,microsecond=0)
 with db_conn() as c:
  sp=c.execute('SELECT * FROM warnings WHERE staff_id=? ORDER BY warning_date DESC,id DESC',(sid,)).fetchall(); leaves=c.execute('SELECT * FROM leaves WHERE staff_id=? AND out_at>=? ORDER BY out_at DESC',(sid,month_start.isoformat())).fetchall(); pending=c.execute('''SELECT f.*,das.jobdesk_snapshot staff_jobdesk FROM deposit_alert_staff das JOIN deposit_forms f ON f.id=das.deposit_form_id WHERE das.staff_id=? AND f.alert_sent=1 AND f.first_seen>=? ORDER BY f.first_seen DESC''',(sid,start_ts)).fetchall(); mistakes=c.execute('SELECT * FROM mistake_ledger WHERE staff_id=? ORDER BY entry_date DESC,id DESC',(sid,)).fetchall()
  summary={'sp':len(sp),'pending':len(pending),'late_inout':sum(1 for x in leaves if (x['late_minutes'] or 0)>0),'fine':sum((x['fine'] or 0) for x in leaves),'mistake':sum(x['amount'] for x in mistakes if x['entry_type']=='MISTAKE'),'cut':sum(x['amount'] for x in mistakes if x['entry_type']=='POTONGAN')}
 return render_template('history.html',sp=sp,leaves=leaves,pending=pending,mistakes=mistakes,summary=summary,retention_days=RETENTION_DAYS,inout_month=month_start.strftime('%B %Y'))

@app.get('/login-logs')
@roles('superadmin','supervisor','leader')
def login_logs_page():
 event=request.args.get('event',''); q=(request.args.get('q') or '').strip(); office_id=request.args.get('office_id',type=int)
 with db_conn() as c:
  sql='''SELECT l.*,u.username,s.name staff_name,o.name office_name FROM login_logs l LEFT JOIN users u ON u.id=l.user_id LEFT JOIN staff s ON s.id=l.staff_id LEFT JOIN offices o ON o.id=s.office_id WHERE 1=1'''; p=[]
  if event:sql+=' AND l.event=?';p.append(event)
  if q:sql+=' AND (lower(u.username) LIKE ? OR lower(s.name) LIKE ?)';p.extend([f'%{q.lower()}%',f'%{q.lower()}%'])
  if office_id:sql+=' AND s.office_id=?';p.append(office_id)
  sql+=' ORDER BY l.id DESC LIMIT 1000'; rows=c.execute(sql,p).fetchall(); offices=c.execute('SELECT * FROM offices ORDER BY name').fetchall()
 return render_template('login_logs.html',rows=rows,offices=offices,event=event,q=q,office_id=office_id)

@app.get('/account/theme/<mode>')
@login_required
def set_theme(mode):
 if mode not in ('dark','light'):return redirect(url_for('account_page'))
 session['theme']=mode;return redirect(request.referrer or url_for('account_page'))

@app.get('/cek-nawala-coming-soon')
@login_required
def cek_nawala_legacy(): return redirect(url_for('nawala_page'))


@app.route('/settings/policy',methods=['GET','POST'])
@roles('superadmin')
def policy_settings_page():
 with db_conn() as c:
  def getv(k,d=''):
   r=c.execute('SELECT value FROM settings WHERE key=?',(k,)).fetchone(); return r['value'] if r else d
  if request.method=='POST':
   old={k:getv('policy.'+k,'') for k in ('title','subtitle','warning','message','rules','enabled','version')}
   rules=[x.strip() for x in request.form.getlist('rules') if x.strip()]
   if not rules: flash('Minimal satu peraturan wajib diisi.','danger'); return redirect(url_for('policy_settings_page'))
   current_ver=getv('policy.version',POLICY_VERSION)
   stamp=now().strftime('%Y%m%d%H%M%S'); new_ver=f'{current_ver.split("-")[0]}-{stamp}'
   payload={'title':(request.form.get('title') or 'PERATURAN KANTOR').strip(),'subtitle':(request.form.get('subtitle') or 'WAJIB DIBACA DAN DIPATUHI').strip(),'warning':(request.form.get('warning') or '').strip(),'message':(request.form.get('message') or '').strip(),'rules':json.dumps(rules,ensure_ascii=False),'enabled':'1' if request.form.get('enabled') else '0','version':new_ver}
   for k,v in payload.items(): c.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('policy.'+k,v))
   audit(c,'policy.settings.update','Peraturan kantor diperbarui; seluruh pengguna wajib menyetujui versi baru pada login berikutnya.',before=old,after=payload,target_type='policy',target_id=new_ver)
   c.commit(); flash('Peraturan kantor berhasil diperbarui. Versi baru aktif.','success'); return redirect(url_for('policy_settings_page'))
  try: rules=json.loads(getv('policy.rules','[]'))
  except Exception: rules=[]
  if not rules: rules=list(DEFAULT_POLICY_RULES)
  cfg={'title':getv('policy.title','PERATURAN KANTOR'),'subtitle':getv('policy.subtitle','WAJIB DIBACA DAN DIPATUHI'),'warning':getv('policy.warning','Jangan langgar aturan yang sudah ditetapkan atau akan ada konsekuensinya!'),'message':getv('policy.message','Selamat Bekerja Tetap Fokus ☺♥'),'enabled':getv('policy.enabled','1')=='1','version':getv('policy.version',POLICY_VERSION),'rules':rules}
 return render_template('policy_settings.html',cfg=cfg)

@app.get('/reports/pending/<kind>/<int:sid>')
@roles('superadmin','supervisor','leader')
def pending_staff_detail(kind,sid):
 kind=kind.lower()
 if kind not in ('dp','wd'): return ('Jenis laporan tidak valid',400)
 today=now().date(); default=today.isoformat(); shift_id=request.args.get('shift_id',type=int)
 try:
  start=panel_date_required((request.args.get('start') or default),'Tanggal Awal'); end=panel_date_required((request.args.get('end') or default),'Tanggal Akhir')
 except ValueError as e:
  flash(str(e),'danger'); start=end=default
 start_dt=datetime.fromisoformat(start+'T00:00:00').replace(tzinfo=WIB); end_dt=datetime.fromisoformat(end+'T23:59:59').replace(tzinfo=WIB)
 if end_dt<start_dt: start=end=default; start_dt=datetime.fromisoformat(start+'T00:00:00').replace(tzinfo=WIB); end_dt=datetime.fromisoformat(end+'T23:59:59').replace(tzinfo=WIB)
 a,b=int(start_dt.timestamp()),int(end_dt.timestamp())+1
 shift=None
 with db_conn() as c:
  st=c.execute('SELECT s.*,o.name office_name FROM staff s LEFT JOIN offices o ON o.id=s.office_id WHERE s.id=?',(sid,)).fetchone()
  if not st:return ('Staf tidak ditemukan',404)
  if shift_id: shift=c.execute('SELECT id,name,start_time,end_time FROM shifts WHERE id=?',(shift_id,)).fetchone()
  if kind=='dp':
   q='''SELECT f.*,das.jobdesk_snapshot staff_jobdesk,das.cs_name_snapshot,das.office_snapshot,das.shift_name_snapshot FROM deposit_alert_staff das JOIN deposit_forms f ON f.id=das.deposit_form_id WHERE das.staff_id=? AND f.alert_sent=1 AND f.first_seen>=? AND f.first_seen<?'''; qp=[sid,a,b]
   if shift_id: q+=" AND COALESCE(das.shift_id_snapshot,(SELECT a0.shift_id FROM assignments a0 WHERE a0.staff_id=das.staff_id AND a0.work_date='CURRENT' AND a0.is_active=1 ORDER BY a0.id DESC LIMIT 1),(SELECT ss0.shift_id FROM shift_schedules ss0 WHERE ss0.staff_id=das.staff_id AND ss0.work_date='CURRENT' ORDER BY ss0.id DESC LIMIT 1))=?"; qp.append(shift_id)
   q+=' ORDER BY f.first_seen DESC'; rows=c.execute(q,qp).fetchall()
  else:
   q='''SELECT f.*,was.jobdesk_snapshot staff_jobdesk,was.cs_name_snapshot,was.office_snapshot,was.shift_name_snapshot FROM withdraw_alert_staff was JOIN withdraw_forms f ON f.id=was.withdraw_form_id WHERE was.staff_id=? AND f.alert_sent=1 AND f.first_seen>=? AND f.first_seen<?'''; qp=[sid,a,b]
   if shift_id: q+=" AND COALESCE(was.shift_id_snapshot,(SELECT a0.shift_id FROM assignments a0 WHERE a0.staff_id=was.staff_id AND a0.work_date='CURRENT' AND a0.is_active=1 ORDER BY a0.id DESC LIMIT 1),(SELECT ss0.shift_id FROM shift_schedules ss0 WHERE ss0.staff_id=was.staff_id AND ss0.work_date='CURRENT' ORDER BY ss0.id DESC LIMIT 1))=?"; qp.append(shift_id)
   q+=' ORDER BY f.first_seen DESC'; rows=c.execute(q,qp).fetchall()
  traffic=[0]*24
  for r in rows:
   try:traffic[datetime.fromtimestamp(int(r['first_seen']),WIB).hour]+=1
   except Exception:pass
  ages=[int(r['age_at_alert'] or (5 if kind=='dp' else 10)) for r in rows]
  summary={'total':len(rows),'avg':round(sum(ages)/len(ages),1) if ages else 0,'max':max(ages) if ages else 0,'peak_hour':max(range(24),key=lambda h:traffic[h]) if rows else None}
 return render_template('pending_detail.html',kind=kind,st=st,rows=rows,traffic=traffic,summary=summary,start=start,end=end,start_display=fmt_date_id(start),end_display=fmt_date_id(end),shift_id=shift_id,shift=shift)

@app.errorhandler(sqlite3.IntegrityError)
def handle_integrity_error(error):
 print('[integrity]', error, flush=True)
 if request.method=='POST':
  flash('Data tidak dapat disimpan karena ada nilai yang sama atau relasi data belum lengkap. Periksa kembali isian.','danger')
  return redirect(request.referrer or url_for('dashboard'))
 return ('Data tidak valid',400)

@app.errorhandler(sqlite3.OperationalError)
def handle_operational_error(error):
 print('[sqlite operational]', error, flush=True)
 return render_template('error.html', message='Database sedang bermasalah. Silakan muat ulang halaman atau hubungi Master.'),500

def background():
 last_cleanup_day=None
 while True:
  try:
   with db_conn() as c:
    # AUTO IN diproses juga di background, tetapi request/API memiliki fallback yang sama.
    current=now()
    auto_events=process_auto_in_overdue(c,current)
    c.commit()
    flush_auto_in_notifications(auto_events)
    today_key=now().date().isoformat()
    if last_cleanup_day!=today_key:
     # Backup harian SQLite sebelum cleanup/migrasi operasional. Simpan 14 backup terbaru.
     try:
      backup_dir=os.path.join(os.path.dirname(DB_PATH) or '.', 'backups'); os.makedirs(backup_dir,exist_ok=True)
      backup_path=os.path.join(backup_dir,f'omtogel_staff_{today_key}.db')
      if not os.path.exists(backup_path):
       dst=sqlite3.connect(backup_path); c.backup(dst); dst.close()
      backups=sorted([os.path.join(backup_dir,x) for x in os.listdir(backup_dir) if x.startswith('omtogel_staff_') and x.endswith('.db')])
      for old_backup in backups[:-14]:
       try: os.remove(old_backup)
       except OSError: pass
     except Exception as be: print('[backup]',be,flush=True)
     cutoff_dt=now()-timedelta(days=RETENTION_DAYS); cutoff_ts=int(cutoff_dt.timestamp()); cutoff_iso=cutoff_dt.isoformat()
     c.execute('DELETE FROM deposit_forms WHERE first_seen>0 AND first_seen<?',(cutoff_ts,))
     c.execute('DELETE FROM withdraw_forms WHERE first_seen>0 AND first_seen<?',(cutoff_ts,))
     c.execute('DELETE FROM leaves WHERE out_at<?',(cutoff_iso,)); c.execute('DELETE FROM login_logs WHERE created_at<?',(cutoff_iso,))
     c.execute('INSERT INTO audit_logs(user_id,action,detail,created_at) VALUES(NULL,?,?,?)',('retention.cleanup',f'older_than={RETENTION_DAYS}d',now().isoformat()))
     last_cleanup_day=today_key
    c.commit()
  except Exception as e:print('[background]',e,flush=True)
  time.sleep(2)
def start_bg():
 global bg_started
 if not bg_started:bg_started=True;threading.Thread(target=background,daemon=True).start()


@app.route('/attendance',methods=['GET','POST'])
@login_required
def attendance_page():
    if g.user['role']=='staff' and not menu_allowed('attendance'):
        return ('Akses absensi dinonaktifkan oleh Master.',403)
    today=now().date().isoformat()
    with db_conn() as c:
        if request.method=='POST':
            if not g.user['staff_id']:
                flash('Akun ini tidak terhubung ke data staf.','danger')
                return redirect(url_for('attendance_page'))
            current=now()
            state,late,fine=attendance_status_for(current)
            if state=='BELUM DIBUKA':
                flash(f'Absensi belum dibuka. Buka pukul {ATTENDANCE_OPEN} WIB.','danger')
                return redirect(url_for('attendance_page'))
            if state=='DITUTUP':
                flash(f'Absensi sudah ditutup pukul {ATTENDANCE_CLOSE} WIB.','danger')
                return redirect(url_for('attendance_page'))
            existing=c.execute('SELECT * FROM attendance_records WHERE staff_id=? AND work_date=?',(g.user['staff_id'],today)).fetchone()
            if existing:
                flash('Kamu sudah melakukan absensi hari ini.','success')
                return redirect(url_for('attendance_page'))
            c.execute('INSERT INTO attendance_records(staff_id,work_date,clock_in,status,late_minutes,fine,created_at,created_by) VALUES(?,?,?,?,?,?,?,?)',
                      (g.user['staff_id'],today,current.isoformat(),state,late,fine,current.isoformat(),g.user['id']))
            audit(c,'attendance.clock_in',f'{today} {state} late={late}',target_type='attendance',target_id=str(g.user['staff_id']))
            staff_info=c.execute('SELECT s.name,s.cs_name,o.name office_name FROM staff s LEFT JOIN offices o ON o.id=s.office_id WHERE s.id=?',(g.user['staff_id'],)).fetchone()
            c.commit()
            try:
                name=(staff_info['name'] if staff_info else g.user['username'])
                cs=(staff_info['cs_name'] if staff_info else '-') or '-'
                office=(staff_info['office_name'] if staff_info else '-') or '-'
                status_line='✅ TEPAT WAKTU' if state=='TEPAT WAKTU' else f'⚠️ TELAT {late} MENIT'
                tg_send_attendance(f"🕘 <b>ABSENSI STAF</b>\n👤 {escape(str(name))} — {escape(str(cs))}\n🏢 {escape(str(office))}\n📅 {current.strftime('%d-%m-%Y')}\n⏰ {current.strftime('%H:%M:%S')} WIB\n📌 {status_line}\n💸 Denda: Rp{fine:,}")
            except Exception as e:
                print('[attendance-telegram]',e,flush=True)
            flash('Absensi berhasil dicatat.' if state=='TEPAT WAKTU' else f'Absensi tercatat TELAT {late} menit. Denda Rp{fine:,}.','success' if state=='TEPAT WAKTU' else 'danger')
            return redirect(url_for('attendance_page'))
        my=None
        if g.user['staff_id']:
            my=c.execute('SELECT * FROM attendance_records WHERE staff_id=? AND work_date=?',(g.user['staff_id'],today)).fetchone()
        rows=[]
        if g.user['role']!='staff':
            rows=c.execute("""SELECT s.id,s.name,s.cs_name,o.name office_name,a.clock_in,a.status,a.late_minutes,a.fine
                              FROM staff s LEFT JOIN offices o ON o.id=s.office_id
                              LEFT JOIN attendance_records a ON a.staff_id=s.id AND a.work_date=?
                              WHERE s.status='Aktif' ORDER BY s.name COLLATE NOCASE""",(today,)).fetchall()
    state,late,fine=attendance_status_for(now())
    return render_template('attendance.html',today=today,my=my,rows=rows,state=state,late_preview=late,fine_preview=fine)


@app.route('/master-websites',methods=['GET','POST'])
@roles('superadmin','supervisor','leader')
def master_websites_page():
    headers={'X-CRM-Internal':DP_INTERNAL_TOKEN,'X-CRM-User':g.user['username'],'X-CRM-Role':g.user['role']}
    if request.method=='POST':
        try:
            r=requests.post(f"{DP_INTERNAL_URL}/master-websites",headers=headers,data={
                'name':request.form.get('name','').strip(),
                'playerListUrl':request.form.get('playerListUrl','').strip(),
                'cookie':request.form.get('cookie','').strip(),
                'active':'1' if request.form.get('active','1')=='1' else '0'
            },timeout=30,allow_redirects=False)
            if r.status_code>=400: raise RuntimeError(f'DP service HTTP {r.status_code}')
            flash('Website berhasil ditambahkan.','success')
        except Exception as e: flash(f'Website gagal disimpan: {e}','danger')
        return redirect(url_for('master_websites_page'))
    return render_template('master_websites.html',sites=dp_sites())

@app.post('/master-websites/<site_id>/update')
@roles('superadmin','supervisor','leader')
def master_website_update(site_id):
    headers={'X-CRM-Internal':DP_INTERNAL_TOKEN,'X-CRM-User':g.user['username'],'X-CRM-Role':g.user['role']}
    try:
        r=requests.post(f"{DP_INTERNAL_URL}/master-websites/{site_id}/update",headers=headers,data={
            'name':request.form.get('name','').strip(),
            'playerListUrl':request.form.get('playerListUrl','').strip(),
            'cookie':request.form.get('cookie','').strip(),
            'active':'1' if request.form.get('active','1')=='1' else '0'
        },timeout=30,allow_redirects=False)
        if r.status_code>=400: raise RuntimeError(f'DP service HTTP {r.status_code}')
        flash('Website berhasil diperbarui.','success')
    except Exception as e: flash(f'Website gagal diperbarui: {e}','danger')
    return redirect(url_for('master_websites_page'))

@app.post('/master-websites/<site_id>/delete')
@roles('superadmin','supervisor','leader')
def master_website_delete(site_id):
    headers={'X-CRM-Internal':DP_INTERNAL_TOKEN,'X-CRM-User':g.user['username'],'X-CRM-Role':g.user['role']}
    try:
        r=requests.post(f"{DP_INTERNAL_URL}/master-websites/{site_id}/delete",headers=headers,timeout=30,allow_redirects=False)
        if r.status_code>=400: raise RuntimeError(f'DP service HTTP {r.status_code}')
        flash('Website berhasil dihapus.','success')
    except Exception as e: flash(f'Website gagal dihapus: {e}','danger')
    return redirect(url_for('master_websites_page'))

@app.get('/website/<site_id>')
@roles('superadmin','supervisor','leader')
def website_hub(site_id):
    site=next((x for x in dp_sites() if str(x.get('id'))==site_id),None)
    if not site: return ('Website tidak ditemukan.',404)
    return render_template('website_hub.html',site=site)

def _dp_proxy_allowed():
    return bool(g.user and g.user['role'] in ('superadmin','supervisor','leader'))

def _rewrite_dp_html(text):
    reps=[
      ('href="/','href="/dp/'),('action="/','action="/dp/'),
      ("fetch('/","fetch('/dp/"),('src="/','src="/dp/'),
      ('window.location="/','window.location="/dp/'),
      ("window.location='/","window.location='/dp/"),
    ]
    for a,b in reps: text=text.replace(a,b)
    # Link khusus dari modul DP kembali ke Dashboard CRM publik, bukan dashboard internal DP.
    text=text.replace('/dp/__crm_dashboard__','/')
    text=text.replace('/dp/__crm_master_websites__','/master-websites')
    return text

@app.route('/dp/',defaults={'path':''},methods=['GET','POST','PUT','PATCH','DELETE'])
@app.route('/dp/<path:path>',methods=['GET','POST','PUT','PATCH','DELETE'])
@login_required
def dp_proxy(path):
    if not _dp_proxy_allowed(): return ('Akses ditolak',403)
    if not path:
        return redirect(url_for('dashboard'))
    target=f"{DP_INTERNAL_URL}/{path}"
    headers={'X-CRM-Internal':DP_INTERNAL_TOKEN,'X-CRM-User':g.user['username'],'X-CRM-Role':g.user['role']}
    # Forward request content type so Express can parse JSON/form bodies correctly.
    # Without this, /dp/site/<site>/api/jobs receives an empty req.body even though
    # the browser sent a populated UserId list.
    if request.content_type:
        headers['Content-Type']=request.content_type
    if request.headers.get('Accept'):
        headers['Accept']=request.headers.get('Accept')
    data=request.get_data() if request.method in ('POST','PUT','PATCH','DELETE') else None
    try:
        r=requests.request(request.method,target,params=request.args,headers=headers,data=data,timeout=180,allow_redirects=False)
    except Exception as e:
        return Response(f'DP service tidak dapat dihubungi: {escape(str(e))}',status=502,mimetype='text/plain')
    out_headers={}
    for k,v in r.headers.items():
        lk=k.lower()
        if lk in ('content-type','cache-control','content-disposition'): out_headers[k]=v
        elif lk=='location':
            out_headers[k]=('/dp'+v) if v.startswith('/') else v
    body=r.content
    ct=r.headers.get('Content-Type','')
    if 'text/html' in ct:
        try: body=_rewrite_dp_html(r.text).encode(r.encoding or 'utf-8')
        except Exception: pass
    return Response(body,status=r.status_code,headers=out_headers)


init_db();start_bg();print('[startup] SQLite database:',DB_PATH,flush=True); print('[startup] Master login ENV:',ADMIN_USERNAME,flush=True); print('[startup] CRM public port:',os.getenv('PORT','8080'),'| DP internal:',DP_INTERNAL_URL,flush=True)
if __name__=='__main__':app.run(host='0.0.0.0',port=int(os.getenv('PORT','8080')))
