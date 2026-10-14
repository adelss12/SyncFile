"""
الفلاش (الوسيط الناقل) وجلساته.

  <الفلاش>\\SyncFile\\
      carrier.json                  هوية الفلاش (لا نعتمد على حرف القرص E: أو F:)
      sessions\\<id>\\
          session.json              اسم الجلسة ومن أنشأها
          manifest.json             من كتب كل ملف آخر مرة ومتى، وآخر مزامنة
          data\\                     نسخة الملفات نفسها
          data\\.نسخ_احتياطية\\       ما حُذف أو استُبدل على الفلاش
"""

import json
import os
import shutil
import string
import uuid
from datetime import datetime
from pathlib import Path

from .fsutil import filesystem_of, lp

ROOT = "SyncFile"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_json_atomic(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    with open(lp(tmp), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(lp(tmp), lp(path))


def read_json(path: Path, default):
    try:
        with open(lp(path), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


class Carrier:
    def __init__(self, root: Path):
        self.drive = Path(root)
        self.root = self.drive / ROOT

    # ------------------------------------------------------------ الهوية
    @property
    def initialized(self) -> bool:
        return (self.root / "carrier.json").exists()

    def init(self, label=""):
        if not self.initialized:
            write_json_atomic(self.root / "carrier.json",
                              {"id": uuid.uuid4().hex, "created": now_iso(), "label": label})
        return self

    @property
    def id(self):
        return read_json(self.root / "carrier.json", {}).get("id", "")

    def filesystem(self) -> str:
        return filesystem_of(self.drive)

    def free_space(self) -> int:
        try:
            return shutil.disk_usage(str(self.drive)).free
        except OSError:
            return 0

    # ------------------------------------------------------------ الجلسات
    def sessions(self):
        base = self.root / "sessions"
        out = []
        if base.is_dir():
            for d in sorted(base.iterdir()):
                meta = read_json(d / "session.json", None)
                if meta:
                    meta["path"] = d
                    out.append(meta)
        return out

    def session_dir(self, sid) -> Path:
        return self.root / "sessions" / sid

    def data_dir(self, sid) -> Path:
        return self.session_dir(sid) / "data"

    def create_session(self, name, device_name):
        sid = uuid.uuid4().hex[:12]
        write_json_atomic(self.session_dir(sid) / "session.json",
                          {"id": sid, "name": name, "created_by": device_name, "created": now_iso()})
        self.data_dir(sid).mkdir(parents=True, exist_ok=True)
        self.save_manifest(sid, {"files": {}, "last_sync": None})
        return sid

    def session_name(self, sid):
        return read_json(self.session_dir(sid) / "session.json", {}).get("name", sid)

    def manifest(self, sid):
        m = read_json(self.session_dir(sid) / "manifest.json", {})
        m.setdefault("files", {})
        m.setdefault("last_sync", None)
        return m

    def save_manifest(self, sid, m):
        write_json_atomic(self.session_dir(sid) / "manifest.json", m)


def find_carriers():
    """كل الأقراص المتصلة التي فيها مجلد SyncFile (ويندوز). على غير ويندوز تُرجع قائمة فارغة."""
    found = []
    if os.name != "nt":
        return found
    import ctypes
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    system = os.environ.get("SystemDrive", "C:").upper()
    for i, letter in enumerate(string.ascii_uppercase):
        if not mask & (1 << i):
            continue
        drive = f"{letter}:\\"
        kind = ctypes.windll.kernel32.GetDriveTypeW(drive)
        if kind not in (2, 3):              # 2 = قابل للإزالة (فلاش)، 3 = ثابت (قرص USB خارجي)
            continue
        c = Carrier(Path(drive))
        if c.initialized or (kind == 2 and f"{letter}:" != system):
            found.append({"carrier": c, "letter": f"{letter}:", "removable": kind == 2,
                          "initialized": c.initialized})
    return found
