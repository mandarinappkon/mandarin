"""Проверка функций PDF Pro: python tests/test_pdftools.py"""
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine  # noqa: E402
import pdftools as T  # noqa: E402

fitz = engine._fitz()
ok = fail = skip = 0


def check(name, fn):
    global ok, fail, skip
    try:
        msg = fn()
        ok += 1
        print(f"  ok   {name:28} {msg or ''}")
    except SkipTest as e:
        skip += 1
        print(f"  skip {name:28} {e}")
    except Exception as e:
        fail += 1
        print(f"  FAIL {name:28} {type(e).__name__}: {e}")
        traceback.print_exc(limit=3)


class SkipTest(Exception):
    pass


def make_samples(d: Path):
    font = T.cyr_fontfile()
    doc = fitz.open()
    for i in range(3):
        p = doc.new_page()
        p.insert_text((72, 80), f"Договор № {i + 1}", fontsize=18, fontname="F", fontfile=font)
        p.insert_text((72, 120), "Контакты: ivan.petrov@mail.ru, тел. +7 (916) 123-45-67", fontsize=11,
                      fontname="F", fontfile=font)
        p.insert_text((72, 140), "Паспорт 45 12 345678, ИНН 771234567890, карта 4276 1234 5678 9012",
                      fontsize=11, fontname="F", fontfile=font)
        p.insert_text((72, 170), "Стоимость услуг составляет 1000 рублей.", fontsize=11, fontname="F", fontfile=font)
    # таблица
    p = doc[0]
    y0 = 220
    rows = [["Товар", "Кол-во", "Цена"], ["Мандарины", "10", "250"], ["Апельсины", "5", "180"]]
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            rect = fitz.Rect(72 + c * 140, y0 + r * 24, 72 + (c + 1) * 140, y0 + (r + 1) * 24)
            p.draw_rect(rect, color=(0, 0, 0), width=0.8)
            p.insert_text((rect.x0 + 6, rect.y1 - 7), val, fontsize=11, fontname="F", fontfile=font)
    a = d / "договор.pdf"
    doc.save(a)
    # вторая версия для сравнения
    doc[0].add_redact_annot(doc[0].search_for("1000")[0])
    doc[0].apply_redactions()
    doc[0].insert_text((300, 170), "1500", fontsize=11, fontname="F", fontfile=font)
    b = d / "договор v2.pdf"
    doc.save(b)
    doc.close()
    # «скан»
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGB", (1654, 2339), "white")
    dr = ImageDraw.Draw(im)
    f = ImageFont.truetype(font, 60)
    dr.text((150, 200), "Акт выполненных работ", font=f, fill="black")
    dr.text((150, 320), "Invoice number 2026-15", font=f, fill="black")
    im.save(d / "скан.png")
    s = fitz.open()
    pg = s.new_page(width=595, height=842)
    pg.insert_image(pg.rect, filename=str(d / "скан.png"))
    scan = d / "скан.pdf"
    s.save(scan)
    return a, b, scan


