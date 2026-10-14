"""
الواجهة العربية (PySide6 / Qt) — من اليمين لليسار، على طريقة GoodSync:

  • قائمة المهام يميناً، وزر «＋ مهمة جديدة» (مصدر + هدف + نوع المزامنة + الاستثناءات).
  • «تحليل» يعرض كل ملف واتجاهه قبل أي تنفيذ، ثم «مزامنة» تنفّذ.
  • الأسهم: ⬅ من المصدر إلى الهدف، ➡ من الهدف إلى المصدر، ✖ حذف، ⚠ يحتاج قرارك، ⊘ لن يُنسخ.
  • الزر الأيمن على أي ملف أو مجلد يغيّر الاتجاه.
  • الملفات المتعارضة لا تُنسخ حتى تختار في نافذة تعرض النسختين (التاريخ والحجم).
  • مزامنة تلقائية عند توصيل الفلاش، وتنظيف النسخ الاحتياطية الأقدم من 30 يوماً.
"""

import os
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar,
                               QPushButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from . import __version__
from .engine import (A2B, A_TO_B, B2A, B_TO_A, CHOOSE_A, CHOOSE_B, CONFLICT, DECIDE, DEL_A, DEL_B, EQUAL,
                     FORGET, KEEP_BOTH, MODES, NO_COPY, SKIP, TWO_WAY, SyncError, build_plan, choices_for,
                     execute, fmt_time, resolve_choice)
from .fsutil import QUARANTINE_DIR, cleanup_backups, lp, parse_excludes
from .meta import read_folder_id, relocate
from .report import write_reports
from .state import AppConfig

# الرمز، النص، اللون لكل إجراء (الأعمدة من اليمين: الملف | المصدر | الاتجاه | الهدف)
LOOK = {
    EQUAL:    ("＝", "متطابق", "#16a34a"),
    A2B:      ("⬅", "نسخ إلى الهدف", "#0284c7"),
    B2A:      ("➡", "نسخ إلى المصدر", "#7c3aed"),
    DEL_A:    ("✖", "حذف من المصدر", "#ea580c"),
    DEL_B:    ("✖", "حذف من الهدف", "#ea580c"),
    CONFLICT: ("⚠", "تعارض — اختر", "#dc2626"),
    DECIDE:   ("⚠", "التوقيت غير متوافق — اختر", "#dc2626"),
    SKIP:     ("⊘", "لن يُنسخ", "#64748b"),
    FORGET:   ("－", "حُذف من الطرفين", "#94a3b8"),
    "both":   ("⇄", "الاحتفاظ بالنسختين", "#0f766e"),
}

STYLE = """
QWidget { font-family: "Segoe UI", Tahoma; font-size: 10.5pt; }
QMainWindow, QWidget#root { background: #f1f5f9; }
QLabel#title { font-size: 14pt; font-weight: bold; color: #0f172a; }
QLabel#paths { color: #475569; }
QLabel#warn { background: #fef3c7; border: 1px solid #fcd34d; border-radius: 8px; padding: 6px 10px; color: #78350f; }
QLabel#summary { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 6px 10px; }
QPushButton { background: #e2e8f0; border: none; border-radius: 8px; padding: 8px 14px; }
QPushButton:hover { background: #cbd5e1; }
QPushButton:disabled { color: #94a3b8; background: #f1f5f9; }
QPushButton#primary { background: #0284c7; color: white; font-weight: bold; }
QPushButton#primary:hover { background: #0369a1; }
QPushButton#go { background: #16a34a; color: white; font-weight: bold; }
QPushButton#go:hover { background: #15803d; }
QPushButton#stop { background: #dc2626; color: white; }
QTreeWidget, QListWidget, QPlainTextEdit, QTableWidget { background: white; border: 1px solid #e2e8f0; border-radius: 8px; }
QListWidget::item { padding: 8px; border-bottom: 1px solid #f1f5f9; }
QListWidget::item:selected { background: #e0f2fe; color: #0c4a6e; }
QProgressBar { border: 1px solid #cbd5e1; border-radius: 6px; text-align: center; height: 18px; }
QProgressBar::chunk { background: #0284c7; border-radius: 6px; }
"""


def size_ar(n: float) -> str:
    for u in ("بايت", "كيلوبايت", "ميجابايت", "جيجابايت"):
        if n < 1024:
            return f"{int(n)} {u}" if u == "بايت" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} تيرابايت"


def side_text(f) -> str:
    return f"{fmt_time(f['mtime'])}  •  {size_ar(f['size'])}" if f else "—"


def effective(it, choices) -> str:
    """الإجراء الذي سيُنفَّذ فعلاً بعد قرار المستخدم (إن وجد)."""
    c = choices.get(it.key)
    return resolve_choice(it, c) if c is not None else it.action


