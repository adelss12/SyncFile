"""
محرك المزامنة بين مجلد على هذا الجهاز ونسخته على الفلاش.

القاعدة الذهبية: لا نسأل «أي ملف أحدث تاريخاً؟» لأن ساعة أحد الجهازين قد تكون خاطئة.
بل نقارن كل طرف ببصمة الملف عند آخر مزامنة ناجحة لهذا الجهاز (الأساس):
  • الطرف المطابق للأساس لم يتغير، والطرف المختلف عنه هو الذي عُدّل.
  • إذا اختلف الطرفان كلاهما عن الأساس وعن بعضهما ← تعارض: نحتفظ بالنسختين.
  • لا حذف نهائياً ولا كتابة فوق ملف: القديم يذهب أولاً إلى .نسخ_احتياطية
"""

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .carrier import Carrier, now_iso
from .fsutil import (FAT32_LIMIT, INCOMING_SUFFIX, conflict_name, copy_verified, key_of, lp,
                     open_in_office, quarantine, remove_empty_parents, scan)

EQUAL, PUSH, PULL, DEL_REMOTE, DEL_LOCAL, CONFLICT, FORGET = (
    "equal", "push", "pull", "del_remote", "del_local", "conflict", "forget")

LABELS = {
    EQUAL: "متطابق",
    PUSH: "سيُرسل إلى الفلاش",
    PULL: "سيُحدَّث من الفلاش",
    DEL_REMOTE: "سيُحذف من الفلاش (إلى النسخ الاحتياطية)",
    DEL_LOCAL: "سيُحذف من هذا الجهاز (إلى النسخ الاحتياطية)",
    CONFLICT: "تعارض — ستبقى النسختان",
    FORGET: "حُذف من الطرفين",
}
NEW_LABEL = "ملف جديد"


@dataclass
class Item:
    key: str
    rel: str
    action: str
    local: dict = None
    remote: dict = None
    base: dict = None
    note: str = ""
    blocked: str = ""          # سبب منع التنفيذ (ملف مفتوح، حجم لا يقبله الفلاش...)

    @property
    def is_new(self):
        return self.base is None and self.action in (PUSH, PULL)

    @property
    def label(self):
        if self.blocked:
            return "متوقف"
        return NEW_LABEL + (" ← للفلاش" if self.action == PUSH else " ← لهذا الجهاز") if self.is_new else LABELS[self.action]

    @property
    def size(self):
        src = self.local if self.action in (PUSH, CONFLICT) else self.remote
        return (src or self.local or self.remote or {}).get("size", 0)


@dataclass
class Plan:
    sid: str
    name: str
    local_root: Path
    remote_root: Path
    items: list = field(default_factory=list)
    errors: list = field(default_factory=list)      # أخطاء الفحص
    warnings: list = field(default_factory=list)
    mass_delete: bool = False
    local_files: dict = None
    remote_files: dict = None

    def count(self, action):
        return sum(1 for i in self.items if i.action == action and not i.blocked)

    @property
    def changes(self):
        return [i for i in self.items if i.action not in (EQUAL, FORGET)]

    @property
    def transfer_bytes(self):
        return sum(i.size for i in self.items if i.action in (PUSH, PULL, CONFLICT) and not i.blocked)


@dataclass
class Result:
    done: list = field(default_factory=list)         # (rel, الإجراء)
    conflicts: list = field(default_factory=list)    # (rel, اسم نسخة التعارض)
    errors: list = field(default_factory=list)       # (rel, السبب)
    cancelled: bool = False
    started: str = ""
    finished: str = ""
    bytes: int = 0


class SyncError(Exception):
    pass


# ============================================================================
# المعاينة: لا تلمس أي ملف
# ============================================================================

