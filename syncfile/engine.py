"""
محرك المزامنة بين مجلدين (المصدر والهدف) في أي مكان: فلاش، قرص آخر، أو القسم نفسه.
بعد المزامنة يصبح المجلدان متطابقين تماماً، بما في ذلك المجلدات الفرعية.

كيف يعرف البرنامج أي ملف هو المعدَّل؟
  1) إذا سبقت مزامنة بين المجلدين: يقارن كل طرف ببصمة الملف عند آخر مزامنة (محفوظة في ‎.syncfile‎).
     الطرف المطابق للأساس لم يتغير، والمختلف هو المعدَّل — مهما كانت ساعة الجهاز.
  2) في أول مزامنة (لا سجل سابق): يعتمد على تاريخ التعديل الأحدث، ويتحقق من الحجم:
     إذا كان الأحدث هو الأكبر (أو الحجم نفسه) يُختار تلقائياً،
     وإذا كان الأحدث تاريخاً أصغر حجماً ← «التوقيت غير متوافق» ويُعرض الملفان ليختار المستخدم.
لا حذف نهائياً ولا كتابة فوق ملف: القديم ينتقل أولاً إلى ‎.نسخ_احتياطية‎ في الطرف نفسه.
"""

import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .fsutil import (FAT32_LIMIT, INCOMING_SUFFIX, copy_verified, filesystem_of, key_of, lp, open_in_office,
                     quarantine, remove_empty_parents, scan)
from .meta import FolderMeta, now_iso

EQUAL, A2B, B2A, DEL_A, DEL_B, CONFLICT, DECIDE, FORGET, SKIP = (
    "equal", "a2b", "b2a", "del_a", "del_b", "conflict", "decide", "forget", "skip")

# أوضاع المهمة (مثل GoodSync)
TWO_WAY, A_TO_B, B_TO_A = "two_way", "a_to_b", "b_to_a"
MODES = {TWO_WAY: "مزامنة في الاتجاهين", A_TO_B: "نسخ احتياطي: من المصدر إلى الهدف فقط",
         B_TO_A: "نسخ احتياطي: من الهدف إلى المصدر فقط"}

# القرار عند الملفات التي تحتاج اختيار المستخدم (أو تغيير اتجاه أي ملف بالزر الأيمن)
CHOOSE_A, CHOOSE_B, KEEP_BOTH, NO_COPY = "a", "b", "both", "skip"

SIDE = {"a": "المصدر", "b": "الهدف"}
TIME_TOLERANCE = 2.0          # ثانيتان: دقة التاريخ في أنظمة FAT


@dataclass
class Item:
    key: str
    rel: str
    action: str
    a: dict = None
    b: dict = None
    base: dict = None
    note: str = ""
    blocked: str = ""
    suggest: str = ""          # الاقتراح الافتراضي لقرار المستخدم
    reason: str = ""           # new / modified / deleted / conflict / timing

    @property
    def needs_choice(self):
        return self.action in (CONFLICT, DECIDE)

    @property
    def label(self):
        if self.blocked:
            return "متوقف"
        if self.action == EQUAL:
            return "متطابق"
        if self.action in (A2B, B2A):
            to = "الهدف" if self.action == A2B else "المصدر"
            return ("ملف جديد ← " if self.reason == "new" else "معدّل ← ") + to
        if self.action == DEL_A:
            return "سيُحذف من المصدر"
        if self.action == DEL_B:
            return "سيُحذف من الهدف"
        if self.action == CONFLICT:
            return "تعارض — يحتاج قرارك"
        if self.action == DECIDE:
            return "التوقيت غير متوافق — يحتاج قرارك"
        if self.action == SKIP:
            return "لن يُنسخ"
        return "حُذف من الطرفين"

    @property
    def transfer_size(self):
        if self.action == A2B:
            return self.a["size"]
        if self.action == B2A:
            return self.b["size"]
        if self.needs_choice:
            return max((self.a or {}).get("size", 0), (self.b or {}).get("size", 0))
        return 0