def main():
    d = Path(tempfile.mkdtemp(prefix="pdfpro-"))
    a, b, scan = make_samples(d)

    def t_protect():
        r = T.protect(a, "1234", allow_print=True)
        x = fitz.open(r.outputs[0])
        assert x.needs_pass, "нет пароля"
        assert x.authenticate("1234")
        u = T.unlock(r.outputs[0], "1234")
        y = fitz.open(u.outputs[0])
        assert not y.needs_pass and "Договор" in y[0].get_text()
        try:
            T.unlock(r.outputs[0], "0000")
            raise AssertionError("неверный пароль принят")
        except engine.EngineError:
            pass
        return "AES-256, снятие, неверный пароль отклонён"
    check("пароль / снятие", t_protect)

    def t_redact():
        r = T.redact_file(a, ["email", "phone", "passport", "inn", "card"], words=["Стоимость"])
        txt = fitz.open(r.outputs[0])[0].get_text()
        for s in ("ivan.petrov", "123-45-67", "345678", "771234567890", "9012", "Стоимость"):
            assert s not in txt, f"осталось: {s}"
        assert "Договор" in txt
        return r.message
    check("зачернение по шаблонам", t_redact)

    def t_watermark():
        r = T.watermark_file(a, "КОНФИДЕНЦИАЛЬНО")
        assert len(fitz.open(r.outputs[0])[0].get_images()) >= 1
    check("водяной знак", t_watermark)

    def t_numbers():
        r = T.numbers_file(a, fmt="Стр. {n} из {total}", position="bottom-center")
        t = fitz.open(r.outputs[0])[2].get_text()
        assert "Стр. 3 из 3" in t, t[-80:]
        r2 = T.header_footer_file(a, header="{file}", footer="{date} · {n}/{total}")
        t2 = fitz.open(r2.outputs[0])[1].get_text()
        assert "договор.pdf" in t2 and "2/3" in t2
        r3 = T.bates_files([a, b], prefix="ДЕЛО-", start=1, digits=5)
        t3 = fitz.open(r3.outputs[1])[2].get_text()
        assert "ДЕЛО-00006" in t3, t3[-60:]
        return r3.message
    check("номера / колонтитулы / Bates", t_numbers)

    def t_rotated():
        doc = fitz.open(a)
        doc[0].set_rotation(90)
        rp = d / "повёрнут.pdf"
        doc.save(rp)
        r = T.numbers_file(rp, fmt="Лист {n}", position="bottom-right")
        p = fitz.open(r.outputs[0])[0]
        hits = p.search_for("Лист 1")
        assert hits, "номер не найден"
        h = hits[0] * p.rotation_matrix      # search_for даёт координаты неповёрнутой страницы
        h.normalize()
        # на повёрнутой странице номер должен оказаться внизу справа видимой страницы
        assert h.y0 > p.rect.height * 0.8 and h.x0 > p.rect.width * 0.5, (h, p.rect)
        return f"номер в {h.x0:.0f},{h.y0:.0f} на листе {p.rect.width:.0f}×{p.rect.height:.0f}"
    check("номера на повёрнутой стр.", t_rotated)

    def t_crop():
        r = T.crop_file(a, "auto", 5)
        p = fitz.open(r.outputs[0])[0]
        assert p.rect.width < 595 and p.rect.height < 842, p.rect
        return f"{p.rect.width:.0f}×{p.rect.height:.0f} pt"
    check("обрезка полей", t_crop)

    def t_xlsx():
        import openpyxl
        r = T.export_xlsx([a])
        wb = openpyxl.load_workbook(r.outputs[0])
        ws = wb.worksheets[0]
        vals = [[c.value for c in row] for row in ws.iter_rows()]
        assert vals[0] == ["Товар", "Кол-во", "Цена"], vals[:2]
        assert vals[1][2] == 250, vals[1]
        return r.message
    check("экспорт в Excel", t_xlsx)

    def t_pptx():
        from pptx import Presentation
        r = T.export_pptx([a])
        prs = Presentation(r.outputs[0])
        assert len(prs.slides) == 3
        return "3 слайда"
    check("экспорт в PowerPoint", t_pptx)

    def t_ocr():
        if not T.ocr_available():
            raise SkipTest("нет tessdata")
        r = T.ocr_file(scan)
        x = fitz.open(r.outputs[0])
        txt = x[0].get_text()
        assert "Акт" in txt and "2026" in txt, txt
        assert abs(x[0].rect.width - 595) < 2, x[0].rect
        try:
            T.ocr_file(a)
            raise AssertionError("документ с текстом не должен распознаваться")
        except engine.EngineError:
            pass
        return f"{r.message} · «{txt.split(chr(10))[0]}»"
    check("OCR сканов", t_ocr)

    def t_compare():
        r = T.compare_files(a, b)
        assert "Удалено слов: 1" in r.message and "добавлено: 1" in r.message, r.message
        x = fitz.open(r.outputs[0])
        assert len(x) == 4
        return r.message
    check("сравнение версий", t_compare)

    def t_forms():
        doc = fitz.open(a)
        T.add_field(doc[0], (72, 400, 300, 420), "text", "ФИО")
        T.add_field(doc[0], (72, 430, 86, 444), "checkbox", "Согласен")
        T.add_field(doc[0], (72, 460, 300, 480), "combobox", "Город", ["Москва", "Казань"])
        out = d / "форма.pdf"
        doc.save(out)
        doc = fitz.open(out)
        names = [f["name"] for f in T.list_fields(doc)]
        assert names == ["ФИО", "Согласен", "Город"], names
        n = T.fill_fields(doc, {"ФИО": "Иванов И.И.", "Согласен": True, "Город": "Казань"})
        filled = d / "форма заполнена.pdf"
        doc.save(filled)
        f = {x["name"]: x["value"] for x in T.list_fields(fitz.open(filled))}
        assert f["ФИО"] == "Иванов И.И." and f["Город"] == "Казань" and f["Согласен"] not in ("Off", False), f
        fl = T.flatten_file(filled)
        assert not T.list_fields(fitz.open(fl.outputs[0]))
        return f"полей: {n}, сглаживание ок"
    check("формы: создать/заполнить/сгладить", t_forms)

    def t_sign():
        pfx = T.make_self_signed(d / "my.pfx", "Иванов Иван", "ivan@example.com", "pass")
        r = T.sign_with_certificate(a, pfx, "pass", reason="Согласовано", location="Москва")
        sigs = T.verify_signatures(r.outputs[0])
        assert sigs and sigs[0]["intact"], sigs
        # подделка: дописываем байты — подпись должна сломаться
        tampered = d / "подделка.pdf"
        data = bytearray(r.outputs[0].read_bytes())
        i = data.find(b"Stream") if b"Stream" in data else 2000
        data[i + 50] ^= 0x01
        tampered.write_bytes(bytes(data))
        try:
            bad = T.verify_signatures(tampered)
            broken = not bad or not bad[0]["intact"]
        except Exception:
            broken = True
        assert broken, "изменение не обнаружено"
        return f"{sigs[0]['signer']}: целостна; подделка обнаружена"
    check("подпись сертификатом", t_sign)

    def t_pdfa():
        if not T.pdfa_available():
            raise SkipTest("нет Ghostscript")
        # реальные PDF (Word/LibreOffice) и наши вписанные номера страниц должны пережить PDF/A
        numbered = T.numbers_file(b, fmt="Страница {n}").outputs[0]
        r = T.to_pdfa(numbered)
        raw = r.outputs[0].read_bytes()
        assert b"GTS_PDFA1" in raw and b"pdfaid" in raw, "нет признаков PDF/A"
        txt = fitz.open(r.outputs[0])[0].get_text()
        assert "Страница 1" in txt, txt[-60:]
        return r.message + " · кириллица в добавленном тексте сохранена"
    check("PDF/A", t_pdfa)

    def t_stamp():
        png = T.make_stamp_png("УТВЕРЖДЕНО", T.STAMPS["УТВЕРЖДЕНО"])
        w, h = T.png_size(png)
        assert w > 100 and h > 30
        return f"{w}×{h}"
    check("штамп", t_stamp)

    print(f"\nИтого: {ok} ok, {fail} fail, {skip} skip")
    shutil.rmtree(d, ignore_errors=True)
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    os.environ.setdefault("TESSDATA_PREFIX", str(Path(__file__).resolve().parents[1] / "tessdata"))
    main()
