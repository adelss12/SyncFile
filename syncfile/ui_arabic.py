"""
الواجهة العربية (PySide6 / Qt) — من اليمين لليسار بالكامل.

الأقسام:
  • حالة الفلاش: هل هو موصول، نظام ملفاته، المساحة، آخر مزامنة.
  • الجلسات: كل جلسة = مجلد، وزر (+) لجلسة جديدة، وربط الجلسات الجديدة بهذا الجهاز.
  • المعاينة: كل ملف وحالته بالألوان قبل أي تنفيذ.
  • التقدم والسرعة، والسجل.
"""

import os
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QGroupBox, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QListWidget, QListWidgetItem,
                               QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import __version__
from .carrier import Carrier, find_carriers
from .engine import (CONFLICT, DEL_LOCAL, DEL_REMOTE, EQUAL, FORGET, PULL, PUSH, SyncError, build_plan,
                     create_session, execute, join_session)
from .fsutil import QUARANTINE_DIR
from .report import write_reports
from .state import LocalState

COLORS = {  # الأخضر متطابق، الأصفر تحديث، الأحمر تعارض أو متوقف، البرتقالي حذف
    EQUAL: "#16a34a", PUSH: "#ca8a04", PULL: "#ca8a04", CONFLICT: "#dc2626",
    DEL_REMOTE: "#ea580c", DEL_LOCAL: "#ea580c", FORGET: "#64748b",
}

STYLE = """
QWidget { font-family: "Segoe UI", Tahoma; font-size: 10.5pt; }
QMainWindow, QWidget#root { background: #f1f5f9; }
QGroupBox { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; margin-top: 14px; padding: 10px; font-weight: bold; }
QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top right; right: 14px; padding: 0 6px; color: #0369a1; }
QPushButton { background: #e2e8f0; border: none; border-radius: 8px; padding: 8px 14px; }
QPushButton:hover { background: #cbd5e1; }
QPushButton:disabled { color: #94a3b8; background: #f1f5f9; }
QPushButton#primary { background: #0284c7; color: white; font-weight: bold; }
QPushButton#primary:hover { background: #0369a1; }
QPushButton#go { background: #16a34a; color: white; font-weight: bold; }
QPushButton#go:hover { background: #15803d; }
QPushButton#stop { background: #dc2626; color: white; }
QTreeWidget, QListWidget, QPlainTextEdit { background: white; border: 1px solid #e2e8f0; border-radius: 8px; }
QProgressBar { border: 1px solid #cbd5e1; border-radius: 6px; text-align: center; height: 18px; }
QProgressBar::chunk { background: #0284c7; border-radius: 6px; }
"""


def size_ar(n: float) -> str:
    for u in ("بايت", "كيلوبايت", "ميجابايت", "جيجابايت"):
        if n < 1024:
            return f"{int(n)} {u}" if u == "بايت" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} تيرابايت"


class Bridge(QObject):
    """إشارات من الخيط الخلفي إلى الواجهة (Qt ينقلها بأمان إلى الخيط الرئيسي)."""
    log = Signal(str)
    progress = Signal(float, float, str)
    plan_ready = Signal(object)
    sync_done = Signal(object, object)
    failed = Signal(str)