def default_choice(it) -> str:
    if it.suggest:
        return it.suggest
    return KEEP_BOTH if (it.a and it.b) else NO_COPY


class Bridge(QObject):
    """إشارات من الخيط الخلفي إلى الواجهة (Qt ينقلها بأمان إلى الخيط الرئيسي)."""
    log = Signal(str)
    progress = Signal(float, float, str)
    plan_ready = Signal(object, bool)
    sync_done = Signal(object, object)
    failed = Signal(str)


# ───────────────────────────── نافذة المهمة ─────────────────────────────
class JobDialog(QDialog):
    def __init__(self, parent=None, job=None):
        super().__init__(parent)
        self.setWindowTitle("تعديل المهمة" if job else "مهمة جديدة")
        self.setLayoutDirection(Qt.RightToLeft)
        self.setMinimumWidth(620)
        job = job or {}
        form = QFormLayout(self)
        self.name = QLineEdit(job.get("name", ""))
        self.name.setPlaceholderText("مثال: العمل")
        self.a = QLineEdit(job.get("a", ""))
        self.b = QLineEdit(job.get("b", ""))
        for e in (self.a, self.b):
            e.setPlaceholderText("اختر مجلداً…")
        form.addRow("اسم المهمة:", self.name)
        form.addRow("المصدر (يمين):", self._with_browse(self.a))
        form.addRow("الهدف (يسار):", self._with_browse(self.b))
        self.mode = QComboBox()
        for k in (TWO_WAY, A_TO_B, B_TO_A):
            self.mode.addItem(MODES[k], k)
        self.mode.setCurrentIndex(max(0, self.mode.findData(job.get("mode", TWO_WAY))))
        form.addRow("نوع المزامنة:", self.mode)
        self.excl = QLineEdit(job.get("excludes", ""))
        self.excl.setPlaceholderText("مثال: *.bak; مؤقت; صور قديمة/*")
        form.addRow("استثناء:", self.excl)
        self.auto = QCheckBox("مزامنة تلقائية عند توصيل الفلاش أو ظهور المجلدين")
        self.auto.setChecked(bool(job.get("auto", False)))
        form.addRow("", self.auto)
        self.days = QSpinBox()
        self.days.setRange(0, 3650)
        self.days.setValue(int(job.get("keep_days", 30)))
        self.days.setSuffix(" يوماً")
        self.days.setSpecialValueText("للأبد")
        form.addRow(f"حفظ المحذوف في {QUARANTINE_DIR}:", self.days)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("حفظ")
        bb.button(QDialogButtonBox.Cancel).setText("إلغاء")
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _with_browse(self, edit):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(edit, 1)
        btn = QPushButton("استعراض…")
        btn.clicked.connect(lambda: self._browse(edit))
        h.addWidget(btn)
        return w

    def _browse(self, edit):
        d = QFileDialog.getExistingDirectory(self, "اختر المجلد", edit.text() or str(Path.home()))
        if d:
            edit.setText(os.path.normpath(d))
            if not self.name.text().strip():
                self.name.setText(Path(d).name)

    def values(self) -> dict:
        return {"name": self.name.text().strip() or Path(self.a.text().strip()).name,
                "a": os.path.normpath(self.a.text().strip()), "b": os.path.normpath(self.b.text().strip()),
                "mode": self.mode.currentData(), "excludes": self.excl.text().strip(),
                "auto": self.auto.isChecked(), "keep_days": self.days.value()}

    def _ok(self):
        v = self.values()
        if not self.a.text().strip() or not self.b.text().strip():
            QMessageBox.warning(self, "SyncFile", "اختر مجلد المصدر ومجلد الهدف.")
            return
        if os.path.normcase(os.path.abspath(v["a"])) == os.path.normcase(os.path.abspath(v["b"])):
            QMessageBox.warning(self, "SyncFile", "المصدر والهدف هما المجلد نفسه.")
            return
        self.accept()


