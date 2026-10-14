"""
اختبار محرك SyncFile 2.0 على ملفات حقيقية بأسماء عربية.
التشغيل:  python -m unittest discover -s tests -v
"""

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from syncfile import engine                                                     # noqa: E402
from syncfile.engine import (A2B, B2A, CHOOSE_A, CHOOSE_B, CONFLICT, DECIDE, DEL_A, DEL_B, EQUAL,  # noqa: E402
                             KEEP_BOTH, SyncError, build_plan, execute)
from syncfile.fsutil import QUARANTINE_DIR                                       # noqa: E402
from syncfile.meta import META_DIR, relocate, read_folder_id                     # noqa: E402


def write(p: Path, text: str, age_days: float = 0):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    if age_days:
        t = time.time() - age_days * 86400
        os.utime(p, (t, t))


def read(p: Path):
    return p.read_text(encoding="utf-8")


def actions(plan):
    return {i.rel: i.action for i in plan.items}


def files(root: Path):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*")
                  if p.is_file() and META_DIR not in p.parts and QUARANTINE_DIR not in p.parts)


def sync(a, b, choices=None):
    plan = build_plan(a, b)
    res = execute(plan, choices or {}, "اختبار")
    assert not res.errors, res.errors
    return plan, res


class UserScenario(unittest.TestCase):
    """سيناريو المستخدم حرفياً."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="syncfile_"))
        self.pc = self.tmp / "الكمبيوتر"       # المجلد الأب في الجهاز
        self.usb = self.tmp / "الفلاش"          # المجلد الأب في الفلاش
        # المصدر: 4 ملفات وورد، واحد منها عُدّل اليوم
        write(self.pc / "العمل/خطاب 1.docx", "خطاب واحد", age_days=10)
        write(self.pc / "العمل/خطاب 2.docx", "خطاب اثنان — بعد التعديل وإضافة فقرة", age_days=0)
        write(self.pc / "العمل/خطاب 3.docx", "خطاب ثلاثة", age_days=10)
        write(self.pc / "العمل/خطاب 4.docx", "خطاب أربعة جديد", age_days=1)
        # الهدف: 3 ملفات، وفيه خطاب 2 بنسخته القديمة
        write(self.usb / "العمل/خطاب 1.docx", "خطاب واحد", age_days=10)
        write(self.usb / "العمل/خطاب 2.docx", "خطاب اثنان", age_days=10)
        write(self.usb / "العمل/خطاب 3.docx", "خطاب ثلاثة", age_days=10)
        # مجلد موجود في الفلاش فقط
        write(self.usb / "المكتب/جدول المهام.xlsx", "مهام", age_days=3)
        write(self.usb / "المكتب/أرشيف/قديم.pdf", "pdf", age_days=30)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_folder_pair_work(self):
        a, b = self.pc / "العمل", self.usb / "العمل"
        plan = build_plan(a, b)
        act = actions(plan)
        self.assertEqual(act["خطاب 1.docx"], EQUAL)
        self.assertEqual(act["خطاب 3.docx"], EQUAL)
        self.assertEqual(act["خطاب 4.docx"], A2B)
        self.assertEqual(act["خطاب 2.docx"], A2B, "المعدّل الأحدث والأكبر يُختار تلقائياً")
        self.assertEqual(plan.count_reason("new"), 1)
        self.assertEqual(plan.count_reason("modified"), 1)
        self.assertIn("الأحدث في المصدر", [i for i in plan.items if i.rel == "خطاب 2.docx"][0].note)
        self.assertEqual(plan.decisions, [])
        execute(plan, {}, "اختبار")
        self.assertEqual(files(a), files(b))
        self.assertEqual(len(files(b)), 4)
        for f in files(a):
            self.assertEqual((a / f).read_bytes(), (b / f).read_bytes(), "متطابقة بالحرف")
        # النسخة القديمة من خطاب 2 في الهدف محفوظة
        old = list((b / QUARANTINE_DIR).rglob("خطاب 2.docx"))
        self.assertEqual(read(old[0]), "خطاب اثنان")
        self.assertTrue((a / META_DIR / "folder.json").exists() and (b / META_DIR / "folder.json").exists())
        self.assertEqual(build_plan(a, b).changes, [], "بعد المزامنة لا شيء جديد")

    def test_parent_level_copies_office_folder(self):
        plan, _ = sync(self.pc, self.usb)
        act = actions(plan)
        self.assertEqual(act["المكتب/جدول المهام.xlsx"], B2A)
        self.assertEqual(act["المكتب/أرشيف/قديم.pdf"], B2A)
        self.assertTrue((self.pc / "المكتب/أرشيف/قديم.pdf").exists(), "المجلد الفرعي يُنسخ كاملاً")
        self.assertEqual(files(self.pc), files(self.usb))

    def test_same_partition(self):
        """المجلدان على القسم نفسه يعملان، لكن لا يُقبل مجلد داخل الآخر."""
        with self.assertRaises(SyncError):
            build_plan(self.pc, self.pc / "العمل")
        with self.assertRaises(SyncError):
            build_plan(self.pc / "العمل", self.pc)


class Behaviour(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="syncfile_"))
        self.a, self.b = self.tmp / "المصدر/العمل", self.tmp / "الهدف/العمل"
        write(self.a / "تقرير.docx", "نسخة أولى", age_days=5)
        write(self.a / "ميزانية/الربع الأول.xlsx", "أرقام ١٢٣", age_days=5)
        write(self.a / "ملاحظات.txt", "سطر", age_days=5)
        write(self.a / "~$تقرير.docx", "قفل")          # يُتجاهل
        write(self.a / "desktop.ini", "[x]")             # يُتجاهل
        self.b.mkdir(parents=True)
        sync(self.a, self.b)
        (self.a / "~$تقرير.docx").unlink()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ignored_files(self):
        self.assertFalse((self.b / "desktop.ini").exists())
        self.assertFalse((self.b / "~$تقرير.docx").exists())

    def test_history_beats_clock(self):
        """بعد أول مزامنة: تعديل بتاريخ قديم جداً (ساعة خاطئة) يُعرف أنه المعدّل."""
        write(self.b / "تقرير.docx", "عُدّل في الهدف وساعة الجهاز متأخرة سنة", age_days=365)
        plan, _ = sync(self.a, self.b)
        self.assertEqual(actions(plan)["تقرير.docx"], B2A)
        self.assertEqual(read(self.a / "تقرير.docx"), "عُدّل في الهدف وساعة الجهاز متأخرة سنة")

    def test_conflict_choice_and_keep_both(self):
        write(self.a / "تقرير.docx", "تعديل المصدر")
        write(self.b / "تقرير.docx", "تعديل الهدف")
        plan = build_plan(self.a, self.b)
        it = [i for i in plan.items if i.rel == "تقرير.docx"][0]
        self.assertEqual(it.action, CONFLICT)
        self.assertEqual(it.suggest, KEEP_BOTH)
        res = execute(plan, {it.key: KEEP_BOTH}, "اختبار")
        other = res.kept_both[0][1]
        self.assertIn("_نسخة_الهدف_", other)
        self.assertEqual(read(self.a / "تقرير.docx"), "تعديل المصدر")
        self.assertEqual(read(self.b / "تقرير.docx"), "تعديل المصدر")
        self.assertEqual(read(self.a / other), "تعديل الهدف")
        self.assertEqual(read(self.b / other), "تعديل الهدف")
        self.assertEqual(build_plan(self.a, self.b).changes, [])

    def test_conflict_choose_target(self):
        write(self.a / "تقرير.docx", "تعديل المصدر")
        write(self.b / "تقرير.docx", "تعديل الهدف")
        plan = build_plan(self.a, self.b)
        k = [i for i in plan.items if i.rel == "تقرير.docx"][0].key
        execute(plan, {k: CHOOSE_B}, "اختبار")
        self.assertEqual(read(self.a / "تقرير.docx"), "تعديل الهدف")
        self.assertTrue(list((self.a / QUARANTINE_DIR).rglob("تقرير.docx")), "نسخة المصدر في النسخ الاحتياطية")

    def test_delete_propagates_to_backup(self):
        (self.a / "ملاحظات.txt").unlink()
        plan, _ = sync(self.a, self.b)
        self.assertEqual(actions(plan)["ملاحظات.txt"], DEL_B)
        self.assertFalse((self.b / "ملاحظات.txt").exists())
        self.assertTrue(list((self.b / QUARANTINE_DIR).rglob("ملاحظات.txt")))
        (self.b / "ميزانية/الربع الأول.xlsx").unlink()
        plan, _ = sync(self.a, self.b)
        self.assertEqual(actions(plan)["ميزانية/الربع الأول.xlsx"], DEL_A)
        self.assertFalse((self.a / "ميزانية").exists(), "المجلد الفارغ بعد الحذف يُزال")

    def test_edit_beats_delete(self):
        (self.a / "ملاحظات.txt").unlink()
        write(self.b / "ملاحظات.txt", "عدلته في الهدف")
        plan, _ = sync(self.a, self.b)
        self.assertEqual(actions(plan)["ملاحظات.txt"], B2A)
        self.assertEqual(read(self.a / "ملاحظات.txt"), "عدلته في الهدف")

    def test_open_office_file_blocked(self):
        write(self.a / "تقرير.docx", "تعديل")
        write(self.b / "~$قرير.docx", "وورد فاتح الملف")
        plan = build_plan(self.a, self.b)
        it = [i for i in plan.items if i.rel == "تقرير.docx"][0]
        self.assertIn("مفتوح", it.blocked)
        res = execute(plan, {}, "اختبار")
        self.assertEqual(read(self.b / "تقرير.docx"), "نسخة أولى")
        self.assertTrue(res.errors)
        (self.b / "~$قرير.docx").unlink()
        plan, _ = sync(self.a, self.b)
        self.assertEqual(actions(plan)["تقرير.docx"], A2B, "أُعيدت المحاولة بعد إغلاق وورد")

    def test_missing_folder_stops(self):
        shutil.move(str(self.b), str(self.tmp / "نُقل"))
        with self.assertRaises(SyncError):
            build_plan(self.a, self.b)
        self.assertTrue((self.a / "ملاحظات.txt").exists())

    def test_mass_delete_warning(self):
        for i in range(30):
            write(self.a / f"ملفات/ملف {i}.txt", str(i))
        sync(self.a, self.b)
        shutil.rmtree(self.a / "ملفات")
        plan = build_plan(self.a, self.b)
        self.assertTrue(plan.mass_delete)

    def test_failure_retried_next_time(self):
        write(self.a / "ملاحظات.txt", "تعديل")
        plan = build_plan(self.a, self.b)
        real = engine._replace
        engine._replace = lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "locked"))
        try:
            res = execute(plan, {}, "اختبار")
        finally:
            engine._replace = real
        self.assertTrue(res.errors and "مقفل" in res.errors[0][1])
        plan, _ = sync(self.a, self.b)
        self.assertEqual(actions(plan)["ملاحظات.txt"], A2B)

    def test_relocate_when_drive_letter_changes(self):
        fid = read_folder_id(self.b)
        self.assertTrue(fid)
        self.assertEqual(relocate(str(self.b), fid), str(self.b))
        self.assertIsNone(relocate(str(self.b), "غير-موجود"))


class FirstSyncTiming(unittest.TestCase):
    """أول مزامنة بلا سجل: التاريخ مع التحقق من الحجم."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="syncfile_"))
        self.a, self.b = self.tmp / "أ", self.tmp / "ب"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_newer_and_larger_auto(self):
        write(self.a / "ملف.docx", "قصير", age_days=5)
        write(self.b / "ملف.docx", "أطول لأنه عُدّل وأضيف إليه", age_days=1)
        self.assertEqual(actions(build_plan(self.a, self.b))["ملف.docx"], B2A)

    def test_newer_but_smaller_asks_user(self):
        write(self.a / "ملف.docx", "نسخة كبيرة فيها كل الفقرات المكتوبة", age_days=5)
        write(self.b / "ملف.docx", "صغيرة", age_days=1)
        plan = build_plan(self.a, self.b)
        it = plan.items[0]
        self.assertEqual(it.action, DECIDE)
        self.assertEqual(it.suggest, CHOOSE_A, "الاقتراح الافتراضي هو الأكبر")
        self.assertTrue(any("التوقيت غير متوافق" in w for w in plan.warnings))
        res = execute(plan, {}, "اختبار")
        self.assertTrue(res.errors, "لا ينفذ ملف القرار دون اختيار المستخدم")
        self.assertEqual(read(self.b / "ملف.docx"), "صغيرة")
        plan = build_plan(self.a, self.b)
        execute(plan, {plan.items[0].key: CHOOSE_A}, "اختبار")
        self.assertEqual(read(self.b / "ملف.docx"), "نسخة كبيرة فيها كل الفقرات المكتوبة")

    def test_same_time_different_content_asks(self):
        write(self.a / "ملف.txt", "أ", age_days=2)
        write(self.b / "ملف.txt", "ب ب", age_days=2)
        os.utime(self.b / "ملف.txt", (os.stat(self.a / "ملف.txt").st_atime, os.stat(self.a / "ملف.txt").st_mtime))
        self.assertEqual(build_plan(self.a, self.b).items[0].action, DECIDE)

    def test_report(self):
        from syncfile.report import write_reports
        write(self.a / "تقرير.docx", "x")
        self.b.mkdir()
        plan, res = sync(self.a, self.b)
        h, c = write_reports(self.tmp / "reports", plan, res, "جهاز", "العمل")
        self.assertIn('dir="rtl"', h.read_text(encoding="utf-8"))
        self.assertIn("تقرير.docx", c.read_text(encoding="utf-8-sig"))


