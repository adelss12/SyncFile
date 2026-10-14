"""
الواجهة العربية (PySide6 / Qt) — من اليمين لليسار، بتصميم GoodSync:

  ┌ المهام ┐┌ [تحليل] [مزامنة] [إيقاف]                         [خيارات] [تلقائي] ┐
  │ العمل  ││ المصدر: [مسار…] [تغيير المجلد]   ⇄   الهدف: [مسار…] [تغيير المجلد]    │
  │        ││ محلي C: NTFS • المتاح • الإجمالي • المطلوب   …   (نفسها للهدف)          │
  │        ││ ⚠ يوجد تعارض: [المصدر يفوز] [الهدف يفوز] [الأحدث يفوز] [النسختان]      │
  │        ││ الأخطاء | التعارضات | ⬅ | ➡ | التغييرات | حذف… | الكل                  │
  │        ││ الملف | حجم وتاريخ المصدر | الاتجاه | حجم وتاريخ الهدف | الحالة         │
  └────────┘└──────────────────────────────────────────────────────────────────┘
"""

import os
import shutil
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import __version__
from .engine import (A2B, A_TO_B, B2A, B_TO_A, CHOOSE_A, CHOOSE_B, CONFLICT, DECIDE, DEL_A, DEL_B, EQUAL,
                     FORGET, KEEP_BOTH, MODES, NO_COPY, SKIP, TWO_WAY, SyncError, build_plan, choices_for,
                     execute, fmt_time, resolve_choice)
from .fsutil import QUARANTINE_DIR, cleanup_backups, filesystem_of, lp, parse_excludes
from .meta import read_folder_id, relocate
from .report import write_reports
from .state import AppConfig

# الرمز، النص، اللون لكل إجراء (المصدر يمين والهدف يسار، فالنسخ إلى الهدف سهمه ⬅)
LOOK = {
    EQUAL:    ("＝", "متطابق", "#16a34a"),
    A2B:      ("⬅", "نسخ إلى الهدف", "#0284c7"),
    B2A:      ("➡", "نسخ إلى المصدر", "#7c3aed"),
    DEL_A:    ("✖", "حذف من المصدر", "#ea580c"),
    DEL_B:    ("✖", "حذف من الهدف", "#ea580c"),
    CONFLICT: ("❓", "تعارض", "#dc2626"),
    DECIDE:   ("❓", "التوقيت غير متوافق", "#dc2626"),
    SKIP:     ("⊘", "لن يُنسخ", "#64748b"),
    FORGET:   ("－", "حُذف من الطرفين", "#94a3b8"),
    "both":   ("⇄", "الاحتفاظ بالنسختين", "#0f766e"),
}
MODE_ARROW = {TWO_WAY: "⇄", A_TO_B: "⬅", B_TO_A: "➡"}
MODE_SHORT = {TWO_WAY: "في الاتجاهين", A_TO_B: "من المصدر إلى الهدف", B_TO_A: "من الهدف إلى المصدر"}
SRC_BG, DST_BG = "#eef2ff", "#f0fdf4"          # خلفية أعمدة المصدر والهدف كما في GoodSync

# أعمدة الشجرة
C_NAME, C_ASIZE, C_ADATE, C_DIR, C_BSIZE, C_BDATE, C_STATUS = range(7)

FILTERS = [  # (المفتاح، النص)
    ("errors", "⛔ الأخطاء"), ("conflicts", "⚠ التعارضات"), ("to_b", "⬅ إلى الهدف"), ("to_a", "➡ إلى المصدر"),
    ("changes", "⇄ التغييرات"), ("del_a", "✖ حذف من المصدر"), ("del_b", "✖ حذف من الهدف"), ("all", "☰ الكل"),
]

STYLE = """
QWidget { font-family: "Segoe UI", Tahoma; font-size: 10.5pt; }
QMainWindow, QWidget#root { background: #f8fafc; }
QLabel#title { font-size: 13pt; font-weight: bold; color: #0f172a; }
QLabel#muted, QLabel#info { color: #64748b; font-size: 9pt; }
QLabel#side { color: #0369a1; font-weight: bold; }
QFrame#folders { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; }
QFrame#conflict { background: #fef9c3; border: 1px solid #fde047; border-radius: 8px; }
QLabel#warn { background: #fff7ed; border: 1px solid #fed7aa; border-radius: 8px; padding: 6px 10px; color: #7c2d12; }
QLineEdit#path { font-size: 11pt; padding: 6px; border: 1px solid #cbd5e1; border-radius: 6px; background: #fff; }
QPushButton { background: #e2e8f0; border: none; border-radius: 8px; padding: 7px 14px; }
QPushButton:hover { background: #cbd5e1; }
QPushButton:disabled { color: #94a3b8; background: #f1f5f9; }
QPushButton#tool { background: transparent; font-size: 11pt; padding: 6px 12px; }
QPushButton#tool:hover { background: #e0f2fe; }
QPushButton#primary { background: #0284c7; color: white; font-weight: bold; }
QPushButton#primary:hover { background: #0369a1; }
QPushButton#go { background: #16a34a; color: white; font-weight: bold; }
QPushButton#go:hover { background: #15803d; }
QPushButton#stop { background: #dc2626; color: white; }
QPushButton#mode { background: #f1f5f9; border: 1px solid #cbd5e1; border-radius: 22px; font-size: 18pt; padding: 2px 10px; min-width: 44px; }
QPushButton#mode:hover { background: #e0f2fe; }
QPushButton#win { background: transparent; color: #15803d; font-weight: bold; padding: 4px 10px; }
QPushButton#win:hover { background: #fef08a; }
QPushButton#filter { background: transparent; padding: 5px 10px; border-radius: 6px; }
QPushButton#filter:checked { background: #dbeafe; color: #1e3a8a; font-weight: bold; }
QPushButton#filter:hover { background: #eff6ff; }
QTreeWidget, QListWidget, QPlainTextEdit, QTableWidget { background: white; border: 1px solid #e2e8f0; border-radius: 8px; }
QHeaderView::section { background: #f1f5f9; border: none; border-bottom: 1px solid #e2e8f0; padding: 5px; font-weight: bold; }
QListWidget::item { padding: 8px; border-bottom: 1px solid #f1f5f9; }
QListWidget::item:selected { background: #e0f2fe; color: #0c4a6e; }
QProgressBar { border: 1px solid #cbd5e1; border-radius: 6px; text-align: center; height: 16px; }
QProgressBar::chunk { background: #0284c7; border-radius: 6px; }
"""


