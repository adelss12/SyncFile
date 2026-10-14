"""
أدوات الملفات: البصمة، والنسخ الآمن، والمسارات الطويلة، وتجاهل الملفات المؤقتة.
كل الأسماء تُعامل كنص Unicode (UTF-8)، فلا تتشوه الأسماء العربية أو التشكيل.
"""

import hashlib
import os
import shutil
import unicodedata
from pathlib import Path

QUARANTINE_DIR = ".نسخ_احتياطية"
RESERVED_DIRS = {QUARANTINE_DIR, ".syncfile"}
TMP_SUFFIX = ".syncfile_tmp"

# ملفات ينشئها ويندوز أو أوفيس تلقائياً ولا معنى لمزامنتها
IGNORE_NAMES = {"desktop.ini", "thumbs.db", "ehthumbs.db", ".ds_store"}
IGNORE_PREFIXES = ("~$", ".~lock.", "~wr")
INCOMING_SUFFIX = ".syncfile_in"
IGNORE_SUFFIXES = (".tmp", TMP_SUFFIX, INCOMING_SUFFIX, ".crdownload", ".partial", ".~tmp")

FAT32_LIMIT = 4 * 1024 ** 3 - 1


def is_windows() -> bool:
    return os.name == "nt"


def lp(path) -> str:
    """مسار ويندوز الطويل (\\\\?\\) حتى تعمل المجلدات العربية العميقة فوق 260 حرفاً."""
    p = str(path)
    if not is_windows() or p.startswith("\\\\?\\"):
        return p
    p = os.path.abspath(p)
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def key_of(rel: str) -> str:
    """مفتاح المقارنة: صيغة Unicode موحدة، وبلا تفريق بين الحروف الكبيرة والصغيرة كما في ويندوز."""
    return unicodedata.normalize("NFC", rel.replace("\\", "/")).casefold()


def is_ignored(name: str) -> bool:
    low = name.lower()
    return (low in IGNORE_NAMES or low.startswith(IGNORE_PREFIXES) or low.endswith(IGNORE_SUFFIXES))


def file_hash(path, cancel=None, block=1024 * 1024):
    h = hashlib.sha256()
    with open(lp(path), "rb") as f:
        for chunk in iter(lambda: f.read(block), b""):
            if cancel is not None and cancel.is_set():
                raise InterruptedError
            h.update(chunk)
    return h.hexdigest()


def scan(root: Path, cache: dict, cancel=None, errors=None):
    """
    يفحص المجلد ويُرجع {المفتاح: {"rel", "size", "mtime", "hash"}}.
    البصمة تُحسب فقط للملفات التي تغيّر حجمها أو تاريخها منذ آخر فحص (cache)،
    والتاريخ هنا مجرد مؤشر لإعادة الحساب، لا يُستخدم أبداً لتحديد الأحدث.
    """
    out = {}
    errors = errors if errors is not None else []
    root = Path(root)
    for dirpath, dirs, files in os.walk(lp(root)):
        dirs[:] = [d for d in dirs if d not in RESERVED_DIRS]
        for name in files:
            if is_ignored(name):
                continue
            full = Path(dirpath) / name
            rel = os.path.relpath(str(full), lp(root)).replace("\\", "/")
            rel = unicodedata.normalize("NFC", rel)
            try:
                st = os.stat(str(full))
                k = key_of(rel)
                c = cache.get(k)
                if c and c["size"] == st.st_size and c["mtime"] == st.st_mtime_ns and c.get("hash"):
                    h = c["hash"]
                else:
                    h = file_hash(full, cancel)
                out[k] = {"rel": rel, "size": st.st_size, "mtime": st.st_mtime_ns, "hash": h}
            except InterruptedError:
                raise
            except OSError as e:
                errors.append((rel, f"تعذرت قراءة الملف: {e.strerror or e}"))
    return out


def ensure_parent(path: Path):
    os.makedirs(lp(Path(path).parent), exist_ok=True)