def build_plan(state, carrier: Carrier, sid: str, cancel=None) -> Plan:
    local_root = state.local_path(sid)
    remote_root = carrier.data_dir(sid)
    plan = Plan(sid=sid, name=carrier.session_name(sid), local_root=local_root, remote_root=remote_root)

    if local_root is None:
        raise SyncError("هذه الجلسة غير مربوطة بمجلد على هذا الجهاز.")
    if not os.path.isdir(lp(local_root)):
        # بدون هذا الحاجز سيبدو كل شيء «محذوفاً» ويُحذف من الفلاش
        raise SyncError(f"المجلد غير موجود على هذا الجهاز:\n{local_root}\n"
                        "تأكد أن القرص موصول أو أن المجلد لم يُنقل، ثم أعد المحاولة.")
    if not os.path.isdir(lp(remote_root)):
        raise SyncError("مجلد الجلسة غير موجود على الفلاش.")

    L = scan(local_root, state.cache(sid, "local"), cancel, plan.errors)
    R = scan(remote_root, state.cache(sid, "carrier"), cancel, plan.errors)
    B = state.baseline(sid)
    plan.local_files, plan.remote_files = L, R
    manifest = carrier.manifest(sid)

    fs = carrier.filesystem().upper()
    for k in sorted(set(L) | set(R) | set(B)):
        l, r, b = L.get(k), R.get(k), B.get(k)
        lh, rh, bh = (l or {}).get("hash"), (r or {}).get("hash"), (b or {}).get("hash")
        rel = (l or r or b)["rel"]
        note = ""
        if lh == rh:
            action = EQUAL if lh else FORGET
        elif lh == bh:                       # هنا لم يتغير ← التعديل من الجهاز الآخر
            action = PULL if rh else DEL_LOCAL
        elif rh == bh:                       # الفلاش لم يتغير ← التعديل من هنا
            action = PUSH if lh else DEL_REMOTE
        elif lh and rh:
            action = CONFLICT
            who = manifest["files"].get(k, {}).get("by") or "الجهاز الآخر"
            note = f"عُدّل هنا وعُدّل على {who}"
        elif lh:
            action, note = PUSH, "حُذف من الفلاش وعُدّل هنا — يُبقى المعدّل"
        else:
            action, note = PULL, "حُذف هنا وعُدّل على الجهاز الآخر — يُبقى المعدّل"

        item = Item(key=k, rel=rel, action=action, local=l, remote=r, base=b, note=note)
        if action in (PULL, DEL_LOCAL, CONFLICT) and l and open_in_office(local_root / l["rel"]):
            item.blocked = "الملف مفتوح في أوفيس على هذا الجهاز — أغلقه ثم أعد المزامنة"
        if action in (PUSH, CONFLICT) and l and fs == "FAT32" and l["size"] > FAT32_LIMIT:
            item.blocked = "حجمه أكبر من 4 جيجابايت والفلاش بنظام FAT32 (هيّئه بنظام exFAT)"
        plan.items.append(item)

    # فرامل الحذف الجماعي
    for side, act in (("الفلاش", DEL_REMOTE), ("هذا الجهاز", DEL_LOCAL)):
        n = plan.count(act)
        if B and n >= 20 and n > len(B) * 0.5:
            plan.mass_delete = True
            plan.warnings.append(f"⚠️ سيُحذف {n} ملف من {side} (أكثر من نصف الملفات). تأكد أن هذا مقصود.")

    # ساعة هذا الجهاز مقارنة بآخر جهاز زامن
    last = manifest.get("last_sync") or {}
    if last.get("at") and last.get("device_id") != state.device_id:
        try:
            other = datetime.fromisoformat(last["at"])
            diff = (datetime.now().astimezone() - other).total_seconds()
            if diff < -600:
                plan.warnings.append(
                    f"🕒 ساعة هذا الجهاز متأخرة عن جهاز «{last.get('device', '؟')}» بنحو {human_delta(-diff)}. "
                    "المزامنة لا تتأثر لأنها تعتمد على البصمة، لكن يُنصح بتصحيح الساعة.")
        except (ValueError, TypeError):
            pass

    if plan.transfer_bytes and carrier.free_space() and plan.transfer_bytes > carrier.free_space():
        plan.warnings.append("💾 المساحة المتاحة على الفلاش قد لا تكفي لكل الملفات.")
    if fs == "FAT32":
        plan.warnings.append("ℹ️ الفلاش بنظام FAT32 ولا يقبل ملفاً أكبر من 4 جيجابايت.")
    return plan


def human_delta(sec: float) -> str:
    if sec < 3600:
        return f"{int(sec // 60)} دقيقة"
    if sec < 86400:
        return f"{sec / 3600:.1f} ساعة"
    return f"{sec / 86400:.1f} يوم"


# ============================================================================
# التنفيذ
# ============================================================================

def _replace(src_root, src_rel, src_hash, dst_root, dst_rel, old_rel, stamp, cancel, on_bytes):
    """ينسخ ملفاً فوق آخر بأمان: النسخة الجديدة تُكتب وتُتحقق أولاً، والقديمة إلى النسخ الاحتياطية."""
    src = Path(src_root) / src_rel
    dst = Path(dst_root) / dst_rel
    incoming = Path(str(dst) + INCOMING_SUFFIX)
    copy_verified(src, incoming, src_hash, cancel, on_bytes)
    if old_rel and os.path.exists(lp(Path(dst_root) / old_rel)):
        quarantine(dst_root, old_rel, stamp)
    os.replace(lp(incoming), lp(dst))