def size_ar(n: float) -> str:
    for u in ("بايت", "ك.ب", "م.ب", "ج.ب"):
        if n < 1024:
            return f"{int(n)} {u}" if u == "بايت" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} ت.ب"


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


def newer_choice(it) -> str:
    if it.a and it.b:
        return CHOOSE_A if it.a["mtime"] >= it.b["mtime"] else CHOOSE_B
    return CHOOSE_A if it.a else CHOOSE_B


def in_filter(key, it, act, manual=False) -> bool:
    if act == FORGET:
        return False
    return {
        "errors": bool(it.blocked),
        "conflicts": it.needs_choice,
        "to_b": act in (A2B, "both"),
        "to_a": act == B2A,
        "changes": act not in (EQUAL, SKIP) or manual or bool(it.blocked),
        "del_a": act == DEL_A,
        "del_b": act == DEL_B,
        "all": True,
    }[key]


def drive_kind(drive: str) -> str:
    if os.name != "nt" or not drive:
        return "محلي"
    try:
        import ctypes
        t = ctypes.windll.kernel32.GetDriveTypeW(drive + "\\")
        return {2: "فلاش", 3: "محلي", 4: "شبكة", 5: "قرص مضغوط"}.get(t, "محلي")
    except (OSError, AttributeError):
        return "محلي"


def drive_info(path: str, required: int = 0) -> str:
    """«محلي C: NTFS • المتاح … • الإجمالي … • المطلوب …» مثل شريط GoodSync."""
    if not path:
        return "لم يُختر مجلد بعد"
    p = os.path.abspath(path)
    while not os.path.isdir(lp(p)):
        parent = os.path.dirname(p)
        if parent == p:
            return "⚠ القرص غير موجود — هل الفلاش موصول؟"
        p = parent
    try:
        u = shutil.disk_usage(lp(p))
    except OSError:
        return ""
    drive = os.path.splitdrive(p)[0]
    fs = filesystem_of(Path(p))
    head = " ".join(x for x in (drive_kind(drive), drive, fs) if x)
    txt = f"{head}  •  المتاح: {size_ar(u.free)}  •  الإجمالي: {size_ar(u.total)}  •  المطلوب: {size_ar(required)}"
    if not os.path.isdir(lp(path)):
        txt += "  •  المجلد غير موجود وسيُنشأ"
    elif required > u.free:
        txt += "  •  ⚠ المساحة لا تكفي"
    return txt


class Bridge(QObject):
    """إشارات من الخيط الخلفي إلى الواجهة (Qt ينقلها بأمان إلى الخيط الرئيسي)."""
    log = Signal(str)
    progress = Signal(float, float, str)
    plan_ready = Signal(object, bool)
    sync_done = Signal(object, object)
    failed = Signal(str)


# ───────────────────────────── خيارات المهمة ─────────────────────────────
class JobDialog(QDialog):
    """اسم المهمة ونوعها والاستثناءات (المجلدان يُختاران في الشريط العلوي للنافذة)."""

    def __init__(self, parent=None, job=None):
        super().__init__(parent)
        self.setWindowTitle("خيارات المهمة" if job else "مهمة جديدة")
        self.setLayoutDirection(Qt.RightToLeft)
        self.setMinimumWidth(560)
        job = job or {}
        form = QFormLayout(self)
        self.name = QLineEdit(job.get("name", ""))
        self.name.setPlaceholderText("مثال: العمل")
        form.addRow("اسم المهمة:", self.name)
        self.mode = QComboBox()
        for k in (TWO_WAY, A_TO_B, B_TO_A):
            self.mode.addItem(f"{MODE_ARROW[k]}  {MODES[k]}", k)
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
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def values(self) -> dict:
        return {"name": self.name.text().strip() or "مهمة جديدة", "mode": self.mode.currentData(),
                "excludes": self.excl.text().strip(), "auto": self.auto.isChecked(),
                "keep_days": self.days.value()}