@dataclass
class Plan:
    a_root: Path
    b_root: Path
    items: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    mass_delete: bool = False
    first_sync: bool = True
    a_files: dict = None
    b_files: dict = None
    base: dict = None
    seq: int = 0

    def count(self, *actions):
        return sum(1 for i in self.items if i.action in actions and not i.blocked)

    def count_reason(self, reason):
        return sum(1 for i in self.items if i.reason == reason and i.action in (A2B, B2A) and not i.blocked)

    @property
    def changes(self):
        return [i for i in self.items if i.action not in (EQUAL, FORGET, SKIP)]

    @property
    def decisions(self):
        return [i for i in self.items if i.needs_choice and not i.blocked]

    @property
    def transfer_bytes(self):
        return sum(i.transfer_size for i in self.items if not i.blocked)


@dataclass
class Result:
    done: list = field(default_factory=list)
    kept_both: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    cancelled: bool = False
    started: str = ""
    finished: str = ""
    bytes: int = 0


class SyncError(Exception):
    pass


def fmt_time(mtime_ns) -> str:
    return datetime.fromtimestamp(mtime_ns / 1e9).strftime("%Y-%m-%d %H:%M")


def _inside(p: Path, q: Path) -> bool:
    try:
        p, q = Path(os.path.abspath(p)), Path(os.path.abspath(q))
        return p == q or q in p.parents
    except (OSError, ValueError):
        return False


def check_folders(a: Path, b: Path):
    for p, side in ((a, "المصدر"), (b, "الهدف")):
        if not p or not os.path.isdir(lp(p)):
            raise SyncError(f"مجلد {side} غير موجود:\n{p}\n"
                            "تأكد أن الفلاش أو القرص موصول وأن المجلد لم يُنقل، ثم أعد المحاولة.")
    if _inside(a, b) or _inside(b, a):
        raise SyncError("لا يمكن أن يكون أحد المجلدين داخل الآخر؛ اختر مجلدين منفصلين.")


# ============================================================================
# المعاينة: لا تلمس أي ملف
# ============================================================================

def load_base(ma: FolderMeta, mb: FolderMeta):
    pa, pb = ma.load_pair(mb.id), mb.load_pair(ma.id)
    cands = [p for p in (pa, pb) if p]
    if not cands:
        return None
    return max(cands, key=lambda p: p.get("seq", 0))


