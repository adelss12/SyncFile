"""
اختبار المحرك بمحاكاة جهازين (المكتب والبيت) وفلاش واحد، على ملفات حقيقية.
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

from syncfile.carrier import Carrier                       # noqa: E402
from syncfile import engine                                # noqa: E402
from syncfile.engine import (CONFLICT, DEL_LOCAL, DEL_REMOTE, EQUAL, PULL, PUSH, SyncError,  # noqa: E402
                             build_plan, create_session, execute, join_session)
from syncfile.fsutil import QUARANTINE_DIR                 # noqa: E402
from syncfile.state import LocalState                      # noqa: E402


class Device:
    def __init__(self, base: Path, name: str):
        self.state = LocalState(base / f"{name}_appdata")
        self.state.cfg["device_name"] = name
        self.state.save_cfg()
        self.name = name

    def sync(self, carrier, sid, expect_ok=True):
        plan = build_plan(self.state, carrier, sid)
        res = execute(plan, self.state, carrier)
        if expect_ok:
            assert not res.errors, res.errors
        return plan, res


def write(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def actions(plan):
    return {i.rel: i.action for i in plan.items}


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="syncfile_"))
        self.usb = Carrier(self.tmp / "USB").init()
        self.office = Device(self.tmp, "جهاز-المكتب")
        self.home = Device(self.tmp, "جهاز-البيت")
        self.work_office = self.tmp / "office" / "العمل"
        write(self.work_office / "تقرير الشهر.docx", "نسخة أولى")
        write(self.work_office / "ميزانية/الربع الأول.xlsx", "أرقام ١٢٣")
        write(self.work_office / "ملاحظات.txt", "سطر")
        write(self.work_office / "~$تقرير الشهر.docx", "قفل وورد")      # يجب تجاهله
        write(self.work_office / "desktop.ini", "[x]")                  # يجب تجاهله
        self.sid = create_session(self.office.state, self.usb, self.work_office)
        self.office.sync(self.usb, self.sid)
        (self.work_office / "~$تقرير الشهر.docx").unlink()              # «أُغلق وورد»
        self.work_home = join_session(self.home.state, self.usb, self.sid, self.tmp / "home")
        self.home.sync(self.usb, self.sid)

    def tearDown(self):
        for d in (self.office, self.home):
            d.state.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------
    def test_initial_copy_with_arabic_names(self):
        self.assertEqual(self.work_home.name, "العمل")
        self.assertEqual((self.work_home / "ميزانية/الربع الأول.xlsx").read_text(encoding="utf-8"), "أرقام ١٢٣")
        self.assertFalse((self.work_home / "~$تقرير الشهر.docx").exists(), "ملف قفل وورد لا يُزامن")
        self.assertFalse((self.work_home / "desktop.ini").exists())
        plan = build_plan(self.home.state, self.usb, self.sid)
        self.assertEqual(plan.changes, [], "بعد المزامنة كل شيء متطابق")

    def test_edit_travels_office_to_home(self):
        write(self.work_office / "تقرير الشهر.docx", "نسخة ثانية من المكتب")
        p, _ = self.office.sync(self.usb, self.sid)
        self.assertEqual(actions(p)["تقرير الشهر.docx"], PUSH)
        p, _ = self.home.sync(self.usb, self.sid)
        self.assertEqual(actions(p)["تقرير الشهر.docx"], PULL)
        self.assertEqual((self.work_home / "تقرير الشهر.docx").read_text(encoding="utf-8"), "نسخة ثانية من المكتب")
        # النسخة القديمة في البيت محفوظة في النسخ الاحتياطية لا محذوفة
        backups = list((self.work_home / QUARANTINE_DIR).rglob("تقرير الشهر.docx"))
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "نسخة أولى")

    def test_wrong_clock_does_not_matter(self):
        """ساعة البيت متأخرة سنة كاملة: التعديل يبقى هو الأحدث لأن المقارنة بالبصمة."""
        f = self.work_home / "ملاحظات.txt"
        write(f, "تعديل من البيت")
        old = time.time() - 365 * 86400
        os.utime(f, (old, old))
        self.home.sync(self.usb, self.sid)
        p, _ = self.office.sync(self.usb, self.sid)
        self.assertEqual(actions(p)["ملاحظات.txt"], PULL)
        self.assertEqual((self.work_office / "ملاحظات.txt").read_text(encoding="utf-8"), "تعديل من البيت")

    def test_conflict_keeps_both_versions(self):
        write(self.work_office / "تقرير الشهر.docx", "تعديل المكتب")
        self.office.sync(self.usb, self.sid)
        write(self.work_home / "تقرير الشهر.docx", "تعديل البيت")
        p, res = self.home.sync(self.usb, self.sid)
        self.assertEqual(actions(p)["تقرير الشهر.docx"], CONFLICT)
        self.assertIn("جهاز-المكتب", p.items[[i.rel for i in p.items].index("تقرير الشهر.docx")].note)
        cname = res.conflicts[0][1]
        self.assertIn("_تعارض_جهاز-المكتب_", cname)
        self.assertTrue(cname.startswith("تقرير الشهر_تعارض_"), "الاسم العربي الأصلي محفوظ")
        self.assertEqual((self.work_home / "تقرير الشهر.docx").read_text(encoding="utf-8"), "تعديل البيت")
        self.assertEqual((self.work_home / cname).read_text(encoding="utf-8"), "تعديل المكتب")
        # والمكتب يستلم النسختين في المزامنة التالية
        self.office.sync(self.usb, self.sid)
        self.assertEqual((self.work_office / "تقرير الشهر.docx").read_text(encoding="utf-8"), "تعديل البيت")
        self.assertEqual((self.work_office / cname).read_text(encoding="utf-8"), "تعديل المكتب")
        self.assertEqual(build_plan(self.office.state, self.usb, self.sid).changes, [])

    def test_delete_propagates_to_backup_not_permanently(self):
        (self.work_office / "ملاحظات.txt").unlink()
        p, _ = self.office.sync(self.usb, self.sid)
        self.assertEqual(actions(p)["ملاحظات.txt"], DEL_REMOTE)
        self.assertTrue(list((self.usb.data_dir(self.sid) / QUARANTINE_DIR).rglob("ملاحظات.txt")))
        p, _ = self.home.sync(self.usb, self.sid)
        self.assertEqual(actions(p)["ملاحظات.txt"], DEL_LOCAL)
        self.assertFalse((self.work_home / "ملاحظات.txt").exists())
        self.assertTrue(list((self.work_home / QUARANTINE_DIR).rglob("ملاحظات.txt")), "يمكن استرجاعه")

    def test_edit_beats_delete(self):
        (self.work_office / "ملاحظات.txt").unlink()
        self.office.sync(self.usb, self.sid)
        write(self.work_home / "ملاحظات.txt", "عدّلته في البيت قبل أن أعلم بالحذف")
        p, _ = self.home.sync(self.usb, self.sid)
        self.assertEqual(actions(p)["ملاحظات.txt"], PUSH)
        self.office.sync(self.usb, self.sid)
        self.assertEqual((self.work_office / "ملاحظات.txt").read_text(encoding="utf-8"),
                         "عدّلته في البيت قبل أن أعلم بالحذف")

    def test_new_files_both_sides_and_subfolders(self):
        write(self.work_office / "مجلد جديد/عقد.pdf", "pdf")
        write(self.work_home / "صور/إيصال.txt", "إيصال")
        self.office.sync(self.usb, self.sid)
        p, _ = self.home.sync(self.usb, self.sid)
        a = actions(p)
        self.assertEqual(a["مجلد جديد/عقد.pdf"], PULL)
        self.assertEqual(a["صور/إيصال.txt"], PUSH)
        self.assertTrue([i for i in p.items if i.rel == "صور/إيصال.txt"][0].is_new)
        self.office.sync(self.usb, self.sid)
        self.assertTrue((self.work_office / "صور/إيصال.txt").exists())

    def test_open_office_file_is_not_overwritten(self):
        write(self.work_office / "تقرير الشهر.docx", "تعديل جديد")
        self.office.sync(self.usb, self.sid)
        write(self.work_home / "~$رير الشهر.docx", "وورد فاتح الملف")   # أسلوب وورد في تسمية القفل
        p, res = self.home.sync(self.usb, self.sid, expect_ok=False)
        it = [i for i in p.items if i.rel == "تقرير الشهر.docx"][0]
        self.assertIn("مفتوح", it.blocked)
        self.assertEqual((self.work_home / "تقرير الشهر.docx").read_text(encoding="utf-8"), "نسخة أولى")
        (self.work_home / "~$رير الشهر.docx").unlink()
        p, _ = self.home.sync(self.usb, self.sid)                       # بعد إغلاق وورد تكتمل
        self.assertEqual((self.work_home / "تقرير الشهر.docx").read_text(encoding="utf-8"), "تعديل جديد")

    def test_missing_local_folder_stops_instead_of_deleting(self):
        shutil.move(str(self.work_home), str(self.tmp / "نُقل"))
        with self.assertRaises(SyncError):
            build_plan(self.home.state, self.usb, self.sid)
        self.assertTrue((self.usb.data_dir(self.sid) / "ملاحظات.txt").exists(), "لم يُحذف شيء من الفلاش")

    def test_mass_delete_warning(self):
        for i in range(30):
            write(self.work_office / f"ملفات/ملف {i}.txt", str(i))
        self.office.sync(self.usb, self.sid)
        shutil.rmtree(self.work_office / "ملفات")
        (self.work_office / "ملاحظات.txt").unlink()
        p = build_plan(self.office.state, self.usb, self.sid)
        self.assertTrue(p.mass_delete)
        self.assertTrue(any("سيُحذف" in w for w in p.warnings))

    def test_failed_copy_is_retried_next_time(self):
        write(self.work_office / "ملاحظات.txt", "تعديل")
        plan = build_plan(self.office.state, self.usb, self.sid)
        real = engine._replace
        engine._replace = lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "locked"))
        try:
            res = execute(plan, self.office.state, self.usb)
        finally:
            engine._replace = real
        self.assertTrue(res.errors and "مقفل" in res.errors[0][1])
        p, _ = self.office.sync(self.usb, self.sid)
        self.assertEqual(actions(p)["ملاحظات.txt"], PUSH, "لم يُحدّث الأساس عند الفشل فيُعاد المحاولة")

    def test_flash_edited_directly(self):
        """تعديل الملف مباشرة على الفلاش يُعامل كتعديل من الطرف الآخر."""
        write(self.usb.data_dir(self.sid) / "ملاحظات.txt", "عُدّل على الفلاش")
        p, _ = self.office.sync(self.usb, self.sid)
        self.assertEqual(actions(p)["ملاحظات.txt"], PULL)

    def test_relinking_existing_folder_does_not_delete(self):
        """ربط مجلد موجود فيه ملفات على جهاز ثالث: لا حذف، والمختلف تعارض محفوظ."""
        third = Device(self.tmp, "جهاز-ثالث")
        folder = self.tmp / "third" / "العمل"
        write(folder / "ملاحظات.txt", "نسخة قديمة مختلفة")
        write(folder / "خاص.txt", "ملف موجود هنا فقط")
        third.state.link_session(self.sid, "العمل", folder)
        p, res = third.sync(self.usb, self.sid)
        a = actions(p)
        self.assertEqual(a["ملاحظات.txt"], CONFLICT)
        self.assertEqual(a["خاص.txt"], PUSH)
        self.assertNotIn(DEL_LOCAL, a.values())
        self.assertNotIn(DEL_REMOTE, a.values())
        third.state.close()

    def test_equal_after_everything(self):
        p = build_plan(self.office.state, self.usb, self.sid)
        self.assertTrue(all(i.action == EQUAL for i in p.items))

    def test_report_html_and_csv(self):
        from syncfile.report import write_reports
        write(self.work_office / "تقرير الشهر.docx", "تعديل")
        p, res = self.office.sync(self.usb, self.sid)
        h, c = write_reports(self.tmp / "reports", p, res, "جهاز-المكتب")
        page = h.read_text(encoding="utf-8")
        self.assertIn('dir="rtl"', page)
        self.assertIn("تقرير الشهر.docx", page)
        rows = c.read_text(encoding="utf-8-sig").splitlines()
        self.assertEqual(rows[0].split(",")[0], "المسار")
        self.assertTrue(any("تقرير الشهر.docx" in r for r in rows[1:]))

    def test_clock_warning_when_this_pc_is_behind(self):
        m = self.usb.manifest(self.sid)
        m["last_sync"] = {"device": "جهاز-المكتب", "device_id": "other", "at": "2099-01-01T10:00:00+03:00"}
        self.usb.save_manifest(self.sid, m)
        p = build_plan(self.home.state, self.usb, self.sid)
        self.assertTrue(any("متأخرة" in w and "جهاز-المكتب" in w for w in p.warnings), p.warnings)


if __name__ == "__main__":
    unittest.main(verbosity=2)