# ───────────────────────────── مربع المجلد ─────────────────────────────
class FolderBox(QWidget):
    """مسار المجلد مع زر «تغيير المجلد» ومعلومات القرص تحته."""

    def __init__(self, title, on_change):
        super().__init__()
        self.title = title
        self.on_change = on_change
        v = QVBoxLayout(self)
        v.setContentsMargins(6, 4, 6, 4)
        v.setSpacing(3)
        top = QHBoxLayout()
        lbl = QLabel(title)
        lbl.setObjectName("side")
        top.addWidget(lbl)
        top.addStretch(1)
        self.btn = QPushButton("📂 تغيير المجلد")
        self.btn.clicked.connect(self.browse)
        top.addWidget(self.btn)
        v.addLayout(top)
        self.edit = QLineEdit()
        self.edit.setObjectName("path")
        self.edit.setPlaceholderText(f"اختر مجلد {title}…")
        self.edit.editingFinished.connect(lambda: self.on_change(self.path()))
        v.addWidget(self.edit)
        self.info = QLabel("")
        self.info.setObjectName("info")
        v.addWidget(self.info)

    def path(self) -> str:
        t = self.edit.text().strip().strip('"')
        return os.path.normpath(t) if t else ""

    def set_path(self, p):
        self.edit.setText(p or "")

    def browse(self):
        start = self.path() if self.path() and os.path.isdir(self.path()) else str(Path.home())
        d = QFileDialog.getExistingDirectory(self, f"اختر مجلد {self.title}", start)
        if d:
            self.set_path(os.path.normpath(d))
            self.on_change(self.path())


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
        self.rows = {}                        # المفتاح ← (عنصر الشجرة، العنصر)
        self.filter = "changes"
        self.busy = False
        self.cancel = threading.Event()
        self.last_report = None
        self.available = {}                   # هوية المهمة ← هل المجلدان متاحان (للمزامنة التلقائية)
        self.auto_running = False
        self._speed = (time.time(), 0)

        self.setWindowTitle(f"SyncFile {__version__}")
        self.setLayoutDirection(Qt.RightToLeft)
        self.resize(1320, 780)
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
        outer.setContentsMargins(8, 8, 8, 8)
        split = QSplitter(Qt.Horizontal)
        outer.addWidget(split)

        # ── قائمة المهام
        side = QWidget()
        lv = QVBoxLayout(side)
        lv.setContentsMargins(0, 0, 0, 0)
        self.new_btn = QPushButton("＋ مهمة جديدة", objectName="primary")
        self.new_btn.clicked.connect(self.new_job)
        lv.addWidget(self.new_btn)
        t = QLabel("كل المهام")
        t.setObjectName("muted")
        lv.addWidget(t)
        self.jobs = QListWidget()
        self.jobs.currentItemChanged.connect(self.on_job_selected)
        self.jobs.itemDoubleClicked.connect(lambda _: self.edit_job())
        lv.addWidget(self.jobs, 1)
        h = QHBoxLayout()
        self.edit_btn = QPushButton("⚙ خيارات")
        self.edit_btn.clicked.connect(self.edit_job)
        self.del_btn = QPushButton("حذف")
        self.del_btn.clicked.connect(self.delete_job)
        h.addWidget(self.edit_btn)
        h.addWidget(self.del_btn)
        lv.addLayout(h)
        dev = QLabel(f"الجهاز: {self.cfg.device_name}  •  الإصدار {__version__}")
        dev.setObjectName("muted")
        lv.addWidget(dev)
        split.addWidget(side)

        # ── منطقة المهمة
        main = QWidget()
        rv = QVBoxLayout(main)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(6)

        bar = QHBoxLayout()
        self.title_lbl = QLabel("")
        self.title_lbl.setObjectName("title")
        bar.addWidget(self.title_lbl)
        bar.addSpacing(12)
        self.analyze_btn = QPushButton("🔍 تحليل", objectName="primary")
        self.analyze_btn.clicked.connect(lambda: self.start_analyze())
        self.sync_btn = QPushButton("⇄ مزامنة", objectName="go")
        self.sync_btn.clicked.connect(self.start_sync)
        self.stop_btn = QPushButton("■ إيقاف", objectName="stop")
        self.stop_btn.clicked.connect(lambda: self.cancel.set())
        for b in (self.analyze_btn, self.sync_btn, self.stop_btn):
            bar.addWidget(b)
        bar.addStretch(1)
        self.auto_chk = QCheckBox("تلقائي عند توصيل الفلاش")
        self.auto_chk.toggled.connect(self.toggle_auto)
        bar.addWidget(self.auto_chk)
        self.report_btn = QPushButton("📄 آخر تقرير", objectName="tool")
        self.report_btn.clicked.connect(self.open_report)
        bar.addWidget(self.report_btn)
        rv.addLayout(bar)

        # ── شريط المجلدين: المصدر  ⇄  الهدف
        folders = QFrame()
        folders.setObjectName("folders")
        fh = QHBoxLayout(folders)
        fh.setContentsMargins(6, 6, 6, 6)
        self.box_a = FolderBox("المصدر", lambda p: self.set_folder("a", p))
        self.box_b = FolderBox("الهدف", lambda p: self.set_folder("b", p))
        mid = QVBoxLayout()
        mid.addStretch(1)
        self.mode_btn = QPushButton("⇄", objectName="mode")
        self.mode_btn.setToolTip("اضغط لتغيير نوع المزامنة")
        self.mode_btn.clicked.connect(self.cycle_mode)
        mid.addWidget(self.mode_btn, 0, Qt.AlignHCenter)
        self.mode_lbl = QLabel("")
        self.mode_lbl.setObjectName("info")
        self.mode_lbl.setAlignment(Qt.AlignHCenter)
        mid.addWidget(self.mode_lbl)
        mid.addStretch(1)
        fh.addWidget(self.box_a, 1)
        fh.addLayout(mid)
        fh.addWidget(self.box_b, 1)
        rv.addWidget(folders)

        # ── شريط التعارض (أصفر)
        self.conflict_bar = QFrame()
        self.conflict_bar.setObjectName("conflict")
        ch = QHBoxLayout(self.conflict_bar)
        ch.setContentsMargins(10, 4, 10, 4)
        self.conflict_lbl = QLabel("")
        ch.addWidget(self.conflict_lbl)
        for text, how in (("⬅ المصدر يفوز", CHOOSE_A), ("➡ الهدف يفوز", CHOOSE_B), ("🕒 الأحدث يفوز", "newer"),
                          ("✔ المقترح", "suggest"), ("⇄ الاحتفاظ بالنسختين", KEEP_BOTH)):
            b = QPushButton(text, objectName="win")
            b.clicked.connect(lambda _=False, h_=how: self.resolve_all(h_))
            ch.addWidget(b)
        ch.addStretch(1)
        self.conflict_bar.hide()
        rv.addWidget(self.conflict_bar)

        self.warn_lbl = QLabel("")
        self.warn_lbl.setObjectName("warn")
        self.warn_lbl.setWordWrap(True)
        self.warn_lbl.hide()
        rv.addWidget(self.warn_lbl)

        # ── أزرار التصفية
        fl = QHBoxLayout()
        fl.setSpacing(2)
        self.filter_group = QButtonGroup(self)
        self.filter_group.setExclusive(True)
        self.filter_btns = {}
        for key, text in FILTERS:
            b = QPushButton(text, objectName="filter")
            b.setCheckable(True)
            b.setChecked(key == self.filter)
            b.clicked.connect(lambda _=False, k=key: self.set_filter(k))
            self.filter_group.addButton(b)
            self.filter_btns[key] = b
            fl.addWidget(b)
        fl.addStretch(1)
        rv.addLayout(fl)

        vsplit = QSplitter(Qt.Vertical)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["الملف", "حجم المصدر", "تاريخ المصدر", "الاتجاه", "حجم الهدف", "تاريخ الهدف",
                                   "الحالة"])
        hh = self.tree.header()
        hh.setSectionResizeMode(C_NAME, QHeaderView.Stretch)
        for c in (C_ASIZE, C_ADATE, C_DIR, C_BSIZE, C_BDATE):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(C_STATUS, QHeaderView.Interactive)
        self.tree.setColumnWidth(C_STATUS, 260)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.context_menu)
        self.tree.itemDoubleClicked.connect(self.on_double_click)
        self.tree.setUniformRowHeights(True)
        vsplit.addWidget(self.tree)
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(3000)
        vsplit.addWidget(self.log_box)
        vsplit.setSizes([540, 110])
        rv.addWidget(vsplit, 1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        rv.addWidget(self.progress)
        self.status_lbl = QLabel("")
        self.status_lbl.setObjectName("muted")
        rv.addWidget(self.status_lbl)
        split.addWidget(main)
        split.setSizes([230, 1090])
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
        ready = has and bool(self.job.get("a")) and bool(self.job.get("b"))
        self.analyze_btn.setEnabled(ready and not busy)
        self.sync_btn.setEnabled(ready and not busy)
        self.stop_btn.setEnabled(busy)
        for w in (self.new_btn, self.jobs):
            w.setEnabled(not busy)
        for w in (self.edit_btn, self.del_btn, self.box_a, self.box_b, self.mode_btn, self.auto_chk,
                  self.conflict_bar):
            w.setEnabled(has and not busy)
        self.report_btn.setEnabled(self.last_report is not None)
        if status:
            self.status_lbl.setText(status)

    # ---------- المهام ----------
    def load_jobs(self, select=None):
        if select is None and self.job:
            select = self.job["id"]
        self.jobs.blockSignals(True)
        self.jobs.clear()
        current = None
        for p in self.cfg.pairs:
            last = p.get("last") or "لم تُزامَن بعد"
            it = QListWidgetItem(f"{MODE_ARROW.get(p.get('mode'), '⇄')}  {p['name']}\n{last}")
            it.setData(Qt.UserRole, p["id"])
            it.setToolTip(f"المصدر: {p['a'] or '—'}\nالهدف: {p['b'] or '—'}")
            self.jobs.addItem(it)
            if p["id"] == select:
                current = it
        self.jobs.blockSignals(False)
        if current is None and self.jobs.count():
            current = self.jobs.item(0)
        if current is not None:
            self.jobs.blockSignals(True)
            self.jobs.setCurrentItem(current)
            self.jobs.blockSignals(False)
        self.on_job_selected(current, None)

    def clear_plan(self, message=""):
        self.plan = None
        self.choices = {}
        self.rows = {}
        self.tree.clear()
        self.warn_lbl.hide()
        self.conflict_bar.hide()
        self.update_filter_counts()
        self.update_info()
        if message:
            self.status_lbl.setText(message)

    def on_job_selected(self, cur, _prev=None):
        pid = cur.data(Qt.UserRole) if cur is not None else None
        self.job = self.cfg.get(pid) if pid else None
        j = self.job or {}
        self.box_a.set_path(j.get("a", ""))
        self.box_b.set_path(j.get("b", ""))
        self.auto_chk.blockSignals(True)
        self.auto_chk.setChecked(bool(j.get("auto")))
        self.auto_chk.blockSignals(False)
        self.refresh_header()
        if not self.job:
            self.clear_plan("اضغط «＋ مهمة جديدة» ثم اختر مجلد المصدر ومجلد الهدف.")
        elif not (j.get("a") and j.get("b")):
            self.clear_plan("اختر مجلد المصدر ومجلد الهدف من الشريط العلوي (زر «تغيير المجلد»).")
        else:
            self.clear_plan("اضغط «تحليل» لمعرفة ما سيتغير قبل المزامنة.")
        self.set_busy(self.busy)

    def refresh_header(self):
        j = self.job or {}
        mode = j.get("mode", TWO_WAY)
        self.title_lbl.setText(j.get("name", "SyncFile"))
        self.mode_btn.setText(MODE_ARROW.get(mode, "⇄"))
        self.mode_lbl.setText(MODE_SHORT.get(mode, ""))
        self.mode_btn.setToolTip(f"{MODES.get(mode, '')}\nاضغط لتغيير نوع المزامنة")

    def new_job(self):
        if self.busy:
            return
        d = JobDialog(self)
        if self.quiet or d.exec():
            v = d.values() if not self.quiet else {"name": "مهمة جديدة"}
            p = self.cfg.add_pair("", "", v.pop("name"), **v)
            self.log(f"أُنشئت المهمة «{p['name']}» — اختر المجلدين")
            self.load_jobs(select=p["id"])
            if not self.quiet:
                self.box_a.browse()

    def edit_job(self):
        if not self.job or self.busy:
            return
        d = JobDialog(self, self.job)
        if d.exec():
            self.cfg.update(self.job["id"], **d.values())
            self.load_jobs(select=self.job["id"])

    def delete_job(self):
        if not self.job or self.busy:
            return
        if self.ask("حذف المهمة", f"حذف المهمة «{self.job['name']}» من القائمة؟\n"
                                  "لن يُحذف أي ملف من المجلدين."):
            self.cfg.remove(self.job["id"])
            self.job = None
            self.load_jobs()

    def set_folder(self, side, path):
        """اختيار المصدر أو الهدف من الشريط العلوي."""
        if not self.job or self.busy:
            return
        if path == self.job.get(side, ""):
            self.update_info()
            return
        other = self.job.get("b" if side == "a" else "a", "")
        if path and other and os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(other)):
            self.notify("warn", "SyncFile", "المصدر والهدف هما المجلد نفسه. اختر مجلداً آخر.")
            (self.box_a if side == "a" else self.box_b).set_path(self.job.get(side, ""))
            return
        kw = {side: path, f"{side}_id": ""}
        if side == "a" and path and self.job.get("name") in ("", "مهمة جديدة"):
            kw["name"] = Path(path).name
        if side == "a" and path and not self.job.get("b"):
            self.cfg.update(self.job["id"], **kw)
            self.load_jobs(select=self.job["id"])
            if not self.quiet:
                self.box_b.browse()             # بعد المصدر يطلب الهدف مباشرة
            return
        self.cfg.update(self.job["id"], **kw)
        self.load_jobs(select=self.job["id"])
        if self.job.get("a") and self.job.get("b"):
            self.start_analyze()

    def cycle_mode(self):
        if not self.job or self.busy:
            return
        order = [TWO_WAY, A_TO_B, B_TO_A]
        mode = order[(order.index(self.job.get("mode", TWO_WAY)) + 1) % 3]
        self.cfg.update(self.job["id"], mode=mode)
        self.refresh_header()
        self.load_jobs(select=self.job["id"])
        self.log(f"نوع المزامنة: {MODES[mode]}")
        if self.job.get("a") and self.job.get("b"):
            self.start_analyze()

    def toggle_auto(self, on):
        if self.job:
            self.cfg.update(self.job["id"], auto=bool(on))
            self.available.pop(self.job["id"], None)

    def update_info(self):
        """معلومات القرصين والحجم المطلوب في كل طرف."""
        need_a = need_b = 0
        if self.plan:
            for it in self.plan.items:
                if it.blocked:
                    continue
                act = effective(it, self.choices)
                if act in (A2B, "both") and it.a:
                    need_b += it.a["size"]
                if act == "both" and it.b:
                    need_a += it.b["size"]
                if act == B2A and it.b:
                    need_a += it.b["size"]
        j = self.job or {}
        self.box_a.info.setText(drive_info(j.get("a", ""), need_a) if self.job else "")
        self.box_b.info.setText(drive_info(j.get("b", ""), need_b) if self.job else "")

    def resolve_paths(self, job):
        """يعيد (المصدر، الهدف) بعد البحث عن الفلاش إذا تغيّر حرفه، وينشئ مجلداً جديداً عند الحاجة."""
        out = []
        for side, name in (("a", "المصدر"), ("b", "الهدف")):
            path = job[side]
            if not os.path.isdir(lp(path)) and job.get(f"{side}_id"):
                found = relocate(path, job[f"{side}_id"])
                if found:
                    self.log(f"تغيّر حرف القرص: {path} ← {found}")
                    self.cfg.update(job["id"], **{side: found})
                    path = found
                    (self.box_a if side == "a" else self.box_b).set_path(found)
            if (not os.path.isdir(lp(path)) and not job.get(f"{side}_id")
                    and os.path.isdir(lp(os.path.dirname(os.path.abspath(path))))):
                # مهمة جديدة لم تُزامَن بعد: المجلد غير موجود لكن القرص موجود ← ننشئه مثل GoodSync
                if self.ask("إنشاء المجلد", f"مجلد {name} غير موجود:\n{path}\n\nهل تريد إنشاءه؟"):
                    os.makedirs(lp(path), exist_ok=True)
                    self.log(f"أُنشئ مجلد {name}: {path}")
            out.append(path)
        return out

    def open_report(self):
        if self.last_report:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_report)))

    # ---------- التحليل ----------
    def start_analyze(self, auto=False):
        if not self.job or self.busy or not (self.job.get("a") and self.job.get("b")):
            return
        job = self.job
        try:
            a, b = self.resolve_paths(job)
        except OSError as e:
            self.notify("warn", "SyncFile", f"تعذر إنشاء المجلد: {e}")
            return
        self.clear_plan()
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
                         ("حذف", plan.count(DEL_A, DEL_B)), ("تعارض", len(plan.decisions)),
                         ("متوقف", sum(1 for i in plan.items if i.blocked)),
                         ("لن يُنسخ", plan.count(SKIP)), ("متطابق", plan.count(EQUAL))):
            if n:
                parts.append(f"{label}: {n}")
        head = "أول مزامنة لهذين المجلدين — " if plan.first_sync else ""
        return f"{head}{'   •   '.join(parts)}   •   حجم النقل: {size_ar(plan.transfer_bytes)}"

    def set_filter(self, key):
        self.filter = key
        self.filter_btns[key].setChecked(True)
        if self.plan:
            self.show_plan(self.plan)

    def update_filter_counts(self):
        counts = dict.fromkeys(self.filter_btns, 0)
        if self.plan:
            for it in self.plan.items:
                act = effective(it, self.choices)
                manual = it.key in self.choices
                for k in counts:
                    if in_filter(k, it, act, manual):
                        counts[k] += 1
        for key, text in FILTERS:
            self.filter_btns[key].setText(f"{text} ({counts[key]})")

    def show_plan(self, plan):
        self.plan = plan
        self.tree.clear()
        self.rows = {}
        bold = QFont()
        bold.setBold(True)
        top = QTreeWidgetItem([f"📁 {(self.job or {}).get('name', plan.a_root.name)}", "", "", "", "", "", ""])
        top.setData(0, Qt.UserRole, ("dir", ""))
        top.setFont(C_NAME, bold)
        self.tree.addTopLevelItem(top)
        folders = {"": top}
        totals = {}

        def folder_node(path):
            if path in folders:
                return folders[path]
            parent_path, name = os.path.split(path)
            node = QTreeWidgetItem([f"📁 {name}", "", "", "", "", "", ""])
            node.setData(0, Qt.UserRole, ("dir", path))
            node.setFont(C_NAME, bold)
            folder_node(parent_path).addChild(node)
            folders[path] = node
            return node

        for it in plan.items:
            act = effective(it, self.choices)
            if not in_filter(self.filter, it, act, it.key in self.choices):
                continue
            folder, name = os.path.split(it.rel)
            node = QTreeWidgetItem([name, "", "", "", "", "", ""])
            node.setData(0, Qt.UserRole, ("file", it.key))
            node.setToolTip(C_NAME, it.rel)
            folder_node(folder).addChild(node)
            self.rows[it.key] = (node, it)
            self.paint(it)
            parts = folder.split("/") if folder else []
            for i in range(len(parts) + 1):
                t = totals.setdefault("/".join(parts[:i]), [0, 0])
                t[0] += (it.a or {}).get("size", 0)
                t[1] += (it.b or {}).get("size", 0)
        for path, node in folders.items():
            sa, sb = totals.get(path, (0, 0))
            node.setText(C_ASIZE, size_ar(sa))
            node.setText(C_BSIZE, size_ar(sb))
            node.setText(C_ADATE, "(مجلد)")
            node.setText(C_BDATE, "(مجلد)")
            node.setBackground(C_ASIZE, QBrush(QColor(SRC_BG)))
            node.setBackground(C_ADATE, QBrush(QColor(SRC_BG)))
            node.setBackground(C_BSIZE, QBrush(QColor(DST_BG)))
            node.setBackground(C_BDATE, QBrush(QColor(DST_BG)))
        top.setText(C_STATUS, self.summary_text(plan))
        if not self.rows:
            top.setText(C_NAME, top.text(C_NAME) + "   — لا شيء في هذا التصنيف")
        self.tree.expandAll()
        self.status_lbl.setText(self.summary_text(plan))
        if plan.warnings:
            self.warn_lbl.setText("\n".join(plan.warnings))
            self.warn_lbl.show()
        else:
            self.warn_lbl.hide()
        self.after_change()

    def after_change(self):
        """تحديث العدادات وشريط التعارض والمساحة المطلوبة بعد أي تغيير."""
        self.update_filter_counts()
        self.update_info()
        pending = self.pending_decisions() if self.plan else []
        if pending:
            self.conflict_lbl.setText(f"⚠ يوجد {len(pending)} ملف متعارض. اختر اتجاه المزامنة:")
            self.conflict_bar.show()
        else:
            self.conflict_bar.hide()

    def paint(self, it):
        node = self.rows[it.key][0]
        act = effective(it, self.choices)
        sym, text, color = LOOK.get(act, ("?", act, "#000"))
        manual = it.key in self.choices
        if it.blocked:
            sym, text, color = "⛔", "متوقف", "#b91c1c"
        a, b = it.a or {}, it.b or {}
        node.setText(C_ASIZE, size_ar(a["size"]) if a else "")
        node.setText(C_ADATE, fmt_time(a["mtime"]) if a else "")
        node.setText(C_DIR, f"  {sym}  ")
        node.setText(C_BSIZE, size_ar(b["size"]) if b else "")
        node.setText(C_BDATE, fmt_time(b["mtime"]) if b else "")
        status = it.blocked or (f"{text}" + (f" — {it.note}" if it.note else "") + ("  (يدوي)" if manual else ""))
        node.setText(C_STATUS, status)
        node.setToolTip(C_STATUS, status)
        node.setToolTip(C_DIR, f"{text}\nانقر مرتين للتبديل، أو الزر الأيمن للخيارات")
        node.setTextAlignment(C_DIR, Qt.AlignCenter)
        brush = QBrush(QColor(color))
        node.setForeground(C_DIR, brush)
        node.setForeground(C_STATUS, brush)
        conflict = (it.needs_choice and not manual) or bool(it.blocked)
        node.setForeground(C_NAME, QBrush(QColor("#dc2626" if conflict else "#0f172a")))
        f = node.font(C_DIR)
        f.setBold(True)
        f.setPointSize(13)
        node.setFont(C_DIR, f)
        fs = node.font(C_STATUS)
        fs.setItalic(manual)
        node.setFont(C_STATUS, fs)
        for c in (C_ASIZE, C_ADATE):
            node.setBackground(c, QBrush(QColor(SRC_BG)))
        for c in (C_BSIZE, C_BDATE):
            node.setBackground(c, QBrush(QColor(DST_BG)))

    # ---------- تغيير الاتجاه ----------
    def set_choice(self, key, choice, refresh=True):
        """None = إرجاع الملف لاتجاهه المقترح."""
        if key not in self.rows:
            return
        if choice is None:
            self.choices.pop(key, None)
        else:
            self.choices[key] = choice
        self.paint(self.rows[key][1])
        if refresh:
            self.after_change()

    def set_many(self, items, choice):
        for i in items:
            self.set_choice(i.key, choice(i) if callable(choice) else choice, refresh=False)
        self.after_change()

    def resolve_all(self, how):
        """أزرار الشريط الأصفر: المصدر يفوز / الهدف يفوز / الأحدث يفوز / المقترح / النسختان."""
        pick = {"newer": newer_choice, "suggest": default_choice}.get(how, how)
        if how == KEEP_BOTH:
            pick = lambda i: KEEP_BOTH if (i.a and i.b) else default_choice(i)
        self.set_many(self.pending_decisions(), pick)

    def items_under(self, path):
        if not path:
            return [it for _, it in self.rows.values()]
        pre = path.rstrip("/") + "/"
        return [it for _, it in self.rows.values() if it.rel.startswith(pre)]

    def on_double_click(self, node, col):
        """النقر مرتين على عمود الاتجاه يبدّل بين الخيارات مثل GoodSync."""
        kind, ref = node.data(0, Qt.UserRole) or (None, None)
        if kind != "file" or self.busy:
            return
        it = self.rows[ref][1]
        if col != C_DIR:
            f, root = (it.a, self.plan.a_root) if col in (C_ASIZE, C_ADATE) or not it.b else (it.b, self.plan.b_root)
            if f and col != C_NAME:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(root / f["rel"])))
            return
        if it.blocked:
            return
        opts = [v for v, _ in choices_for(it)]
        cur = self.choices.get(it.key)
        nxt = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else opts[0]
        self.set_choice(it.key, nxt)

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
            menu.addAction(f"📁 {ref or 'المجلد كله'} — {len(items)} ملف").setEnabled(False)
            for val, text in ((CHOOSE_A, "⬅  اجعل الهدف مثل المصدر"), (CHOOSE_B, "➡  اجعل المصدر مثل الهدف"),
                              ("newer", "🕒  الأحدث يفوز"), (NO_COPY, "⊘  لا تنسخ الآن"),
                              (None, "↺  الاتجاه المقترح")):
                pick = newer_choice if val == "newer" else val
                menu.addAction(text).triggered.connect(lambda _=False, p=pick, its=items: self.set_many(its, p))
            if ref:
                menu.addSeparator()
                menu.addAction("فتح المجلد في المصدر").triggered.connect(
                    lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.plan.a_root / ref))))
                menu.addAction("فتح المجلد في الهدف").triggered.connect(
                    lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.plan.b_root / ref))))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    # ---------- المزامنة ----------
    def pending_decisions(self):
        return [i for i in self.plan.items if i.needs_choice and not i.blocked and i.key not in self.choices]

    def start_sync(self):
        if not self.job or self.busy:
            return
        if self.plan is None:
            self.start_analyze()                # مثل GoodSync: «مزامنة» بلا تحليل تحلّل أولاً
            return
        if not self.plan.changes and not self.choices:
            self.notify("info", "SyncFile", "كل الملفات متطابقة، لا يوجد ما يُزامَن.")
            return
        pending = self.pending_decisions()
        if pending:
            if self.quiet:
                self.set_many(pending, default_choice)
            else:
                d = DecisionDialog(self, self.plan, pending, self.choices)
                if not d.exec():
                    return
                picked = d.result_choices()
                self.set_many(pending, lambda i: picked[i.key])
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
        self.status_lbl.setText(f"{size_ar(done)} من {size_ar(total)}  •  {size_ar(speed)}/ث  •  {Path(rel).name}")

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
        self.load_jobs()
        self.start_analyze()

    def on_failed(self, msg):
        self.progress.setRange(0, 1000)
        self.auto_running = False
        self.set_busy(False, msg.splitlines()[0])
        self.notify("warn", "SyncFile", msg)

    # ---------- التلقائي ----------
    def job_available(self, job) -> bool:
        if not (job.get("a") and job.get("b")):
            return False
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
                if job.get(side) and os.path.isdir(lp(job[side])):
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
    (tmp / "USB").mkdir(parents=True)
    for i in range(1, 5):
        (src / f"خطاب {i}.txt").write_text(f"خطاب {i}", encoding="utf-8")
    (src / "المكتب" / "ملاحظات.txt").write_text("ملاحظات", encoding="utf-8")
    (src / "نسخة.bak").write_text("مستثنى", encoding="utf-8")

    app = QApplication.instance() or QApplication(sys.argv)
    app.setLayoutDirection(Qt.RightToLeft)
    app.setStyleSheet(STYLE)
    cfg = AppConfig(tmp / "appdata")
    w = MainWindow(cfg, quiet=True)
    w.start_analyze = lambda *a, **k: None      # التحليل هنا يدوي بلا خيوط

    # مهمة جديدة ← اختيار المصدر ثم الهدف من الشريط العلوي (الهدف غير موجود فيُنشأ)
    w.new_job()
    assert w.job and not w.job["a"], "يجب أن تُنشأ مهمة فارغة"
    w.box_a.set_path(str(src))
    w.set_folder("a", str(src))
    assert w.job["name"] == "العمل" and w.job["a"] == str(src)
    w.cfg.update(w.job["id"], excludes="*.bak")
    w.box_b.set_path(str(dst))
    w.set_folder("b", str(dst))
    assert w.job["b"] == str(dst)
    a, b = w.resolve_paths(w.job)
    assert dst.is_dir(), "يجب إنشاء مجلد الهدف"
    assert "المتاح" in drive_info(str(src)), drive_info(str(src))

    plan = build_plan(a, b, None, w.job["mode"], parse_excludes(w.job["excludes"]))
    w.on_plan(plan)
    assert set(w.rows) == {"خطاب 1.txt", "خطاب 2.txt", "خطاب 3.txt", "خطاب 4.txt", "المكتب/ملاحظات.txt"}, sorted(w.rows)
    w.set_filter("to_b")
    assert len(w.rows) == 5
    w.set_filter("conflicts")
    assert len(w.rows) == 0
    w.set_filter("changes")
    key = "خطاب 4.txt"
    w.set_choice(key, NO_COPY)
    assert effective(w.rows[key][1], w.choices) == SKIP
    w.set_choice(key, None)
    w.cycle_mode()                              # ⇄ ← ⬅
    assert w.cfg.get(w.job["id"])["mode"] == A_TO_B
    w.cfg.update(w.job["id"], mode=TWO_WAY)
    w.plan = plan
    DecisionDialog(w, plan, plan.items[:1], {}).result_choices()

    res = execute(plan, w.choices, cfg.device_name)
    w.on_progress(10.0, 10.0, "خطاب 4.txt")
    w.finish_sync(plan, res)
    assert not res.errors, res.errors
    assert (dst / "المكتب" / "ملاحظات.txt").exists() and (dst / "خطاب 4.txt").exists()
    assert not (dst / "نسخة.bak").exists(), "الاستثناء لم يُطبَّق"
    assert cfg.get(w.job["id"])["a_id"], "يجب حفظ هوية المجلد بعد المزامنة"
    assert w.last_report and Path(w.last_report).exists()

    # تعارض: عُدّل الملف في الطرفين ← الشريط الأصفر
    (src / "خطاب 1.txt").write_text("تعديل المصدر", encoding="utf-8")
    (dst / "خطاب 1.txt").write_text("تعديل الهدف الأطول", encoding="utf-8")
    plan2 = build_plan(src, dst)
    w.on_plan(plan2)
    assert len(w.pending_decisions()) == 1
    w.resolve_all("newer")
    assert not w.pending_decisions()
    res2 = execute(plan2, w.choices, cfg.device_name)
    assert not res2.errors, res2.errors
    assert (src / "خطاب 1.txt").read_text(encoding="utf-8") == (dst / "خطاب 1.txt").read_text(encoding="utf-8")

    w.show_plan(build_plan(src, dst))
    assert not w.plan.changes
    w.watch()
    w.close()
    print("SMOKE TEST OK")
    return 0
