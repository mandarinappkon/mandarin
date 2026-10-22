"""
Проверка PDF-редактора на виртуальном экране настоящими событиями мыши:
    xvfb-run -a python tests/test_viewer.py [скриншот.png]
"""
import os
import shutil
import sys
import tempfile
import tkinter as tk
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="mandarin-appdata-")
import dialogs  # noqa: E402
import engine  # noqa: E402
import pdftools  # noqa: E402
import test_pdftools as samples  # noqa: E402
import viewer  # noqa: E402

fitz = engine._fitz()
shot = sys.argv[1] if len(sys.argv) > 1 else None
d = Path(tempfile.mkdtemp(prefix="viewer-test-"))
a, b, scan = samples.make_samples(d)

# заглушки диалогов
answers = {"ask": "Проверка заметки", "multi": "Стоимость услуг составляет 2000 рублей."}
viewer.simpledialog.askstring = lambda *a, **k: k.get("initialvalue") or answers["ask"]
viewer.ask_multiline = lambda *a, **k: answers["multi"]
viewer.messagebox.askyesno = lambda *a, **k: True
viewer.messagebox.showinfo = lambda *a, **k: None
dialogs.ask = lambda key, parent, files: {"watermark": {"text": "ЧЕРНОВИК", "opacity": 0.2, "angle": 45,
                                                      "color": (200, 30, 40), "pages": ""}}.get(key, {})

try:
    from tkinterdnd2 import TkinterDnD
    root = TkinterDnD.Tk()
except Exception:
    root = tk.Tk()
root.withdraw()
v = viewer.PdfViewer(root, a)
v.win.geometry("1320x860+0+0")
results = []
fails = []


def step(name, fn):
    try:
        msg = fn()
        results.append(f"  ok   {name:26} {msg or ''}")
    except Exception as e:
        fails.append(name)
        results.append(f"  FAIL {name:26} {type(e).__name__}: {e}")
        traceback.print_exc(limit=4)


def cxy(pno, x, y):
    """Точка PDF → координаты события на холсте."""
    r = fitz.Rect(x, y, x, y)
    X0, Y0, _, _ = v.to_canvas(pno, r)
    return int(X0 - v.canvas.canvasx(0)), int(Y0 - v.canvas.canvasy(0))


def drag(p0, p1, steps=8):
    c = v.canvas
    c.event_generate("<ButtonPress-1>", x=p0[0], y=p0[1])
    for i in range(1, steps + 1):
        c.event_generate("<B1-Motion>", x=int(p0[0] + (p1[0] - p0[0]) * i / steps),
                         y=int(p0[1] + (p1[1] - p0[1]) * i / steps))
    c.event_generate("<ButtonRelease-1>", x=p1[0], y=p1[1])
    root.update()


def click(p):
    drag(p, p, steps=1)


def word_rect(pno, text):
    hits = v.doc[pno].search_for(text)
    assert hits, f"нет «{text}» на странице"
    return hits[0]


def annots(pno=0):
    return [x.type[1] for x in v.doc[pno].annots() or []]