# ───────────────────────────── نافذة القرار ─────────────────────────────
class DecisionDialog(QDialog):
    """تعرض النسختين لكل ملف يحتاج قرارك، مع تحديد الاقتراح مسبقاً."""

    def __init__(self, parent, plan, items, choices):
        super().__init__(parent)
        self.plan, self.items = plan, items
        self.setWindowTitle(f"قرارك مطلوب في {len(items)} ملف")
        self.setLayoutDirection(Qt.RightToLeft)
        self.resize(1000, 460)
        v = QVBoxLayout(self)
        hint = QLabel("هذه الملفات لن تُنسخ حتى تختار. الأكبر حجماً أو الأحدث مُعلَّم بالأخضر. "
                      "انقر مرتين على أي نسخة لفتحها والتأكد منها.")
        hint.setWordWrap(True)
        v.addWidget(hint)
        self.table = QTableWidget(len(items), 5)
        self.table.setHorizontalHeaderLabels(["الملف", "نسخة المصدر", "نسخة الهدف", "السبب", "قرارك"])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3, 4):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.cellDoubleClicked.connect(self._open)
        self.combos = []
        green = QBrush(QColor("#dcfce7"))
        for r, it in enumerate(items):
            self.table.setItem(r, 0, QTableWidgetItem(it.rel))
            ca, cb = QTableWidgetItem(side_text(it.a)), QTableWidgetItem(side_text(it.b))
            if it.suggest == CHOOSE_A:
                ca.setBackground(green)
            elif it.suggest == CHOOSE_B:
                cb.setBackground(green)
            self.table.setItem(r, 1, ca)
            self.table.setItem(r, 2, cb)
            self.table.setItem(r, 3, QTableWidgetItem(it.note or it.label))
            combo = QComboBox()
            for val, text in choices_for(it):
                combo.addItem(text, val)
            combo.setCurrentIndex(max(0, combo.findData(choices.get(it.key, default_choice(it)))))
            self.table.setCellWidget(r, 4, combo)
            self.combos.append(combo)
        v.addWidget(self.table, 1)
        row = QHBoxLayout()
        for text, fn in (("الكل: المقترح", lambda: self.set_all(None)),
                         ("الكل: الاحتفاظ بالنسختين", lambda: self.set_all(KEEP_BOTH)),
                         ("الكل: لا تنسخ الآن", lambda: self.set_all(NO_COPY))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("اعتماد والمزامنة")
        bb.button(QDialogButtonBox.Cancel).setText("رجوع")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        row.addWidget(bb)
        v.addLayout(row)

    def set_all(self, value):
        for it, combo in zip(self.items, self.combos):
            combo.setCurrentIndex(max(0, combo.findData(value or default_choice(it))))

    def _open(self, r, c):
        it = self.items[r]
        f, root = (it.a, self.plan.a_root) if c == 1 else (it.b, self.plan.b_root) if c == 2 else (None, None)
        if f:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(root / f["rel"])))

    def result_choices(self) -> dict:
        return {it.key: combo.currentData() for it, combo in zip(self.items, self.combos)}