def build_plan(a_root, b_root, cancel=None, mode=TWO_WAY, excludes=()) -> Plan:
    a_root, b_root = Path(a_root), Path(b_root)
    check_folders(a_root, b_root)
    ma, mb = FolderMeta(a_root), FolderMeta(b_root)
    plan = Plan(a_root=a_root, b_root=b_root)

    A = scan(a_root, ma.cache(), cancel, plan.errors, excludes)
    B = scan(b_root, mb.cache(), cancel, plan.errors, excludes)
    base_doc = load_base(ma, mb) if ma.id and mb.id else None
    base = (base_doc or {}).get("files", {})
    plan.first_sync = base_doc is None
    plan.seq = (base_doc or {}).get("seq", 0)
    plan.a_files, plan.b_files, plan.base = A, B, base

    timing = 0
    future = time.time() + 86400
    fs_b, fs_a = filesystem_of(b_root).upper(), filesystem_of(a_root).upper()
    for k in sorted(set(A) | set(B) | set(base)):
        a, b, s = A.get(k), B.get(k), base.get(k)
        ah, bh, sh = (a or {}).get("hash"), (b or {}).get("hash"), (s or {}).get("hash")
        rel = (a or b or s)["rel"]
        it = Item(key=k, rel=rel, action=EQUAL, a=a, b=b, base=s)

        if ah == bh:
            it.action = EQUAL if ah else FORGET
        elif s is not None:                              # يوجد سجل سابق لهذا الملف
            if ah == sh:
                it.action, it.reason = (B2A, "modified") if bh else (DEL_A, "deleted")
            elif bh == sh:
                it.action, it.reason = (A2B, "modified") if ah else (DEL_B, "deleted")
            elif ah and bh:
                it.action, it.reason, it.suggest = CONFLICT, "conflict", KEEP_BOTH
                it.note = "عُدّل في المصدر وفي الهدف منذ آخر مزامنة"
            elif ah:
                it.action, it.reason, it.note = A2B, "modified", "حُذف من الهدف وعُدّل في المصدر — يُبقى المعدّل"
            else:
                it.action, it.reason, it.note = B2A, "modified", "حُذف من المصدر وعُدّل في الهدف — يُبقى المعدّل"
        elif ah and not bh:
            it.action, it.reason = A2B, "new"
        elif bh and not ah:
            it.action, it.reason = B2A, "new"
        else:                                            # موجود في الطرفين ومختلف ولا سجل له
            classify_first_time(it)
            if it.action == DECIDE:
                timing += 1

        apply_mode(it, mode)
        if it.action in (B2A, DEL_A) or (it.needs_choice and a):
            if a and open_in_office(a_root / a["rel"]):
                it.blocked = "الملف مفتوح في أوفيس في المصدر — أغلقه ثم أعد المزامنة"
        if it.action in (A2B, DEL_B) or (it.needs_choice and b):
            if b and open_in_office(b_root / b["rel"]):
                it.blocked = "الملف مفتوح في أوفيس في الهدف — أغلقه ثم أعد المزامنة"
        if it.action == A2B and fs_b == "FAT32" and a["size"] > FAT32_LIMIT:
            it.blocked = "أكبر من 4 جيجابايت والهدف بنظام FAT32 (هيّئه بنظام exFAT)"
        if it.action == B2A and fs_a == "FAT32" and b["size"] > FAT32_LIMIT:
            it.blocked = "أكبر من 4 جيجابايت والمصدر بنظام FAT32"
        plan.items.append(it)

    if timing:
        plan.warnings.append(f"🕒 التوقيت غير متوافق في {timing} ملف: الأحدث تاريخاً هو الأصغر حجماً. "
                             "قد تكون ساعة أحد الجهازين خاطئة — ستختار النسخة الصحيحة بنفسك.")
    fut = sum(1 for d in (A, B) for v in d.values() if v["mtime"] / 1e9 > future)
    if fut:
        plan.warnings.append(f"🕒 {fut} ملف تاريخه في المستقبل — ساعة الجهاز الذي عُدّل عليه غير صحيحة.")
    for side, act in (("المصدر", DEL_A), ("الهدف", DEL_B)):
        n = plan.count(act)
        if base and n >= 20 and n > len(base) * 0.5:
            plan.mass_delete = True
            plan.warnings.append(f"⚠️ سيُحذف {n} ملف من {side} (أكثر من نصف الملفات). تأكد أن هذا مقصود.")
    if "FAT32" in (fs_a, fs_b):
        plan.warnings.append("ℹ️ أحد المجلدين على قرص بنظام FAT32 ولا يقبل ملفاً أكبر من 4 جيجابايت.")
    return plan


def apply_mode(it: Item, mode: str):
    """النسخ الاحتياطي باتجاه واحد: لا يُنقل أي تغيير من الطرف الآخر ولا يُحذف منه شيء."""
    if mode == A_TO_B:
        if it.action in (B2A, DEL_A):
            it.action, it.note = SKIP, "تغيّر في الهدف — وضع النسخ الاحتياطي لا ينقله إلى المصدر"
        elif it.needs_choice:
            it.suggest = CHOOSE_A
    elif mode == B_TO_A:
        if it.action in (A2B, DEL_B):
            it.action, it.note = SKIP, "تغيّر في المصدر — وضع النسخ الاحتياطي لا ينقله إلى الهدف"
        elif it.needs_choice:
            it.suggest = CHOOSE_B


