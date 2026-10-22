"""
Проверка редактора страниц на виртуальном экране:
    xvfb-run -a python tests/test_organizer.py [скриншот.png]
"""
import sys
import tempfile
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine  # noqa: E402
import organizer  # noqa: E402

COLORS = [(1, .55, .12), (.3, .55, 1), (.18, .62, .42), (.75, .36, 1), (1, .3, .43)]


def make_pdf(path, prefix, n, color):
    fitz = engine._fitz()
    doc = fitz.open()
    for i in range(n):
        p = doc.new_page(width=595, height=842)
        p.draw_rect(p.rect, color=None, fill=color)
        p.insert_text((60, 470), f"{prefix}{i + 1}", fontsize=220, color=(1, 1, 1))
    doc.save(path)


def page_labels(doc):
    out = []
    for p in doc:
        t = p.get_text().strip()
        out.append(f"{t}@{p.rotation}" if p.rotation else t)
    return out


def main():
    shot = sys.argv[1] if len(sys.argv) > 1 else None
    d = Path(tempfile.mkdtemp(prefix="org-test-"))
    a, b = d / "А.pdf", d / "Б.pdf"
    make_pdf(a, "A", 4, COLORS[0])
    make_pdf(b, "B", 3, COLORS[1])
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (800, 600), (46, 158, 106))
    ImageDraw.Draw(im).ellipse((250, 150, 550, 450), fill=(255, 255, 255))
    img = d / "фото.png"
    im.save(img)

    try:
        from tkinterdnd2 import TkinterDnD
        root = TkinterDnD.Tk()
    except Exception:
        root = tk.Tk()
    root.withdraw()
    org = organizer.PdfOrganizer(root, [a, b, img])
    results = {}

    def step1():
        assert len(org.pages) == 8, len(org.pages)
        assert page_labels(org.build_doc(org.pages))[:7] == ["A1", "A2", "A3", "A4", "B1", "B2", "B3"]
        # поворот A2, удаление A3, дубль B1
        p = org.pages
        org.rotate(90, [p[1]])
        org.delete([p[2]])
        org.sel = {org.pages[3].uid}           # B1
        org.duplicate()
        labels = page_labels(org.build_doc(org.pages))
        assert labels[:6] == ["A1", "A2@90", "A4", "B1", "B1", "B2"], labels
        # отмена дубля и удаления
        org.undo(); org.undo()
        labels = page_labels(org.build_doc(org.pages))
        assert labels[:4] == ["A1", "A2@90", "A3", "A4"], labels
        org.redo()
        results["ops"] = labels
        org.win.update()
        root.after(200, step2)

    def step2():
        # перетаскивание мышью: B3 (индекс 5 после удаления A3) в самое начало
        org.win.update()
        target = org.pages[5]
        assert page_labels(org.build_doc([target])) == ["B3"]
        x0, y0, x1, y1 = org.geom[target.uid]
        c = org.canvas
        sx, sy = (x0 + x1) / 2, (y0 + y1) / 2 - c.canvasy(0)
        fx0, fy0, _, fy1 = org.geom[org.pages[0].uid]
        tx, ty = fx0 - 5, (fy0 + fy1) / 2 - c.canvasy(0)
        c.event_generate("<ButtonPress-1>", x=int(sx), y=int(sy))
        for i in range(1, 11):
            c.event_generate("<B1-Motion>", x=int(sx + (tx - sx) * i / 10), y=int(sy + (ty - sy) * i / 10))
        c.event_generate("<ButtonRelease-1>", x=int(tx), y=int(ty))
        labels = page_labels(org.build_doc(org.pages))
        assert labels[0] == "B3", f"перетаскивание не сработало: {labels}"
        results["drag"] = labels
        # чередование: сортируем по файлам, затем A1 B1 A2 B2…
        org.sort_by_file()
        org.interleave()
        results["interleave"] = page_labels(org.build_doc(org.pages))
        assert results["interleave"] == ["A1", "B1", "", "A2@90", "B2", "A4", "B3"], results["interleave"]
        # сохранение
        out = d / "итог.pdf"
        org._write(org.pages, out)
        fitz = engine._fitz()
        saved = page_labels(fitz.open(out))
        assert saved == results["interleave"], saved
        results["saved"] = saved
        org.sel = {org.pages[1].uid, org.pages[2].uid}
        org.hover_uid = org.pages[4].uid
        org.redraw()
        root.after(1200, finish)

    def finish():
        if shot:
            import subprocess
            org.win.update()
            subprocess.run(["import", "-window", "root", shot])
        for k, v in results.items():
            print(f"  ok  {k:10} {' '.join(x or '□' for x in v)}")
        print("Редактор: все проверки пройдены")
        root.destroy()

    def guarded(fn):
        def run():
            try:
                fn()
            except Exception:
                import traceback
                traceback.print_exc()
                root.destroy()
                sys.exit(1)
        return run
    step1, step2, finish = guarded(step1), guarded(step2), guarded(finish)
    root.after(600, step1)
    root.mainloop()


if __name__ == "__main__":
    main()
