"""
المجلد المخفي ‎.syncfile‎ داخل كل مجلد مُزامَن:

  .syncfile\\
      folder.json            هوية المجلد (تبقى ثابتة حتى لو تغيّر حرف الفلاش)
      cache.json             بصمات الملفات لتسريع الفحص
      pairs\\<هوية الطرف الآخر>.json
                             حالة كل ملف عند آخر مزامنة مع ذلك المجلد (الأساس)

الأساس يُكتب في الطرفين بالرقم نفسه (seq)، فإذا تعذرت الكتابة في أحدهما يُعتمد الأحدث.
"""

import json
import os
import string
import uuid
from datetime import datetime
from pathlib import Path

from .fsutil import hide, lp

META_DIR = ".syncfile"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write_json(path: Path, data):
    os.makedirs(lp(path.parent), exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    with open(lp(tmp), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(lp(tmp), lp(path))


def _read_json(path: Path, default):
    try:
        with open(lp(path), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


class FolderMeta:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.dir = self.root / META_DIR

    def ensure(self):
        if not os.path.isdir(lp(self.dir)):
            os.makedirs(lp(self.dir), exist_ok=True)
            hide(self.dir)
        if not os.path.exists(lp(self.dir / "folder.json")):
            _write_json(self.dir / "folder.json", {"id": uuid.uuid4().hex, "created": now_iso(),
                                                   "name": self.root.name})
        return self

    @property
    def id(self) -> str:
        return _read_json(self.dir / "folder.json", {}).get("id", "")

    def cache(self) -> dict:
        return _read_json(self.dir / "cache.json", {})

    def save_cache(self, files: dict):
        _write_json(self.dir / "cache.json",
                    {k: {"size": v["size"], "mtime": v["mtime"], "hash": v["hash"]} for k, v in files.items()})

    def load_pair(self, other_id: str):
        return _read_json(self.dir / "pairs" / f"{other_id}.json", None)

    def save_pair(self, other_id: str, data: dict):
        _write_json(self.dir / "pairs" / f"{other_id}.json", data)


def read_folder_id(path: Path) -> str:
    return _read_json(Path(path) / META_DIR / "folder.json", {}).get("id", "")


def relocate(path: str, folder_id: str):
    """
    إذا لم يُعثر على المجلد في مساره المحفوظ (مثلاً صار الفلاش F: بدل E:)
    يبحث عن المسار نفسه على بقية الأقراص ويتأكد من هويته.
    """
    if not path or not folder_id:
        return None
    if os.path.isdir(lp(path)) and read_folder_id(path) == folder_id:
        return path
    drive, rest = os.path.splitdrive(path)
    if os.name != "nt" or len(drive) != 2:
        return None
    for letter in string.ascii_uppercase:
        cand = f"{letter}:{rest}"
        try:
            if os.path.isdir(cand) and read_folder_id(cand) == folder_id:
                return cand
        except OSError:
            continue
    return None