def classify_first_time(it: Item):
    """أول مزامنة لملف مختلف في الطرفين: التاريخ الأحدث، مع التحقق من الحجم."""
    a, b = it.a, it.b
    dt = (a["mtime"] - b["mtime"]) / 1e9
    newer = "a" if dt > 0 else "b"
    larger = "a" if a["size"] > b["size"] else ("b" if b["size"] > a["size"] else None)
    if abs(dt) <= TIME_TOLERANCE:
        it.action, it.reason = DECIDE, "timing"
        it.suggest = larger or CHOOSE_A
        it.note = "نفس التاريخ تقريباً والمحتوى مختلف"
    elif larger is None or larger == newer:
        it.action, it.reason = (A2B if newer == "a" else B2A), "modified"
        it.note = f"الأحدث في {SIDE[newer]} ({fmt_time(it.a['mtime'] if newer == 'a' else it.b['mtime'])})"
    else:
        it.action, it.reason, it.suggest = DECIDE, "timing", larger
        it.note = f"الأحدث تاريخاً في {SIDE[newer]} لكنه أصغر حجماً"


# ============================================================================
# التنفيذ
# ============================================================================

def _replace(src_root, src_rel, src_hash, dst_root, dst_rel, old_rel, stamp, cancel, on_bytes):
    """النسخة الجديدة تُكتب وتُتحقق أولاً، ثم ينتقل القديم إلى ‎.نسخ_احتياطية‎، ثم تحل الجديدة مكانه."""
    src, dst = Path(src_root) / src_rel, Path(dst_root) / dst_rel
    incoming = Path(str(dst) + INCOMING_SUFFIX)
    copy_verified(src, incoming, src_hash, cancel, on_bytes)
    if old_rel and os.path.exists(lp(Path(dst_root) / old_rel)):
        quarantine(dst_root, old_rel, stamp)
    os.replace(lp(incoming), lp(dst))


def keep_both_name(rel: str, side: str, stamp: str) -> str:
    folder, name = os.path.split(rel)
    stem, ext = os.path.splitext(name)
    new = f"{stem}_نسخة_{SIDE[side]}_{stamp}{ext}"
    return f"{folder}/{new}" if folder else new


def execute(plan: Plan, choices: dict = None, device: str = "", cancel=None, progress=None) -> Result:
    """choices: {المفتاح: "a" أو "b" أو "both"} للملفات التي تحتاج قراراً."""
    choices = choices or {}
    res = Result(started=now_iso())
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    cstamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    A, B = plan.a_root, plan.b_root
    ma, mb = FolderMeta(A).ensure(), FolderMeta(B).ensure()
    new_base = {}
    total = max(plan.transfer_bytes, 1)
    moved, current = [0], [""]

    def on_bytes(n):
        moved[0] += n
        if progress:
            progress(moved[0], total, current[0])

    def remember(key, f):
        new_base[key] = {"rel": f["rel"], "hash": f["hash"], "size": f["size"]}

    for it in plan.items:
        if cancel is not None and cancel.is_set():
            res.cancelled = True
        if res.cancelled:
            if it.base:                                   # ما لم يُنفذ يبقى أساسه القديم
                new_base[it.key] = it.base
            continue
        current[0] = it.rel
        a, b = it.a, it.b
        if it.blocked:
            res.errors.append((it.rel, it.blocked))
            if it.base:
                new_base[it.key] = it.base
            continue
        try:
            act = it.action
            choice = choices.get(it.key)
            if it.needs_choice and choice is None:
                raise SyncError("لم يُحدَّد أي النسختين تريد — لم يُنسخ")
            if choice is not None:                         # قرار المستخدم أو تغيير الاتجاه بالزر الأيمن
                act = resolve_choice(it, choice)
            if act == EQUAL:
                remember(it.key, a)
            elif act == FORGET:
                pass
            elif act == SKIP:
                if it.base:
                    new_base[it.key] = it.base
            elif act == A2B:
                _replace(A, a["rel"], a["hash"], B, a["rel"], b and b["rel"], stamp, cancel, on_bytes)
                remember(it.key, a)
                res.done.append((it.rel, "نُسخ من المصدر إلى الهدف"))
            elif act == B2A:
                _replace(B, b["rel"], b["hash"], A, b["rel"], a and a["rel"], stamp, cancel, on_bytes)
                remember(it.key, b)
                res.done.append((it.rel, "نُسخ من الهدف إلى المصدر"))
            elif act == DEL_A:
                quarantine(A, a["rel"], stamp)
                remove_empty_parents(A, a["rel"])
                res.done.append((it.rel, "حُذف من المصدر (إلى النسخ الاحتياطية)"))
            elif act == DEL_B:
                quarantine(B, b["rel"], stamp)
                remove_empty_parents(B, b["rel"])
                res.done.append((it.rel, "حُذف من الهدف (إلى النسخ الاحتياطية)"))
            elif act == "both":
                # نسخة المصدر تبقى بالاسم الأصلي، ونسخة الهدف تُحفظ باسم واضح في الطرفين
                other = keep_both_name(b["rel"], "b", cstamp)
                copy_verified(B / b["rel"], A / other, b["hash"], cancel, on_bytes)
                copy_verified(B / b["rel"], B / other, b["hash"], cancel)
                _replace(A, a["rel"], a["hash"], B, a["rel"], b["rel"], stamp, cancel, None)
                remember(it.key, a)
                remember(key_of(other), {"rel": other, "hash": b["hash"], "size": b["size"]})
                res.kept_both.append((it.rel, other))
                res.done.append((it.rel, f"حُفظت النسختان (نسخة الهدف باسم {Path(other).name})"))
        except InterruptedError:
            res.cancelled = True
            if it.base:
                new_base[it.key] = it.base
        except Exception as e:
            res.errors.append((it.rel, _friendly(e)))
            if it.base:                                   # الفشل لا يغيّر الأساس فيُعاد في المرة القادمة
                new_base[it.key] = it.base

    doc = {"seq": plan.seq + 1, "sync_id": uuid.uuid4().hex, "at": now_iso(), "device": device,
           "a": str(A), "b": str(B), "files": new_base}
    for m, other in ((ma, mb), (mb, ma)):
        try:
            m.save_pair(other.id, doc)
        except OSError as e:
            res.errors.append((str(m.root), f"تعذر حفظ سجل المزامنة: {e}"))
    for m, files in ((ma, plan.a_files), (mb, plan.b_files)):
        try:
            m.save_cache(files or {})
        except OSError:
            pass
    res.bytes = moved[0]
    res.finished = now_iso()
    return res