def run():
    root.update()
    v.goto(0)
    root.update()

    def t_select():
        r = word_rect(0, "Стоимость")
        r2 = word_rect(0, "рублей")
        v.set_tool("select")
        drag(cxy(0, r.x0 + 2, (r.y0 + r.y1) / 2), cxy(0, r2.x1 - 2, (r2.y0 + r2.y1) / 2))
        t = v.selected_text()
        assert t.startswith("Стоимость") and "рублей" in t, t
        v.copy()
        assert root.clipboard_get().startswith("Стоимость")
        return f"«{t}»"
    step("выделение и копирование", t_select)

    def t_highlight():
        r = word_rect(0, "Контакты")
        v.set_tool("highlight")
        drag(cxy(0, r.x0 + 1, (r.y0 + r.y1) / 2), cxy(0, r.x1 - 1, (r.y0 + r.y1) / 2))
        assert "Highlight" in annots(), annots()
        return "маркер"
    step("маркер", t_highlight)

    def t_shapes():
        for tool, (x0, y0, x1, y1) in (("rect", (380, 300, 480, 360)), ("ellipse", (380, 380, 480, 440)),
                                       ("arrow", (300, 470, 450, 520)), ("ink", (100, 520, 250, 560))):
            v.set_tool(tool)
            drag(cxy(0, x0, y0), cxy(0, x1, y1))
        n = annots()
        for t in ("Square", "Circle", "Line", "Ink"):
            assert t in n, n
        v.set_tool("note")
        click(cxy(0, 520, 90))
        assert "Text" in annots()
        return ", ".join(annots())
    step("фигуры, рисование, заметка", t_shapes)

    def t_move_erase():
        v.set_tool("select")
        sq = [x for x in v.pg(0).annots() if x.type[1] == "Square"][0]
        r0 = fitz.Rect(sq.rect)
        drag(cxy(0, r0.x0 + 2, r0.y0 + 2), cxy(0, r0.x0 + 42, r0.y0 + 22))
        sq = [x for x in v.pg(0).annots() if x.type[1] == "Square"][0]
        assert abs(sq.rect.x0 - (r0.x0 + 40)) < 3, (r0, sq.rect)
        v.set_tool("erase")
        cc = [x for x in v.pg(0).annots() if x.type[1] == "Circle"][0].rect
        click(cxy(0, cc.x0 + 2, (cc.y0 + cc.y1) / 2))
        assert "Circle" not in annots(), annots()
        return "перемещение и ластик"
    step("двигать и удалять", t_move_erase)

    def t_sign_date_check():
        v.signature_png = dialogs.strokes_to_png([[(10, 40), (40, 10), (70, 45), (110, 15), (150, 40)]])
        before = len(v.pg(0).get_images())
        v.set_tool("sign")
        click(cxy(0, 420, 700))
        assert len(v.pg(0).get_images()) == before + 1
        v.set_tool("date")
        click(cxy(0, 120, 700))
        import datetime
        assert datetime.date.today().strftime("%d.%m.%Y") in v.pg(0).get_text()
        v.set_tool("check")
        click(cxy(0, 90, 740))
        v.stamp_choice = pdftools.make_stamp_png("УТВЕРЖДЕНО", pdftools.STAMPS["УТВЕРЖДЕНО"])
        v.tool = "stamp"
        click(cxy(0, 470, 780))
        return "подпись, дата, галочка, штамп"
    step("подпись и штампы", t_sign_date_check)

    def t_edit_text():
        r = word_rect(0, "Стоимость")
        v.set_tool("edit_text")
        click(cxy(0, r.x0 + 5, (r.y0 + r.y1) / 2))
        t = v.pg(0).get_text()
        assert "2000" in t and "1000" not in t, t
        return "1000 → 2000"
    step("правка текста", t_edit_text)

    def t_redact():
        r = word_rect(0, "ivan.petrov@mail.ru")
        v.set_tool("redact")
        drag(cxy(0, r.x0 - 1, r.y0 - 1), cxy(0, r.x1 + 1, r.y1 + 1))
        assert "Redact" in annots()
        v.apply_redact()
        assert "ivan.petrov" not in v.pg(0).get_text()
        return "e-mail удалён из файла"
    step("зачернение", t_redact)

    def t_form():
        v.set_tool("f_text")
        drag(cxy(0, 72, 600), cxy(0, 300, 620))
        f = pdftools.list_fields(v.doc)
        assert f and f[-1]["type"] == "Text", f
        w = list(v.pg(0).widgets())[-1]
        v._set_widget(0, w, "Иванов Иван")
        assert pdftools.list_fields(v.doc)[-1]["value"] == "Иванов Иван"
        v.set_tool("f_check")
        click(cxy(0, 72, 640))
        v.set_tool("fill")
        click(cxy(0, 78, 646))
        vals = {x["name"]: x["value"] for x in pdftools.list_fields(v.doc)}
        return f"поля: {vals}"
    step("создание и заполнение формы", t_form)

    def t_watermark_undo():
        n_img = len(v.pg(1).get_images())
        v.do_watermark()
        assert len(v.pg(1).get_images()) == n_img + 1
        v.undo()
        assert len(v.pg(1).get_images()) == n_img, "отмена не сработала"
        v.redo()
        assert len(v.pg(1).get_images()) == n_img + 1
        return "водяной знак, Ctrl+Z, Ctrl+Y"
    step("водяной знак и отмена", t_watermark_undo)

    def t_search():
        v.search_var.set("Договор")
        v.search(+1)
        assert len(v.search_hits) == 3, v.search_hits
        v.search(+1)
        return v.search_lbl.cget("text")
    step("поиск", t_search)

    def t_save():
        out = d / "результат.pdf"
        v.save_path = out
        v.save()
        x = fitz.open(out)
        types = {an.type[1] for an in x[0].annots()}
        assert {"Highlight", "Square", "Ink"} <= types, types
        assert "ivan.petrov" not in x[0].get_text()
        assert not v.dirty
        return f"{out.name}: {len(list(x[0].annots()))} комм."
    step("сохранение", t_save)


def ocr_then_finish():
    def t_ocr():
        v.open(scan)
        root.update()
        v.do_ocr()
        for _ in range(300):
            root.update()
            root.after(50)
            if v.win.cget("cursor") == "" and "Акт" in v.pg(0).get_text():
                break
        assert "Акт" in v.pg(0).get_text(), v.status.cget("text")
        return "скан распознан прямо в редакторе"
    step("OCR в редакторе", t_ocr)
    v.open(d / "результат.pdf")
    root.after(900, finish)


def finish():
    root.update()
    if shot:
        import subprocess
        v.set_tool("highlight")
        root.update()
        subprocess.run(["import", "-window", "root", "-crop", "1320x860+0+0", shot])
    print("\n".join(results))
    print(f"\nРедактор: {len(results) - len(fails)} ok, {len(fails)} fail")
    shutil.rmtree(d, ignore_errors=True)
    root.destroy()
    sys.exit(1 if fails else 0)


root.after(800, lambda: (run(), root.after(200, ocr_then_finish)))
root.mainloop()