def copy_verified(src: Path, dst: Path, expected_hash: str, cancel=None, on_bytes=None):
    """
    ينسخ إلى اسم مؤقت بجانب الهدف، ثم يتحقق من البصمة، ثم يستبدل الهدف دفعة واحدة.
    إذا انقطع الفلاش أثناء النسخ يبقى الملف الأصلي سليماً ويبقى المؤقت فقط.
    """
    ensure_parent(dst)
    tmp = Path(str(dst) + TMP_SUFFIX)
    h = hashlib.sha256()
    with open(lp(src), "rb") as fi, open(lp(tmp), "wb") as fo:
        for chunk in iter(lambda: fi.read(1024 * 1024), b""):
            if cancel is not None and cancel.is_set():
                fo.close()
                os.remove(lp(tmp))
                raise InterruptedError
            fo.write(chunk)
            h.update(chunk)
            if on_bytes:
                on_bytes(len(chunk))
        fo.flush()
        os.fsync(fo.fileno())
    if expected_hash and h.hexdigest() != expected_hash:
        os.remove(lp(tmp))
        raise IOError("تغيّر الملف أثناء النسخ أو تلف؛ لم يُستبدل شيء")
    try:
        shutil.copystat(lp(src), lp(tmp))
    except OSError:
        pass
    os.replace(lp(tmp), lp(dst))


def quarantine(root: Path, rel: str, stamp: str) -> Path:
    """ينقل الملف إلى .نسخ_احتياطية/<الوقت>/<المسار> بدل حذفه أو الكتابة فوقه."""
    src = Path(root) / rel
    dst = Path(root) / QUARANTINE_DIR / stamp / rel
    ensure_parent(dst)
    if os.path.exists(lp(dst)):
        base, ext = os.path.splitext(str(dst))
        n = 2
        while os.path.exists(lp(f"{base}_{n}{ext}")):
            n += 1
        dst = Path(f"{base}_{n}{ext}")
    os.replace(lp(src), lp(dst))
    hide(Path(root) / QUARANTINE_DIR)
    return dst


def hide(path: Path):
    if is_windows():
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(lp(path), 0x2)  # FILE_ATTRIBUTE_HIDDEN
        except (OSError, AttributeError):
            pass


def remove_empty_parents(root: Path, rel: str):
    """بعد حذف ملف، يحذف المجلدات التي أصبحت فارغة حتى الجذر (rmdir يرفض غير الفارغ)."""
    root = Path(root)
    p = (root / rel).parent
    while p != root and root in p.parents:
        try:
            os.rmdir(lp(p))
        except OSError:
            break
        p = p.parent


def open_in_office(path: Path) -> bool:
    """أوفيس ينشئ ملف قفل ~$ بجانب المستند المفتوح؛ وجوده يعني أن الملف مفتوح الآن."""
    path = Path(path)
    name = path.name
    candidates = {"~$" + name, "~$" + name[2:], "~$" + name[1:]}
    try:
        return any(os.path.exists(lp(path.parent / c)) for c in candidates if len(c) > 2)
    except OSError:
        return False


def filesystem_of(path: Path) -> str:
    """نوع نظام ملفات القرص (FAT32 / exFAT / NTFS) على ويندوز."""
    if not is_windows():
        return ""
    try:
        import ctypes
        root = os.path.splitdrive(os.path.abspath(str(path)))[0] + "\\"
        buf = ctypes.create_unicode_buffer(64)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(root, None, 0, None, None, None, buf, 64)
        return buf.value if ok else ""
    except (OSError, AttributeError):
        return ""


def conflict_name(rel: str, device: str, stamp: str) -> str:
    """تقرير.docx ← تقرير_تعارض_جهاز-البيت_2026-10-15_1430.docx (الاسم العربي يبقى كما هو)."""
    folder, name = os.path.split(rel)
    stem, ext = os.path.splitext(name)
    safe_dev = "".join(c if c.isalnum() or c in "-_" else "-" for c in (device or "جهاز-آخر"))
    new = f"{stem}_تعارض_{safe_dev}_{stamp}{ext}"
    return f"{folder}/{new}" if folder else new