def resolve_choice(it: Item, choice: str) -> str:
    """يحوّل اختيار المستخدم إلى إجراء، مع مراعاة أن يكون الملف موجوداً في الطرف المنسوخ منه."""
    if choice == NO_COPY:
        return SKIP
    if choice == KEEP_BOTH:
        return "both" if (it.a and it.b) else (A2B if it.a else B2A)
    if choice == CHOOSE_A:
        return A2B if it.a else DEL_B          # المصدر بلا الملف ← اختيار المصدر يعني حذفه من الهدف
    if choice == CHOOSE_B:
        return B2A if it.b else DEL_A
    raise SyncError("اختيار غير معروف")


def choices_for(it: Item):
    """الخيارات المتاحة لملف في القائمة (للزر الأيمن ونافذة القرار)."""
    out = []
    if it.a:
        out.append((CHOOSE_A, "نسخة المصدر ← إلى الهدف"))
    else:
        out.append((CHOOSE_A, "حذفه من الهدف (كما في المصدر)"))
    if it.b:
        out.append((CHOOSE_B, "نسخة الهدف ← إلى المصدر"))
    else:
        out.append((CHOOSE_B, "حذفه من المصدر (كما في الهدف)"))
    if it.a and it.b:
        out.append((KEEP_BOTH, "الاحتفاظ بالنسختين"))
    out.append((NO_COPY, "لا تنسخ الآن"))
    return out


def _friendly(e: Exception) -> str:
    if isinstance(e, SyncError):
        return str(e)
    if isinstance(e, PermissionError):
        return "الملف مقفل أو مفتوح في برنامج آخر، أو لا توجد صلاحية — أغلقه وأعد المزامنة"
    if isinstance(e, FileNotFoundError):
        return "اختفى الملف أثناء المزامنة (ربما نُقل أو فُصل الفلاش)"
    if isinstance(e, OSError) and getattr(e, "errno", None) == 28:
        return "لا توجد مساحة كافية"
    return str(e)