def execute(plan: Plan, state, carrier: Carrier, cancel=None, progress=None) -> Result:
    """progress(نُقل_بايت, الإجمالي, المسار_الحالي)"""
    res = Result(started=now_iso())
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    cstamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    me = state.device_name
    sid, L_root, R_root = plan.sid, plan.local_root, plan.remote_root
    manifest = carrier.manifest(sid)
    files = manifest["files"]
    total = max(plan.transfer_bytes, 1)
    moved = [0]

    def on_bytes(n):
        moved[0] += n
        if progress:
            progress(moved[0], total, current[0])

    current = [""]
    when = now_iso()
    for it in plan.items:
        if cancel is not None and cancel.is_set():
            res.cancelled = True
            break
        if it.blocked:
            res.errors.append((it.rel, it.blocked))
            continue
        current[0] = it.rel
        l, r = it.local, it.remote
        try:
            if it.action == EQUAL:
                state.set_baseline(sid, it.key, l["rel"], l["hash"], l["size"], when)
            elif it.action == FORGET:
                state.drop_baseline(sid, it.key)
                files.pop(it.key, None)
            elif it.action == PUSH:
                _replace(L_root, l["rel"], l["hash"], R_root, l["rel"], r and r["rel"], stamp, cancel, on_bytes)
                files[it.key] = {"rel": l["rel"], "hash": l["hash"], "by": me, "at": when}
                state.set_baseline(sid, it.key, l["rel"], l["hash"], l["size"], when)
                res.done.append((it.rel, "أُرسل إلى الفلاش"))
            elif it.action == PULL:
                _replace(R_root, r["rel"], r["hash"], L_root, r["rel"], l and l["rel"], stamp, cancel, on_bytes)
                state.set_baseline(sid, it.key, r["rel"], r["hash"], r["size"], when)
                res.done.append((it.rel, "حُدّث من الفلاش"))
            elif it.action == DEL_REMOTE:
                quarantine(R_root, r["rel"], stamp)
                remove_empty_parents(R_root, r["rel"])
                files.pop(it.key, None)
                state.drop_baseline(sid, it.key)
                res.done.append((it.rel, "حُذف من الفلاش"))
            elif it.action == DEL_LOCAL:
                quarantine(L_root, l["rel"], stamp)
                remove_empty_parents(L_root, l["rel"])
                state.drop_baseline(sid, it.key)
                res.done.append((it.rel, "حُذف من هذا الجهاز"))
            elif it.action == CONFLICT:
                other = files.get(it.key, {}).get("by") or "الجهاز-الآخر"
                cname = conflict_name(l["rel"], other, cstamp)
                ck = key_of(cname)
                # 1) نسخة الطرف الآخر تُحفظ باسم التعارض على الجهاز والفلاش
                copy_verified(Path(R_root) / r["rel"], Path(L_root) / cname, r["hash"], cancel, on_bytes)
                copy_verified(Path(R_root) / r["rel"], Path(R_root) / cname, r["hash"], cancel)
                # 2) نسخة هذا الجهاز تبقى بالاسم الأصلي في الطرفين
                _replace(L_root, l["rel"], l["hash"], R_root, l["rel"], r["rel"], stamp, cancel, None)
                files[it.key] = {"rel": l["rel"], "hash": l["hash"], "by": me, "at": when}
                files[ck] = {"rel": cname, "hash": r["hash"], "by": other, "at": when}
                state.set_baseline(sid, it.key, l["rel"], l["hash"], l["size"], when)
                state.set_baseline(sid, ck, cname, r["hash"], r["size"], when)
                res.conflicts.append((it.rel, cname))
                res.done.append((it.rel, f"تعارض: حُفظت نسخة {other} باسم {Path(cname).name}"))
        except InterruptedError:
            res.cancelled = True
            break
        except Exception as e:  # ملف مقفل أو صلاحيات: نسجل ونكمل، ولا نحدّث أساسه
            res.errors.append((it.rel, _friendly(e)))
        state.commit()

    manifest["last_sync"] = {"device": me, "device_id": state.device_id, "at": now_iso()}
    carrier.save_manifest(sid, manifest)
    state.save_cache(sid, "local", plan.local_files or {})
    state.save_cache(sid, "carrier", plan.remote_files or {})
    res.bytes = moved[0]
    res.finished = now_iso()
    return res


def _friendly(e: Exception) -> str:
    if isinstance(e, PermissionError):
        return "الملف مقفل أو مفتوح في برنامج آخر، أو لا توجد صلاحية — أغلقه وأعد المزامنة"
    if isinstance(e, FileNotFoundError):
        return "اختفى الملف أثناء المزامنة (ربما نُقل أو سُحب الفلاش)"
    if isinstance(e, OSError) and getattr(e, "errno", None) == 28:
        return "لا توجد مساحة كافية"
    return str(e)


# ============================================================================
# الجلسات
# ============================================================================

def create_session(state, carrier: Carrier, local_folder: Path) -> str:
    """زر (+): ينشئ جلسة على الفلاش باسم المجلد ويربطها بهذا الجهاز."""
    local_folder = Path(local_folder)
    if not local_folder.is_dir():
        raise SyncError("اختر مجلداً موجوداً.")
    for s in carrier.sessions():
        if state.local_path(s["id"]) and Path(state.local_path(s["id"])).resolve() == local_folder.resolve():
            raise SyncError("هذا المجلد مربوط بجلسة موجودة أصلاً.")
    carrier.init()
    sid = carrier.create_session(local_folder.name, state.device_name)
    state.link_session(sid, local_folder.name, local_folder)
    return sid


def join_session(state, carrier: Carrier, sid: str, parent: Path) -> Path:
    """جلسة موجودة على الفلاش وجديدة على هذا الجهاز: ينشئ مجلداً بالاسم نفسه ويربطه."""
    name = carrier.session_name(sid)
    target = Path(parent) / name
    os.makedirs(lp(target), exist_ok=True)
    state.link_session(sid, name, target)
    return target
