"""
إعدادات البرنامج على هذا الجهاز: اسم الجهاز، والمزامنات المحفوظة (كل مزامنة = مجلدان).
حالة الملفات نفسها لا تُحفظ هنا بل في المجلد المخفي ‎.syncfile‎ داخل كل مجلد.
"""

import json
import os
import socket
import uuid
from pathlib import Path


def home_dir() -> Path:
    if os.environ.get("SYNCFILE_HOME"):
        p = Path(os.environ["SYNCFILE_HOME"])
    elif os.name == "nt":
        p = Path(os.environ.get("APPDATA", str(Path.home()))) / "SyncFile"
    else:
        p = Path.home() / ".syncfile_app"
    p.mkdir(parents=True, exist_ok=True)
    return p


class AppConfig:
    def __init__(self, home: Path = None):
        self.home = Path(home) if home else home_dir()
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "config.json"
        try:
            self.cfg = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.cfg = {}
        self.cfg.setdefault("device_name", (os.environ.get("COMPUTERNAME") or socket.gethostname() or "جهاز").strip())
        self.cfg.setdefault("pairs", [])   # [{"id", "name", "a", "b", "a_id", "b_id", "mode", "excludes", "auto", ...}]
        for p in self.cfg["pairs"]:
            p.setdefault("mode", "two_way"); p.setdefault("excludes", ""); p.setdefault("auto", False)
            p.setdefault("keep_days", 30); p.setdefault("last", "")
        self.save()

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    @property
    def device_name(self):
        return self.cfg["device_name"]

    @property
    def pairs(self):
        return self.cfg["pairs"]

    def add_pair(self, a, b, name=None, **extra) -> dict:
        p = {"id": uuid.uuid4().hex[:10], "name": name or Path(a).name, "a": str(a), "b": str(b), "a_id": "", "b_id": "",
             "mode": "two_way", "excludes": "", "auto": False, "keep_days": 30, "last": ""}
        p.update(extra)
        self.cfg["pairs"].append(p)
        self.save()
        return p

    def get(self, pid):
        return next((p for p in self.pairs if p["id"] == pid), None)

    def update(self, pid, **kw):
        p = self.get(pid)
        if p:
            p.update(kw)
            self.save()
        return p

    def remove(self, pid):
        self.cfg["pairs"] = [p for p in self.pairs if p["id"] != pid]
        self.save()