# ───────────────────────────── النافذة الرئيسية ─────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig = None, quiet=False):
        super().__init__()
        self.cfg = config or AppConfig()
        self.quiet = quiet                    # للاختبار الآلي: بلا نوافذ رسائل
        self.job = None
        self.plan = None
        self.choices = {}
        self.rows = {}                        # المفتاح ← عنصر الشجرة
        self.busy = False
        self.cancel = threading.Event()
        self.last_report = None
        self.available = {}                   # هوية المهمة ← هل المجلدان متاحان (للمزامنة التلقائية)
        self.auto_running = False
        self._speed = (time.time(), 0)

        self.setWindowTitle(f"SyncFile {__version__} — مزامنة المجلدات بدون إنترنت")
        self.setLayoutDirection(Qt.RightToLeft)
        self.resize(1250, 760)
        self.bridge = Bridge()
        self.bridge.log.connect(self.log)
        self.bridge.progress.connect(self.on_progress)
        self.bridge.plan_ready.connect(self.on_plan)
        self.bridge.sync_done.connect(self.on_synced)
        self.bridge.failed.connect(self.on_failed)
        self._build()
        self.load_jobs()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.watch)
        self.timer.start(3000)
        QTimer.singleShot(500, self.cleanup_all)

    # ---------- البناء ----------
    def _build(self):
        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        split = QSplitter(Qt.Horizontal)
        outer.addWidget(split)

        # قائمة المهام
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        t = QLabel("المهام")
        t.setObjectName("title")
        lv.addWidget(t)
        self.jobs = QListWidget()
        self.jobs.currentItemChanged.connect(self.on_job_selected)
        self.jobs.itemDoubleClicked.connect(lambda _: self.edit_job())
        lv.addWidget(self.jobs, 1)
        self.new_btn = QPushButton("＋ مهمة جديدة", objectName="primary")
        self.new_btn.clicked.connect(self.new_job)
        lv.addWidget(self.new_btn)
        h = QHBoxLayout()
        self.edit_btn = QPushButton("تعديل")
        self.edit_btn.clicked.connect(self.edit_job)
        self.del_btn = QPushButton("حذف")
        self.del_btn.clicked.connect(self.delete_job)
        h.addWidget(self.edit_btn)
        h.addWidget(self.del_btn)
        lv.addLayout(h)
        dev = QLabel(f"الجهاز: {self.cfg.device_name}")
        dev.setObjectName("paths")
        lv.addWidget(dev)
        split.addWidget(left)

        # منطقة المهمة
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.title_lbl = QLabel("اختر مهمة أو أنشئ واحدة جديدة")
        self.title_lbl.setObjectName("title")
        rv.addWidget(self.title_lbl)
        self.paths_lbl = QLabel("")
        self.paths_lbl.setObjectName("paths")
        self.paths_lbl.setTextFormat(Qt.PlainText)
        rv.addWidget(self.paths_lbl)

        bar = QHBoxLayout()
        self.analyze_btn = QPushButton("🔍 تحليل", objectName="primary")
        self.analyze_btn.clicked.connect(self.start_analyze)
        self.sync_btn = QPushButton("⇄ مزامنة", objectName="go")
        self.sync_btn.clicked.connect(self.start_sync)
        self.stop_btn = QPushButton("إيقاف", objectName="stop")
        self.stop_btn.clicked.connect(lambda: self.cancel.set())
        self.report_btn = QPushButton("آخر تقرير")
        self.report_btn.clicked.connect(self.open_report)
        self.open_a = QPushButton("فتح المصدر")
        self.open_a.clicked.connect(lambda: self.open_folder("a"))
        self.open_b = QPushButton("فتح الهدف")
        self.open_b.clicked.connect(lambda: self.open_folder("b"))
        for b in (self.analyze_btn, self.sync_btn, self.stop_btn):
            bar.addWidget(b)
        bar.addStretch(1)
        self.show_equal = QCheckBox("إظهار المتطابقة")
        self.show_equal.toggled.connect(lambda _: self.plan and self.show_plan(self.plan))
        bar.addWidget(self.show_equal)
        for b in (self.open_a, self.open_b, self.report_btn):
            bar.addWidget(b)
        rv.addLayout(bar)

        self.summary_lbl = QLabel("")
        self.summary_lbl.setObjectName("summary")
        self.summary_lbl.setWordWrap(True)
        rv.addWidget(self.summary_lbl)
        self.warn_lbl = QLabel("")
        self.warn_lbl.setObjectName("warn")
        self.warn_lbl.setWordWrap(True)
        self.warn_lbl.hide()
        rv.addWidget(self.warn_lbl)

        vsplit = QSplitter(Qt.Vertical)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["الملف", "المصدر (التاريخ • الحجم)", "الاتجاه", "الهدف (التاريخ • الحجم)", "ملاحظة"])
        hh = self.tree.header()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.context_menu)
        self.tree.setUniformRowHeights(True)
        vsplit.addWidget(self.tree)
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(3000)
        vsplit.addWidget(self.log_box)
        vsplit.setSizes([520, 140])
        rv.addWidget(vsplit, 1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        rv.addWidget(self.progress)
        self.speed_lbl = QLabel("")
        self.speed_lbl.setObjectName("paths")
        rv.addWidget(self.speed_lbl)
        split.addWidget(right)
        split.setSizes([260, 990])
        self.set_busy(False)

    # ---------- أدوات ----------
    def log(self, msg: str):
        self.log_box.appendPlainText(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def notify(self, kind, title, text):
        self.log(f"{title}: {text}".replace("\n", " "))
        if not self.quiet:
            {"info": QMessageBox.information, "warn": QMessageBox.warning}[kind](self, title, text)

    def ask(self, title, text) -> bool:
        if self.quiet:
            return True
        return QMessageBox.question(self, title, text) == QMessageBox.Yes

    def set_busy(self, busy, status=""):
        self.busy = busy
        has = self.job is not None
        self.analyze_btn.setEnabled(has and not busy)
        self.sync_btn.setEnabled(has and not busy)
        self.stop_btn.setEnabled(busy)
        for b in (self.new_btn, self.edit_btn, self.del_btn, self.jobs):
            b.setEnabled(not busy)
        for b in (self.edit_btn, self.del_btn, self.open_a, self.open_b):
            b.setEnabled(has and not busy)
        self.report_btn.setEnabled(self.last_report is not None)
        if status:
            self.speed_lbl.setText(status)

    # ---------- المهام ----------
    def load_jobs(self, select=None):
        self.jobs.blockSignals(True)
        self.jobs.clear()
        current = None
        for p in self.cfg.pairs:
            mode = {TWO_WAY: "⇄", A_TO_B: "⬅", B_TO_A: "➡"}.get(p.get("mode"), "⇄")
            last = p.get("last") or "لم تُزامَن بعد"
            it = QListWidgetItem(f"{mode}  {p['name']}\n{last}")
            it.setData(Qt.UserRole, p["id"])
            if p.get("auto"):
                it.setToolTip("مزامنة تلقائية عند توصيل الفلاش")
            self.jobs.addItem(it)
            if p["id"] == select:
                current = it
        self.jobs.blockSignals(False)
        if current is None and self.jobs.count() and select is None and self.job:
            select = self.job["id"]
            current = next((self.jobs.item(i) for i in range(self.jobs.count())
                            if self.jobs.item(i).data(Qt.UserRole) == select), None)
        if current is None and self.jobs.count():
            current = self.jobs.item(0)
        if current is not None:
            self.jobs.setCurrentItem(current)
        self.on_job_selected(current, None)

    def on_job_selected(self, cur, _prev=None):
        pid = cur.data(Qt.UserRole) if cur is not None else None
        self.job = self.cfg.get(pid) if pid else None
        self.plan = None
        self.choices = {}
        self.tree.clear()
        self.rows = {}
        self.warn_lbl.hide()
        if not self.job:
            self.title_lbl.setText("اختر مهمة أو أنشئ واحدة جديدة")
            self.paths_lbl.setText("")
            self.summary_lbl.setText("أنشئ مهمة: اختر مجلد المصدر ومجلد الهدف (في الجهاز نفسه أو على الفلاش)، "
                                     "ثم اضغط «تحليل».")
        else:
            j = self.job
            self.title_lbl.setText(f"{j['name']}  —  {MODES.get(j.get('mode'), MODES[TWO_WAY])}")
            ex = f"   •   استثناء: {j['excludes']}" if j.get("excludes") else ""
            self.paths_lbl.setText(f"المصدر: {j['a']}\nالهدف:  {j['b']}{ex}")
            self.summary_lbl.setText("اضغط «تحليل» لمعرفة ما سيتغير قبل المزامنة.")
        self.set_busy(self.busy)

    def new_job(self):
        d = JobDialog(self)
        if d.exec():
            v = d.values()
            p = self.cfg.add_pair(v.pop("a"), v.pop("b"), v.pop("name"), **v)
            self.log(f"أُنشئت المهمة «{p['name']}»")
            self.load_jobs(select=p["id"])
            self.start_analyze()

    def edit_job(self):
        if not self.job or self.busy:
            return
        d = JobDialog(self, self.job)
        if d.exec():
            v = d.values()
            if (v["a"], v["b"]) != (self.job["a"], self.job["b"]):
                v.update(a_id="", b_id="")
            self.cfg.update(self.job["id"], **v)
            self.load_jobs(select=self.job["id"])

    def delete_job(self):
        if not self.job or self.busy:
            return
        if self.ask("حذف المهمة", f"حذف المهمة «{self.job['name']}» من القائمة؟\n"
                                  "لن يُحذف أي ملف من المجلدين."):
            self.cfg.remove(self.job["id"])
            self.job = None
            self.load_jobs()

    def resolve_paths(self, job):
        """يعيد (المصدر، الهدف) بعد البحث عن الفلاش إذا تغيّر حرفه."""
        out = []
        for side in ("a", "b"):
            path = job[side]
            if not os.path.isdir(lp(path)) and job.get(f"{side}_id"):
                found = relocate(path, job[f"{side}_id"])
                if found:
                    self.log(f"تغيّر حرف القرص: {path} ← {found}")
                    self.cfg.update(job["id"], **{side: found})
                    path = found
            out.append(path)
        return out

    def open_folder(self, side):
        if self.job:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.job[side]))

    def open_report(self):
        if self.last_report:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_report)))

    # ---------- التحليل ----------
    def start_analyze(self, auto=False):
        if not self.job or self.busy:
            return
        job = self.job
        a, b = self.resolve_paths(job)
        self.on_job_selected(self.jobs.currentItem())
        self.cancel.clear()
        self.set_busy(True, "جارٍ التحليل…")
        self.progress.setRange(0, 0)
        self.log(f"تحليل «{job['name']}»…")
        mode, excl = job.get("mode", TWO_WAY), parse_excludes(job.get("excludes", ""))

        def work():
            try:
                plan = build_plan(a, b, self.cancel, mode, excl)
                self.bridge.plan_ready.emit(plan, auto)
            except InterruptedError:
                self.bridge.failed.emit("أُوقف التحليل")
            except SyncError as e:
                self.bridge.failed.emit(str(e))
            except Exception as e:
                self.bridge.failed.emit(f"تعذر التحليل: {e}")

        threading.Thread(target=work, daemon=True).start()

    def on_plan(self, plan, auto=False):
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.set_busy(False, "")
        self.show_plan(plan)
        for rel, why in plan.errors:
            self.log(f"⚠️ {rel}: {why}")
        if auto:
            self.auto_continue()

    def summary_text(self, plan) -> str:
        if not plan.changes:
            return f"✅ كل الملفات متطابقة ({len(plan.items)} ملف). لا يوجد ما يُزامَن."
        parts = []
        for label, n in (("جديد", plan.count_reason("new")), ("معدّل", plan.count_reason("modified")),
                         ("حذف", plan.count(DEL_A, DEL_B)), ("يحتاج قرارك", len(plan.decisions)),
                         ("متوقف", sum(1 for i in plan.items if i.blocked)),
                         ("لن يُنسخ", plan.count(SKIP)), ("متطابق", plan.count(EQUAL))):
            if n:
                parts.append(f"{label}: {n}")
        head = "أول مزامنة لهذين المجلدين — " if plan.first_sync else ""
        return f"{head}{'   •   '.join(parts)}   •   حجم النقل: {size_ar(plan.transfer_bytes)}"

    def show_plan(self, plan):
        self.plan = plan
        self.tree.clear()
        self.rows = {}
        folders = {}
        show_eq = self.show_equal.isChecked()
        bold = QFont()
        bold.setBold(True)

        def folder_node(path):
            if not path:
                return None
            if path in folders:
                return folders[path]
            parent_path, name = os.path.split(path)
            parent = folder_node(parent_path)
            node = QTreeWidgetItem([f"📁 {name}", "", "", "", ""])
            node.setData(0, Qt.UserRole, ("dir", path))
            node.setFont(0, bold)
            (parent.addChild(node) if parent else self.tree.addTopLevelItem(node))
            folders[path] = node
            return node

        for it in plan.items:
            if it.action == EQUAL and not show_eq and it.key not in self.choices:
                continue
            if it.action == FORGET:
                continue
            folder, name = os.path.split(it.rel)
            node = QTreeWidgetItem([name, "", "", "", ""])
            node.setData(0, Qt.UserRole, ("file", it.key))
            node.setToolTip(0, it.rel)
            parent = folder_node(folder.replace("\\", "/"))
            (parent.addChild(node) if parent else self.tree.addTopLevelItem(node))
            self.rows[it.key] = (node, it)
            self.paint(it)
        self.tree.expandAll()
        self.summary_lbl.setText(self.summary_text(plan))
        if plan.warnings:
            self.warn_lbl.setText("\n".join(plan.warnings))
            self.warn_lbl.show()
        else:
            self.warn_lbl.hide()

    def paint(self, it):
        node = self.rows[it.key][0]
        act = effective(it, self.choices)
        sym, text, color = LOOK.get(act, ("?", act, "#000"))
        manual = it.key in self.choices
        if it.blocked:
            sym, text, color = "⛔", "متوقف", "#b91c1c"
        node.setText(1, side_text(it.a))
        node.setText(2, f"{sym}  {text}" + ("  (يدوي)" if manual else ""))
        node.setText(3, side_text(it.b))
        node.setText(4, it.blocked or it.note)
        brush = QBrush(QColor(color))
        node.setForeground(2, brush)
        f = node.font(2)
        f.setBold(act != EQUAL)
        f.setItalic(manual)
        node.setFont(2, f)
        if it.needs_choice and not manual and not it.blocked:
            for c in range(5):
                node.setBackground(c, QBrush(QColor("#fef2f2")))
        else:
            for c in range(5):
                node.setBackground(c, QBrush())

    # ---------- الزر الأيمن ----------
    def set_choice(self, key, choice):
        """None = إرجاع الملف لاتجاهه المقترح."""
        if key not in self.rows:
            return
        if choice is None:
            self.choices.pop(key, None)
        else:
            self.choices[key] = choice
        self.paint(self.rows[key][1])

    def items_under(self, path):
        pre = path.rstrip("/") + "/"
        return [it for _, it in self.rows.values() if it.rel.replace("\\", "/").startswith(pre)]

    def context_menu(self, pos):
        node = self.tree.itemAt(pos)
        if node is None or self.busy or not self.plan:
            return
        kind, ref = node.data(0, Qt.UserRole)
        menu = QMenu(self)
        menu.setLayoutDirection(Qt.RightToLeft)
        if kind == "file":
            it = self.rows[ref][1]
            if it.blocked:
                menu.addAction(f"⛔ {it.blocked}").setEnabled(False)
            else:
                for val, text in choices_for(it):
                    act = resolve_choice(it, val)
                    sym = LOOK.get(act, ("", "", ""))[0]
                    menu.addAction(f"{sym}  {text}").triggered.connect(lambda _=False, v=val: self.set_choice(ref, v))
                menu.addSeparator()
                menu.addAction("↺  الاتجاه المقترح").triggered.connect(lambda: self.set_choice(ref, None))
                menu.addSeparator()
                if it.a:
                    menu.addAction("فتح نسخة المصدر").triggered.connect(
                        lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.plan.a_root / it.a["rel"]))))
                if it.b:
                    menu.addAction("فتح نسخة الهدف").triggered.connect(
                        lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.plan.b_root / it.b["rel"]))))
        else:
            items = [i for i in self.items_under(ref) if i.action != EQUAL and not i.blocked]
            menu.addAction(f"📁 {ref} — {len(items)} ملف").setEnabled(False)
            for val, text in ((CHOOSE_A, "⬅  اجعل الهدف مثل المصدر"), (CHOOSE_B, "➡  اجعل المصدر مثل الهدف"),
                              (NO_COPY, "⊘  لا تنسخ هذا المجلد الآن"), (None, "↺  الاتجاه المقترح")):
                menu.addAction(text).triggered.connect(
                    lambda _=False, v=val, its=items: [self.set_choice(i.key, v) for i in its])
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    # ---------- المزامنة ----------
    def pending_decisions(self):
        return [i for i in self.plan.items if i.needs_choice and not i.blocked and i.key not in self.choices]

    def start_sync(self):
        if not self.job or self.busy:
            return
        if self.plan is None:
            self.notify("info", "SyncFile", "اضغط «تحليل» أولاً لترى ما سيتغير.")
            return
        if not self.plan.changes and not self.choices:
            self.notify("info", "SyncFile", "كل الملفات متطابقة، لا يوجد ما يُزامَن.")
            return
        pending = self.pending_decisions()
        if pending:
            if self.quiet:
                for it in pending:
                    self.choices[it.key] = default_choice(it)
            else:
                d = DecisionDialog(self, self.plan, pending, self.choices)
                if not d.exec():
                    return
                self.choices.update(d.result_choices())
            for it in pending:
                self.paint(it)
        if self.plan.mass_delete and not self.ask(
                "تأكيد الحذف", "ستحذف المزامنة عدداً كبيراً من الملفات (تُنقل إلى النسخ الاحتياطية).\n"
                               "هل أنت متأكد أن هذا مقصود؟"):
            return
        self.run_sync()

    def run_sync(self):
        plan, choices, device = self.plan, dict(self.choices), self.cfg.device_name
        self.cancel.clear()
        self._speed = (time.time(), 0)
        self.set_busy(True, "جارٍ المزامنة…")
        self.log(f"مزامنة «{self.job['name']}»…")

        def work():
            try:
                res = execute(plan, choices, device, self.cancel,
                              lambda done, total, rel: self.bridge.progress.emit(float(done), float(total), rel))
                self.bridge.sync_done.emit(plan, res)
            except Exception as e:
                self.bridge.failed.emit(f"تعذرت المزامنة: {e}")

        threading.Thread(target=work, daemon=True).start()

    def on_progress(self, done, total, rel):
        self.progress.setValue(int(done / max(total, 1) * 1000))
        t0, _ = self._speed
        speed = done / max(time.time() - t0, 0.001)
        self.speed_lbl.setText(f"{size_ar(done)} من {size_ar(total)}  •  {size_ar(speed)}/ث  •  {Path(rel).name}")

    def finish_sync(self, plan, res) -> str:
        """يحفظ نتيجة المزامنة (التقرير، هوية المجلدين، التنظيف) ويعيد ملخصاً."""
        job = self.job
        try:
            self.last_report, _ = write_reports(self.cfg.home / "reports", plan, res, self.cfg.device_name,
                                                job["name"] if job else "")
        except OSError as e:
            self.log(f"تعذرت كتابة التقرير: {e}")
        for rel, what in res.done:
            self.log(f"{what}: {rel}")
        for rel, why in res.errors:
            self.log(f"⚠️ {rel}: {why}")
        if job:
            self.cfg.update(job["id"], a_id=read_folder_id(plan.a_root), b_id=read_folder_id(plan.b_root),
                            last=f"آخر مزامنة: {time.strftime('%Y-%m-%d %H:%M')}")
            days = int(job.get("keep_days", 30))
            n = cleanup_backups(plan.a_root, days) + cleanup_backups(plan.b_root, days)
            if n:
                self.log(f"نُظّفت {n} نسخة احتياطية أقدم من {days} يوماً")
        self.progress.setValue(1000 if not res.cancelled else self.progress.value())
        return "\n".join([f"تم: {len(res.done)}", f"حُفظت فيها النسختان: {len(res.kept_both)}",
                          f"أخطاء: {len(res.errors)}", f"نُقل: {size_ar(res.bytes)}"])

    def on_synced(self, plan, res):
        self.set_busy(False)
        text = self.finish_sync(plan, res)
        auto = self.auto_running
        self.auto_running = False
        if res.cancelled:
            self.notify("warn", "أُوقفت المزامنة", text)
        elif res.errors:
            self.notify("warn", "اكتملت مع أخطاء", text + "\n\nالملفات التي لم تُنقل ستُعاد في المزامنة القادمة.")
        elif auto:
            self.log("✅ اكتملت المزامنة التلقائية — " + text.replace("\n", "، "))
        else:
            self.notify("info", "اكتملت المزامنة", text + "\n\nيمكنك الآن إخراج الفلاش بأمان.")
        self.load_jobs(select=self.job["id"] if self.job else None)
        self.start_analyze()

    def on_failed(self, msg):
        self.progress.setRange(0, 1000)
        self.auto_running = False
        self.set_busy(False, msg.splitlines()[0])
        self.notify("warn", "SyncFile", msg)

    # ---------- التلقائي ----------
    def job_available(self, job) -> bool:
        for side in ("a", "b"):
            if os.path.isdir(lp(job[side])):
                continue
            if not (job.get(f"{side}_id") and relocate(job[side], job[f"{side}_id"])):
                return False
        return True

    def watch(self):
        """كل 3 ثوانٍ: إذا ظهر الفلاش لمهمة تلقائية تُحلَّل وتُزامَن مباشرة."""
        for job in self.cfg.pairs:
            if not job.get("auto"):
                continue
            now = self.job_available(job)
            was = self.available.get(job["id"])
            self.available[job["id"]] = now
            if now and was is False and not self.busy:
                self.log(f"🔌 ظهر مجلدا «{job['name']}» — مزامنة تلقائية")
                self.load_jobs(select=job["id"])
                self.auto_running = True
                self.start_analyze(auto=True)
                return

    def auto_continue(self):
        """بعد التحليل التلقائي: يُزامن إلا إذا احتاج الأمر قراراً أو كان الحذف كبيراً."""
        plan = self.plan
        if plan is None or not plan.changes:
            self.auto_running = False
            self.log("المزامنة التلقائية: كل الملفات متطابقة")
            return
        if self.pending_decisions() or plan.mass_delete:
            self.auto_running = False
            self.raise_()
            self.activateWindow()
            self.notify("warn", "المزامنة التلقائية تنتظرك",
                        "توجد ملفات تحتاج قرارك أو حذف كبير — راجع القائمة ثم اضغط «مزامنة».")
            return
        self.run_sync()

    def cleanup_all(self):
        for job in self.cfg.pairs:
            for side in ("a", "b"):
                if os.path.isdir(lp(job[side])):
                    try:
                        cleanup_backups(Path(job[side]), int(job.get("keep_days", 30)))
                    except OSError:
                        pass


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
    src, dst = tmp / "PC" / "العمل", tmp / "USB" / "العمل"
    (src / "المكتب").mkdir(parents=True)
    dst.mkdir(parents=True)
    for i in range(1, 5):
        (src / f"خطاب {i}.txt").write_text(f"خطاب {i}", encoding="utf-8")
    for i in range(1, 4):
        (dst / f"خطاب {i}.txt").write_text(f"خطاب {i}", encoding="utf-8")
    (src / "خطاب 2.txt").write_text("خطاب 2 بعد التعديل والإضافة", encoding="utf-8")
    old = time.time() - 3600
    os.utime(dst / "خطاب 2.txt", (old, old))
    (src / "المكتب" / "ملاحظات.txt").write_text("ملاحظات", encoding="utf-8")
    (src / "نسخة.bak").write_text("مستثنى", encoding="utf-8")

    app = QApplication.instance() or QApplication(sys.argv)
    app.setLayoutDirection(Qt.RightToLeft)
    app.setStyleSheet(STYLE)
    cfg = AppConfig(tmp / "appdata")
    job = cfg.add_pair(src, dst, "العمل", excludes="*.bak", auto=True)
    w = MainWindow(cfg, quiet=True)
    w.load_jobs(select=job["id"])
    assert w.job and w.job["id"] == job["id"], "يجب أن تُختار المهمة"
    JobDialog(w, job).values()

    plan = build_plan(src, dst, None, job["mode"], parse_excludes(job["excludes"]))
    w.on_plan(plan)
    assert set(w.rows) == {"خطاب 2.txt", "خطاب 4.txt", "المكتب/ملاحظات.txt"}, sorted(w.rows)
    assert effective(plan.items[0], w.choices) is not None
    key = "خطاب 4.txt"
    w.set_choice(key, NO_COPY)
    assert effective(w.rows[key][1], w.choices) == SKIP
    w.set_choice(key, None)
    assert "جديد" in w.summary_lbl.text() or w.summary_text(plan)
    DecisionDialog(w, plan, plan.items[:1], {}).result_choices()

    res = execute(plan, w.choices, cfg.device_name)
    w.on_progress(10.0, 10.0, "خطاب 4.txt")
    w.finish_sync(plan, res)
    assert not res.errors, res.errors
    assert (dst / "المكتب" / "ملاحظات.txt").exists() and (dst / "خطاب 4.txt").exists()
    assert not (dst / "نسخة.bak").exists(), "الاستثناء لم يُطبَّق"
    assert cfg.get(job["id"])["a_id"], "يجب حفظ هوية المجلد بعد المزامنة"
    assert w.last_report and Path(w.last_report).exists()

    again = build_plan(src, dst, None, job["mode"], parse_excludes(job["excludes"]))
    w.show_plan(again)
    assert not again.changes and "متطابقة" in w.summary_text(again)
    w.watch()
    w.close()
    print("SMOKE TEST OK")
    return 0