class MainWindow(QMainWindow):
    def __init__(self, state: LocalState = None):
        super().__init__()
        self.state = state or LocalState()
        self.carrier = None
        self.plan = None
        self.busy = False
        self.cancel = threading.Event()
        self.last_report = None
        self.bridge = Bridge()
        self.bridge.log.connect(self.log)
        self.bridge.progress.connect(self.on_progress)
        self.bridge.plan_ready.connect(self.show_plan)
        self.bridge.sync_done.connect(self.on_synced)
        self.bridge.failed.connect(self.on_failed)
        self._speed = (0.0, 0.0)

        self.setWindowTitle(f"SyncFile — مزامنة الملفات بالفلاش  {__version__}")
        self.resize(1100, 760)
        self.build()
        self.refresh_carriers(select_last=True)
        self.timer = QTimer(self)                 # يلاحظ توصيل الفلاش أو فصله
        self.timer.timeout.connect(self.watch_drives)
        self.timer.start(3000)

    # ================================================================ البناء
    def build(self):
        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(14, 12, 14, 12)

        head = QHBoxLayout()
        title = QLabel("🔄 مزامنة الملفات بين الأجهزة عن طريق الفلاش")
        title.setStyleSheet("font-size: 16pt; font-weight: bold; color: #0f172a;")
        head.addWidget(title)
        head.addStretch()
        self.device_btn = QPushButton()
        self.device_btn.clicked.connect(self.rename_device)
        head.addWidget(self.device_btn)
        v.addLayout(head)
        self.update_device_btn()

        # --- الفلاش
        g = QGroupBox("حالة الفلاش")
        gl = QHBoxLayout(g)
        self.carrier_combo = QComboBox()
        self.carrier_combo.setMinimumWidth(240)
        self.carrier_combo.currentIndexChanged.connect(self.on_carrier_changed)
        gl.addWidget(self.carrier_combo)
        b = QPushButton("اختيار مجلد يدويًا…")
        b.clicked.connect(self.pick_carrier_folder)
        gl.addWidget(b)
        self.carrier_lbl = QLabel()
        self.carrier_lbl.setWordWrap(True)
        gl.addWidget(self.carrier_lbl, 1)
        v.addWidget(g)

        split = QSplitter(Qt.Horizontal)
        # --- الجلسات
        sg = QGroupBox("الجلسات (كل جلسة مجلد)")
        sl = QVBoxLayout(sg)
        self.sessions = QListWidget()
        self.sessions.currentItemChanged.connect(lambda *_: self.on_session_changed())
        sl.addWidget(self.sessions)
        row = QHBoxLayout()
        self.add_btn = QPushButton("＋ جلسة جديدة", objectName="primary")
        self.add_btn.clicked.connect(self.add_session)
        row.addWidget(self.add_btn)
        self.link_btn = QPushButton("ربط بهذا الجهاز")
        self.link_btn.clicked.connect(self.link_session)
        row.addWidget(self.link_btn)
        sl.addLayout(row)
        self.open_btn = QPushButton("📂 فتح المجلد")
        self.open_btn.clicked.connect(lambda: self.open_path(self.current_local()))
        sl.addWidget(self.open_btn)
        self.backup_btn = QPushButton("🗂 النسخ الاحتياطية")
        self.backup_btn.clicked.connect(self.open_backups)
        sl.addWidget(self.backup_btn)
        split.addWidget(sg)

        # --- المعاينة
        pg = QGroupBox("معاينة التغييرات قبل المزامنة")
        pl = QVBoxLayout(pg)
        top = QHBoxLayout()
        self.summary_lbl = QLabel("اختر جلسة ثم اضغط «فحص ومعاينة».")
        top.addWidget(self.summary_lbl, 1)
        self.show_equal = QCheckBox("إظهار المتطابق")
        self.show_equal.stateChanged.connect(lambda *_: self.show_plan(self.plan) if self.plan else None)
        top.addWidget(self.show_equal)
        pl.addLayout(top)
        self.warn_lbl = QLabel()
        self.warn_lbl.setWordWrap(True)
        self.warn_lbl.setStyleSheet("color: #b45309;")
        self.warn_lbl.hide()
        pl.addWidget(self.warn_lbl)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["الملف", "الحالة", "الحجم", "ملاحظة"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        hdr = self.tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        pl.addWidget(self.tree)
        split.addWidget(pg)
        split.setSizes([300, 800])
        v.addWidget(split, 1)

        # --- الأوامر والتقدم
        act = QHBoxLayout()
        self.scan_btn = QPushButton("🔍 فحص ومعاينة", objectName="primary")
        self.scan_btn.clicked.connect(self.start_scan)
        act.addWidget(self.scan_btn)
        self.sync_btn = QPushButton("✔ مزامنة الآن", objectName="go")
        self.sync_btn.clicked.connect(self.start_sync)
        act.addWidget(self.sync_btn)
        self.stop_btn = QPushButton("⏹ إيقاف", objectName="stop")
        self.stop_btn.clicked.connect(lambda: self.cancel.set())
        act.addWidget(self.stop_btn)
        self.report_btn = QPushButton("📄 آخر تقرير")
        self.report_btn.clicked.connect(lambda: self.open_path(self.last_report))
        act.addWidget(self.report_btn)
        act.addStretch()
        self.speed_lbl = QLabel()
        act.addWidget(self.speed_lbl)
        v.addLayout(act)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        v.addWidget(self.progress)
        self.log_box = QPlainTextEdit(readOnly=True)
        self.log_box.setMaximumHeight(130)
        v.addWidget(self.log_box)
        self.update_buttons()

    # ================================================================ أدوات
    def log(self, msg):
        self.log_box.appendPlainText(f"{time.strftime('%H:%M:%S')}  {msg}")

    def open_path(self, p):
        if p and os.path.exists(str(p)):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    def update_device_btn(self):
        self.device_btn.setText(f"💻 هذا الجهاز: {self.state.device_name}")

    def rename_device(self):
        name, ok = QInputDialog.getText(self, "اسم الجهاز",
                                        "اسم يظهر في أسماء ملفات التعارض (مثل: جهاز-المكتب):",
                                        text=self.state.device_name)
        if ok and name.strip():
            self.state.cfg["device_name"] = name.strip()
            self.state.save_cfg()
            self.update_device_btn()

    def current_sid(self):
        it = self.sessions.currentItem()
        return it.data(Qt.UserRole) if it else None

    def current_local(self):
        sid = self.current_sid()
        return self.state.local_path(sid) if sid else None

    def update_buttons(self):
        sid = self.current_sid()
        linked = bool(sid and self.state.local_path(sid))
        has = self.carrier is not None
        idle = not self.busy
        self.add_btn.setEnabled(has and idle)
        self.link_btn.setEnabled(bool(sid) and not linked and idle)
        self.open_btn.setEnabled(linked)
        self.backup_btn.setEnabled(linked)
        self.scan_btn.setEnabled(linked and idle)
        self.sync_btn.setEnabled(idle and self.plan is not None and bool(self.plan.changes)
                                 and self.plan.sid == sid)
        self.stop_btn.setEnabled(self.busy)
        self.report_btn.setEnabled(bool(self.last_report))
        self.carrier_combo.setEnabled(idle)

    # ================================================================ الفلاش
    def refresh_carriers(self, select_last=False):
        found = find_carriers()
        keep = self.carrier.drive if self.carrier else None
        self.carrier_combo.blockSignals(True)
        self.carrier_combo.clear()
        paths = []
        for f in found:
            c = f["carrier"]
            label = f"{f['letter']}  " + ("✓ فلاش SyncFile" if f["initialized"] else "فلاش جديد")
            self.carrier_combo.addItem(label, str(c.drive))
            paths.append(str(c.drive))
        last = self.state.cfg.get("last_carrier")
        manual = [p for p in (str(keep) if keep else None, last) if p and p not in paths and os.path.isdir(p)]
        for p in dict.fromkeys(manual):
            self.carrier_combo.addItem(f"📁 {p}", p)
            paths.append(p)
        self._detected = tuple(str(f["carrier"].drive) for f in found)
        if not paths:
            self.carrier_combo.addItem("لا يوجد فلاش موصول", "")
        target = str(keep) if keep else (last if select_last else None)
        idx = self.carrier_combo.findData(target) if target else -1
        self.carrier_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.carrier_combo.blockSignals(False)
        self.on_carrier_changed()

    def watch_drives(self):
        if self.busy:
            return
        now = tuple(str(f["carrier"].drive) for f in find_carriers())
        if now != getattr(self, "_detected", ()):
            self.log("تغيّرت الأقراص الموصولة.")
            self.refresh_carriers()

    def pick_carrier_folder(self):
        p = QFileDialog.getExistingDirectory(self, "اختر الفلاش أو أي مجلد ينقل الملفات")
        if p:
            self.set_carrier(Path(p))
            self.refresh_carriers()

    def on_carrier_changed(self):
        path = self.carrier_combo.currentData()
        self.set_carrier(Path(path) if path else None)

    def set_carrier(self, path):
        self.carrier = Carrier(path) if path else None
        self.plan = None
        self.tree.clear()
        if self.carrier:
            self.state.cfg["last_carrier"] = str(path)
            self.state.save_cfg()
        self.show_carrier_status()
        self.load_sessions()

    def show_carrier_status(self):
        c = self.carrier
        if not c:
            self.carrier_lbl.setText("🔌 وصّل الفلاش، أو اختر مجلدًا يدويًا.")
            return
        fs = c.filesystem() or "—"
        parts = [f"🟢 متصل: {c.drive}", f"المساحة المتاحة: {size_ar(c.free_space())}", f"النظام: {fs}"]
        if fs.upper() == "FAT32":
            parts.append("⚠️ FAT32 لا يقبل ملفًا أكبر من 4 جيجابايت")
        if not c.initialized:
            parts.append("جديد — اضغط «＋ جلسة جديدة» لبدء أول جلسة عليه")
        self.carrier_lbl.setText("   •   ".join(parts))

    # ================================================================ الجلسات
    def load_sessions(self, select=None):
        sid_before = select or self.current_sid()
        self.sessions.clear()
        if self.carrier and self.carrier.initialized:
            for s in self.carrier.sessions():
                local = self.state.local_path(s["id"])
                last = (self.carrier.manifest(s["id"]).get("last_sync") or {})
                when = f"\nآخر مزامنة: {last.get('device', '')} {last.get('at', '')[:16].replace('T', ' ')}" if last else ""
                text = f"📁 {s['name']}\n" + (f"↔ {local}" if local else "🆕 جديدة على هذا الجهاز — اضغط «ربط»") + when
                it = QListWidgetItem(text)
                it.setData(Qt.UserRole, s["id"])
                if not local:
                    it.setForeground(QBrush(QColor("#0369a1")))
                self.sessions.addItem(it)
                if s["id"] == sid_before:
                    self.sessions.setCurrentItem(it)
        if self.sessions.count() and not self.sessions.currentItem():
            self.sessions.setCurrentRow(0)
        self.update_buttons()

    def on_session_changed(self):
        self.plan = None
        self.tree.clear()
        self.warn_lbl.hide()
        sid = self.current_sid()
        if sid and not self.state.local_path(sid):
            self.summary_lbl.setText("هذه الجلسة موجودة على الفلاش وليست مربوطة بهذا الجهاز بعد. اضغط «ربط بهذا الجهاز».")
        else:
            self.summary_lbl.setText("اضغط «فحص ومعاينة» لرؤية التغييرات.")
        self.update_buttons()

    def add_session(self):
        folder = QFileDialog.getExistingDirectory(self, "اختر المجلد الذي تريد مزامنته")
        if not folder:
            return
        try:
            sid = create_session(self.state, self.carrier, Path(folder))
        except SyncError as e:
            QMessageBox.warning(self, "جلسة جديدة", str(e))
            return
        self.log(f"أُنشئت جلسة «{Path(folder).name}» على الفلاش.")
        self.show_carrier_status()
        self.load_sessions(select=sid)
        self.start_scan()

    def link_session(self):
        sid = self.current_sid()
        if not sid:
            return
        name = self.carrier.session_name(sid)
        default = str(Path.home() / "Documents")
        parent = QFileDialog.getExistingDirectory(
            self, f"اختر المكان الذي سيُنشأ فيه المجلد «{name}» على هذا الجهاز", default)
        if not parent:
            return
        target = join_session(self.state, self.carrier, sid, Path(parent))
        self.log(f"رُبطت الجلسة «{name}» بالمجلد {target}")
        self.load_sessions(select=sid)
        self.start_scan()

    def open_backups(self):
        local = self.current_local()
        if local:
            p = Path(local) / QUARANTINE_DIR
            if p.exists():
                self.open_path(p)
            else:
                QMessageBox.information(self, "النسخ الاحتياطية", "لا توجد نسخ احتياطية بعد لهذا المجلد.")

    # ================================================================ المعاينة
    def set_busy(self, busy, text=None):
        self.busy = busy
        if busy:
            self.cancel.clear()
            self.progress.setRange(0, 0)       # مؤشر انتظار متحرك
        else:
            self.progress.setRange(0, 1000)
            self.progress.setValue(0)
            self.speed_lbl.setText("")
        if text:
            self.summary_lbl.setText(text)
        self.update_buttons()

    def start_scan(self):
        sid = self.current_sid()
        if not sid or not self.state.local_path(sid) or self.busy:
            return
        self.set_busy(True, "جارٍ الفحص… لا يُلمس أي ملف في هذه المرحلة.")
        self.tree.clear()

        def work():
            try:
                plan = build_plan(self.state, self.carrier, sid, self.cancel)
                self.bridge.plan_ready.emit(plan)
            except InterruptedError:
                self.bridge.failed.emit("أُوقف الفحص.")
            except SyncError as e:
                self.bridge.failed.emit(str(e))
            except Exception as e:
                self.bridge.failed.emit(f"خطأ غير متوقع أثناء الفحص: {e}")
        threading.Thread(target=work, daemon=True).start()

    def show_plan(self, plan):
        self.plan = plan
        if self.busy:
            self.set_busy(False)
        self.tree.clear()
        show_all = self.show_equal.isChecked()
        for it in plan.items:
            if it.action == FORGET or (it.action == EQUAL and not show_all):
                continue
            row = QTreeWidgetItem([it.rel, it.label, size_ar(it.size), it.blocked or it.note])
            color = QColor("#dc2626" if it.blocked else COLORS.get(it.action, "#334155"))
            row.setForeground(1, QBrush(color))
            f = QFont()
            f.setBold(it.action != EQUAL)
            row.setFont(1, f)
            row.setTextAlignment(2, Qt.AlignCenter)
            self.tree.addTopLevelItem(row)
        n = len(plan.changes)
        blocked = sum(1 for i in plan.items if i.blocked)
        if n:
            txt = (f"التغييرات: {n}  •  للفلاش: {plan.count(PUSH)}  •  لهذا الجهاز: {plan.count(PULL)}  •  "
                   f"تعارض: {plan.count(CONFLICT)}  •  حذف: {plan.count(DEL_REMOTE) + plan.count(DEL_LOCAL)}"
                   + (f"  •  متوقف: {blocked}" if blocked else "") + f"  •  النقل: {size_ar(plan.transfer_bytes)}")
        else:
            txt = f"✅ كل الملفات متطابقة ({len(plan.items)} ملف). لا شيء لمزامنته."
        self.summary_lbl.setText(txt)
        msgs = plan.warnings + [f"تعذرت قراءة {r}: {why}" for r, why in plan.errors[:5]]
        self.warn_lbl.setText("\n".join(msgs))
        self.warn_lbl.setVisible(bool(msgs))
        self.update_buttons()

    # ================================================================ المزامنة
    def start_sync(self):
        plan = self.plan
        if not plan or self.busy:
            return
        if plan.mass_delete:
            r = QMessageBox.warning(self, "تأكيد الحذف",
                                    "\n".join(w for w in plan.warnings if "سيُحذف" in w) +
                                    "\n\nالملفات المحذوفة تذهب إلى النسخ الاحتياطية ويمكن استرجاعها.\nهل تريد المتابعة؟",
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                return
        self.set_busy(True, "جارٍ المزامنة… لا تفصل الفلاش.")
        self.progress.setRange(0, 1000)
        self._speed = (time.time(), 0.0)

        def work():
            try:
                res = execute(plan, self.state, self.carrier, self.cancel,
                              progress=lambda done, total, rel: self.bridge.progress.emit(float(done), float(total), rel))
                self.bridge.sync_done.emit(plan, res)
            except Exception as e:
                self.bridge.failed.emit(f"خطأ غير متوقع أثناء المزامنة: {e}")
        threading.Thread(target=work, daemon=True).start()

    def on_progress(self, done, total, rel):
        self.progress.setValue(int(done / max(total, 1) * 1000))
        t0, _ = self._speed
        speed = done / max(time.time() - t0, 0.001)
        self.speed_lbl.setText(f"{size_ar(done)} من {size_ar(total)}  •  {size_ar(speed)}/ث  •  {Path(rel).name}")

    def on_synced(self, plan, res):
        self.set_busy(False)
        try:
            h, _ = write_reports(self.state.home / "reports", plan, res, self.state.device_name)
            self.last_report = h
        except OSError as e:
            self.log(f"تعذرت كتابة التقرير: {e}")
        for rel, what in res.done:
            self.log(f"{what}: {rel}")
        for rel, why in res.errors:
            self.log(f"⚠️ {rel}: {why}")
        lines = [f"تم: {len(res.done)}", f"تعارضات (حُفظت النسختان): {len(res.conflicts)}", f"أخطاء: {len(res.errors)}"]
        if res.cancelled:
            title, box = "أُوقفت المزامنة", QMessageBox.warning
        elif res.errors:
            title, box = "اكتملت مع أخطاء", QMessageBox.warning
            lines.append("\nالملفات التي لم تُنقل ستُعاد محاولتها في المزامنة القادمة.")
        else:
            title, box = "اكتملت المزامنة", QMessageBox.information
            lines.append("\nيمكنك الآن إخراج الفلاش بأمان.")
        box(self, title, "\n".join(lines))
        self.load_sessions(select=plan.sid)
        self.start_scan()

    def on_failed(self, msg):
        self.set_busy(False, msg.splitlines()[0])
        self.log(msg)
        QMessageBox.warning(self, "SyncFile", msg)


def run():
    app = QApplication(sys.argv)
    app.setLayoutDirection(Qt.RightToLeft)
    app.setStyleSheet(STYLE)
    w = MainWindow()
    w.show()
    return app.exec()


def smoke_test():
    """للبناء التلقائي: يفتح الواجهة دون شاشة ويمر بالمسار الكامل على مجلدات مؤقتة."""
    import tempfile
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    tmp = Path(tempfile.mkdtemp())
    (tmp / "work" / "العمل").mkdir(parents=True)
    (tmp / "work" / "العمل" / "تقرير.txt").write_text("مرحبا", encoding="utf-8")
    app = QApplication.instance() or QApplication(sys.argv)
    app.setLayoutDirection(Qt.RightToLeft)
    app.setStyleSheet(STYLE)
    w = MainWindow(LocalState(tmp / "appdata"))
    w.set_carrier(tmp / "usb")
    sid = create_session(w.state, w.carrier, tmp / "work" / "العمل")
    w.load_sessions(select=sid)
    plan = build_plan(w.state, w.carrier, sid)
    w.show_plan(plan)
    assert w.tree.topLevelItemCount() == 1, "يجب أن يظهر الملف الجديد في المعاينة"
    res = execute(plan, w.state, w.carrier)
    w.on_progress(10.0, 10.0, "تقرير.txt")
    w.last_report, _ = write_reports(tmp / "reports", plan, res, "اختبار")
    assert not res.errors and (w.carrier.data_dir(sid) / "تقرير.txt").exists()
    w.show_plan(build_plan(w.state, w.carrier, sid))
    assert "متطابقة" in w.summary_lbl.text()
    w.close()
    print("SMOKE TEST OK")
    return 0