class GoodSyncFeatures(unittest.TestCase):
    """أنواع المهام، تغيير الاتجاه بالزر الأيمن، الاستثناءات، تنظيف النسخ الاحتياطية."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="syncfile_gs_"))
        self.a, self.b = self.tmp / "مصدر", self.tmp / "هدف"
        write(self.a / "مشترك.txt", "1")
        write(self.b / "مشترك.txt", "1")
        sync(self.a, self.b)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_one_way_backup_a_to_b(self):
        write(self.a / "جديد في المصدر.txt", "أ")
        write(self.b / "جديد في الهدف.txt", "ب")
        plan = build_plan(self.a, self.b, mode=engine.A_TO_B)
        acts = actions(plan)
        self.assertEqual(acts["جديد في المصدر.txt"], A2B)
        self.assertEqual(acts["جديد في الهدف.txt"], engine.SKIP)
        execute(plan, {}, "اختبار")
        self.assertTrue((self.b / "جديد في المصدر.txt").exists())
        self.assertFalse((self.a / "جديد في الهدف.txt").exists())
        # الملف الذي لم يُنسخ يبقى ظاهراً في التحليل القادم
        self.assertEqual(actions(build_plan(self.a, self.b))["جديد في الهدف.txt"], B2A)

    def test_one_way_b_to_a_does_not_delete_target_only(self):
        write(self.a / "فقط في المصدر.txt", "أ")
        plan = build_plan(self.a, self.b, mode=engine.B_TO_A)
        self.assertEqual(actions(plan)["فقط في المصدر.txt"], engine.SKIP)
        self.assertEqual(plan.changes, [])

    def test_right_click_override(self):
        write(self.a / "مشترك.txt", "معدّل في المصدر")
        plan = build_plan(self.a, self.b)
        it = next(i for i in plan.items if i.rel == "مشترك.txt")
        self.assertEqual(it.action, A2B)
        self.assertEqual([v for v, _ in engine.choices_for(it)], [CHOOSE_A, CHOOSE_B, KEEP_BOTH, engine.NO_COPY])
        execute(plan, {it.key: CHOOSE_B}, "اختبار")          # المستخدم عكس الاتجاه
        self.assertEqual(read(self.a / "مشترك.txt"), "1")
        self.assertTrue(any(QUARANTINE_DIR in str(p) for p in self.a.rglob("مشترك.txt")))

    def test_skip_keeps_change_for_later(self):
        write(self.a / "مشترك.txt", "معدّل")
        plan = build_plan(self.a, self.b)
        key = plan.items[0].key
        execute(plan, {key: engine.NO_COPY}, "اختبار")
        self.assertEqual(read(self.b / "مشترك.txt"), "1")
        self.assertEqual(actions(build_plan(self.a, self.b))["مشترك.txt"], A2B)

    def test_override_on_missing_side_means_delete(self):
        write(self.a / "جديد.txt", "x")
        plan = build_plan(self.a, self.b)
        it = next(i for i in plan.items if i.rel == "جديد.txt")
        self.assertEqual(engine.resolve_choice(it, CHOOSE_B), DEL_A)
        execute(plan, {it.key: CHOOSE_B}, "اختبار")
        self.assertFalse((self.a / "جديد.txt").exists())

    def test_excludes(self):
        from syncfile.fsutil import parse_excludes
        pats = parse_excludes("*.bak; مؤقت، صور قديمة/*")
        self.assertEqual(pats, ("*.bak", "مؤقت", "صور قديمة/*"))
        write(self.a / "ملف.BAK", "x")
        write(self.a / "مؤقت/أ.txt", "x")
        write(self.a / "صور قديمة/1.jpg", "x")
        write(self.a / "مهم.txt", "x")
        plan = build_plan(self.a, self.b, excludes=pats)
        self.assertEqual(sorted(i.rel for i in plan.changes), ["مهم.txt"])

    def test_cleanup_old_backups(self):
        from syncfile.fsutil import cleanup_backups
        old = self.a / QUARANTINE_DIR / "2020-01-01_000000"
        new = self.a / QUARANTINE_DIR / "اليوم"
        write(old / "قديم.txt", "x")
        write(new / "حديث.txt", "x")
        t = time.time() - 40 * 86400
        os.utime(old, (t, t))
        self.assertEqual(cleanup_backups(self.a, 30), 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        self.assertEqual(cleanup_backups(self.a, 0), 0)        # 0 = احفظ للأبد


class AppConfigTest(unittest.TestCase):
    def test_jobs_saved(self):
        from syncfile.state import AppConfig
        tmp = Path(tempfile.mkdtemp())
        try:
            c = AppConfig(tmp)
            p = c.add_pair("C:/a", "E:/b", "العمل", mode=engine.A_TO_B, excludes="*.bak", auto=True)
            c2 = AppConfig(tmp)
            j = c2.get(p["id"])
            self.assertEqual((j["name"], j["mode"], j["excludes"], j["auto"], j["keep_days"]),
                             ("العمل", engine.A_TO_B, "*.bak", True, 30))
            c2.remove(p["id"])
            self.assertEqual(AppConfig(tmp).pairs, [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
