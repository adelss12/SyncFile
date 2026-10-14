"""
الحالة المحلية لكل جهاز (لا تُحفظ على الفلاش حتى لا تتلف إذا سُحب فجأة):
  • config.json: هوية الجهاز واسمه، والجلسات المربوطة ومجلد كل منها.
  • state.db (SQLite): آخر حالة مزامنة لكل ملف (الأساس)، وذاكرة البصمات لتسريع الفحص.
"""

import json
import os
import socket
import sqlite3
import uuid
from pathlib import Path


def home_dir() -> Path:
    if os.environ.get("SYNCFILE_HOME"):          # للاختبار: محاكاة أكثر من جهاز
        p = Path(os.environ["SYNCFILE_HOME"])
    elif os.name == "nt":
        p = Path(os.environ.get("APPDATA", str(Path.home()))) / "SyncFile"
    else:
        p = Path.home() / ".syncfile"
    p.mkdir(parents=True, exist_ok=True)
    return p


class LocalState:
    def __init__(self, home: Path = None):
        self.home = Path(home) if home else home_dir()
        self.home.mkdir(parents=True, exist_ok=True)
        self.cfg_path = self.home / "config.json"
        self.cfg = self._load_cfg()
        # الواجهة تشغّل المزامنة في خيط خلفي، وعملية واحدة فقط تعمل في كل مرة
        self.db = sqlite3.connect(str(self.home / "state.db"), check_same_thread=False)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS baseline (
                session TEXT, key TEXT, rel TEXT, hash TEXT, size INTEGER,
                synced_at TEXT, counter INTEGER DEFAULT 0, PRIMARY KEY (session, key));
            CREATE TABLE IF NOT EXISTS hash_cache (
                session TEXT, side TEXT, key TEXT, size INTEGER, mtime INTEGER, hash TEXT,
                PRIMARY KEY (session, side, key));
        """)

    # ---------------------------------------------------------------- الإعدادات
    def _load_cfg(self):
        try:
            cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}
        cfg.setdefault("device_id", uuid.uuid4().hex)
        cfg.setdefault("device_name", (os.environ.get("COMPUTERNAME") or socket.gethostname() or "جهاز").strip())
        cfg.setdefault("sessions", {})          # {session_id: {"name", "local_path"}}
        cfg.setdefault("last_carrier", "")
        self.cfg = cfg
        self.save_cfg()
        return cfg

    def save_cfg(self):
        tmp = self.cfg_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.cfg_path)

    @property
    def device_name(self):
        return self.cfg["device_name"]

    @property
    def device_id(self):
        return self.cfg["device_id"]

    def link_session(self, sid, name, local_path):
        self.cfg["sessions"][sid] = {"name": name, "local_path": str(local_path)}
        self.save_cfg()

    def unlink_session(self, sid):
        self.cfg["sessions"].pop(sid, None)
        self.save_cfg()
        self.db.execute("DELETE FROM baseline WHERE session=?", (sid,))
        self.db.execute("DELETE FROM hash_cache WHERE session=?", (sid,))
        self.db.commit()

    def local_path(self, sid):
        s = self.cfg["sessions"].get(sid)
        return Path(s["local_path"]) if s else None

    # ---------------------------------------------------------------- الأساس
    def baseline(self, sid) -> dict:
        rows = self.db.execute("SELECT key, rel, hash, size, counter FROM baseline WHERE session=?", (sid,))
        return {k: {"rel": r, "hash": h, "size": s, "counter": c} for k, r, h, s, c in rows}

    def set_baseline(self, sid, key, rel, hsh, size, when):
        self.db.execute(
            "INSERT INTO baseline(session,key,rel,hash,size,synced_at,counter) VALUES(?,?,?,?,?,?,1) "
            "ON CONFLICT(session,key) DO UPDATE SET rel=excluded.rel, hash=excluded.hash, size=excluded.size, "
            "synced_at=excluded.synced_at, counter=baseline.counter+1",
            (sid, key, rel, hsh, size, when))

    def drop_baseline(self, sid, key):
        self.db.execute("DELETE FROM baseline WHERE session=? AND key=?", (sid, key))

    # --------------------------------------------------------- ذاكرة البصمات
    def cache(self, sid, side) -> dict:
        rows = self.db.execute("SELECT key, size, mtime, hash FROM hash_cache WHERE session=? AND side=?", (sid, side))
        return {k: {"size": s, "mtime": m, "hash": h} for k, s, m, h in rows}

    def save_cache(self, sid, side, files: dict):
        self.db.execute("DELETE FROM hash_cache WHERE session=? AND side=?", (sid, side))
        self.db.executemany(
            "INSERT INTO hash_cache(session,side,key,size,mtime,hash) VALUES(?,?,?,?,?,?)",
            [(sid, side, k, v["size"], v["mtime"], v["hash"]) for k, v in files.items()])
        self.db.commit()

    def commit(self):
        self.db.commit()

    def close(self):
        self.db.close()
