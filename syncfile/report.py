"""تقارير المزامنة: صفحة HTML عربية (من اليمين لليسار) وملف CSV يفتح في إكسل."""

import csv
import html
from datetime import datetime
from pathlib import Path

from .engine import CONFLICT, EQUAL, FORGET, LABELS


def _size(n):
    for u in ("بايت", "كيلوبايت", "ميجابايت", "جيجابايت"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "بايت" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} تيرابايت"


def write_reports(folder: Path, plan, res, device_name: str):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in plan.name)
    html_path = folder / f"تقرير_{safe}_{stamp}.html"
    csv_path = folder / f"تقرير_{safe}_{stamp}.csv"

    errors = dict(res.errors)
    done = dict(res.done)
    rows = []
    for it in plan.items:
        if it.action in (EQUAL, FORGET):
            continue
        src = it.local if it.action != "pull" else it.remote
        rows.append({
            "path": it.rel, "action": it.label, "size": (src or it.local or it.remote or {}).get("size", 0),
            "hash": (src or it.local or it.remote or {}).get("hash", ""),
            "result": "خطأ: " + errors[it.rel] if it.rel in errors else done.get(it.rel, "لم يُنفَّذ"),
            "conflict": it.action == CONFLICT, "error": it.rel in errors,
        })

    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:   # BOM حتى يفتحه إكسل بالعربي
        w = csv.writer(f)
        w.writerow(["المسار", "الإجراء", "الحجم (بايت)", "SHA-256", "النتيجة"])
        for r in rows:
            w.writerow([r["path"], r["action"], r["size"], r["hash"], r["result"]])

    scanned = len(plan.items)
    equal = sum(1 for i in plan.items if i.action == EQUAL)
    status = ("اكتملت بلا أخطاء" if not res.errors and not res.cancelled
              else "أُوقفت قبل الانتهاء" if res.cancelled else f"اكتملت مع {len(res.errors)} خطأ")
    esc = html.escape
    trs = "\n".join(
        f'<tr class="{"err" if r["error"] else "conf" if r["conflict"] else ""}">'
        f'<td class="p">{esc(r["path"])}</td><td>{esc(r["action"])}</td><td class="c">{_size(r["size"])}</td>'
        f'<td class="h" title="{esc(r["hash"])}">{esc(r["hash"][:12])}…</td><td>{esc(r["result"])}</td></tr>'
        for r in rows) or '<tr><td colspan="5" class="c">لا توجد تغييرات — كل الملفات متطابقة</td></tr>'
    warn = "".join(f"<li>{esc(w)}</li>" for w in plan.warnings)
    page = f"""<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<title>تقرير مزامنة {esc(plan.name)}</title>
<style>
body{{font-family:"Segoe UI",Tahoma,Arial,sans-serif;margin:28px;color:#1e293b;background:#f8fafc}}
h1{{color:#0369a1;margin:0 0 4px}} .muted{{color:#64748b}}
.cards{{display:flex;gap:12px;flex-wrap:wrap;margin:18px 0}}
.card{{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:12px 18px;min-width:120px}}
.card b{{display:block;font-size:22px}}
table{{border-collapse:collapse;width:100%;background:#fff;border:1px solid #e2e8f0}}
th,td{{padding:8px 10px;border-bottom:1px solid #e2e8f0;text-align:right;vertical-align:top}}
th{{background:#0f172a;color:#fff}} td.c{{text-align:center}} td.h{{font-family:Consolas,monospace;direction:ltr;text-align:center;color:#64748b}}
tr.conf td{{background:#fef3c7}} tr.err td{{background:#fee2e2}} td.p{{unicode-bidi:plaintext}}
</style></head><body>
<h1>تقرير مزامنة: {esc(plan.name)}</h1>
<div class="muted">الجهاز: {esc(device_name)} • من {esc(res.started)} إلى {esc(res.finished)} • {status}</div>
<div class="cards">
<div class="card">مفحوصة<b>{scanned}</b></div><div class="card">متطابقة<b>{equal}</b></div>
<div class="card">حُدّثت<b>{len(res.done)}</b></div><div class="card">تعارضات<b>{len(res.conflicts)}</b></div>
<div class="card">أخطاء<b>{len(res.errors)}</b></div><div class="card">نُقل<b>{_size(res.bytes)}</b></div>
</div>
{f'<ul>{warn}</ul>' if warn else ''}
<table><tr><th>المسار</th><th>الإجراء</th><th>الحجم</th><th>البصمة SHA-256</th><th>النتيجة</th></tr>
{trs}</table>
<p class="muted">المحذوف والمستبدل محفوظ في مجلد <b>.نسخ_احتياطية</b> داخل المجلد على الجهاز وعلى الفلاش.</p>
</body></html>"""
    html_path.write_text(page, encoding="utf-8")
    return html_path, csv_path


__all__ = ["write_reports", "LABELS"]
