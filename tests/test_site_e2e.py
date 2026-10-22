"""
Сквозная проверка продажи в браузере (Playwright + имитация Робокассы):
    python tests/test_site_e2e.py [папка_для_скриншотов]
"""
import base64, hashlib, json, os, re, shutil, sys, tempfile, threading, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer, SimpleHTTPRequestHandler
from functools import partial
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "server"))
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import license as L
SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else None

k = Ed25519PrivateKey.generate()
L.PUBLIC_KEY_B64 = base64.b64encode(k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
LOGIN, PW1, PW2 = "mandarin", "p-one", "p-two"
STATE = {}        # InvId → (код состояния, сумма)
CFG = {}          # заполним адресами серверов после старта


def rk_sig(*parts):
    return hashlib.sha256(":".join(str(p) for p in parts).encode()).hexdigest()


def start(handler_cls):
    s = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=s.serve_forever, daemon=True).start(); return s


class RK(BaseHTTPRequestHandler):          # имитация Робокассы: страница оплаты + OpStateExt
    def log_message(self, *a): pass

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path); q = dict(urllib.parse.parse_qsl(u.query))
        if u.path.endswith("/OpStateExt"):
            inv = q.get("InvoiceID", "")
            assert q.get("Signature", "").lower() == rk_sig(LOGIN, inv, PW2)
            code, out = STATE.get(inv, (5, 0.0))
            xml = ('<?xml version="1.0" encoding="utf-8"?>'
                   '<OperationStateResponse xmlns="http://auth.robokassa.ru/Merchant/WebService/Service">'
                   '<Result><Code>0</Code></Result>'
                   f'<State><Code>{code}</Code></State>'
                   f'<Info><OutSum>{out}</OutSum></Info></OperationStateResponse>').encode()
            self.send_response(200); self.send_header("Content-Type", "text/xml")
            self.send_header("Content-Length", str(len(xml))); self.end_headers(); self.wfile.write(xml); return
        # страница оплаты: «покупатель оплатил» → ResultURL → возврат на Success URL
        out, inv, sealed = q.get("OutSum", ""), q.get("InvId", ""), q.get("Shp_d", "")
        STATE[inv] = (100, float(out))
        import sales
        cb = {"a": "webhook", "OutSum": out, "InvId": inv, "Shp_d": sealed,
              "SignatureValue": rk_sig(f"{out}:{inv}:{PW2}:Shp_d={sealed}")}
        sales.dispatch("POST", cb, "", "", None)
        sp = urllib.parse.urlencode({"OutSum": out, "InvId": inv, "Shp_d": sealed,
                                     "SignatureValue": rk_sig(f"{out}:{inv}:{PW1}:Shp_d={sealed}")})
        self.send_response(302); self.send_header("Location", f"{CFG['site']}/success.html?{sp}")
        self.end_headers()


class API(BaseHTTPRequestHandler):         # функция продаж (как в облаке)
    def log_message(self, *a): pass
    def _go(self):
        import sales
        u = urllib.parse.urlsplit(self.path); n = int(self.headers.get("Content-Length") or 0)
        st, hd, out = sales.dispatch(self.command, dict(urllib.parse.parse_qsl(u.query)),
                                     self.rfile.read(n).decode() if n else "",
                                     self.headers.get("Content-Type", ""), self.headers.get("Origin"))
        self.send_response(st); [self.send_header(a, b) for a, b in hd.items()]; d = out.encode()
        self.send_header("Content-Length", str(len(d))); self.end_headers(); self.wfile.write(d)
    do_GET = do_POST = do_OPTIONS = _go


rk = start(RK); api = start(API)
site_dir = Path(tempfile.mkdtemp()); shutil.copytree(ROOT / "site", site_dir, dirs_exist_ok=True)
site = start(partial(SimpleHTTPRequestHandler, directory=str(site_dir)))
SITE = f"http://127.0.0.1:{site.server_port}"; API_URL = f"http://127.0.0.1:{api.server_port}/"
CFG["site"] = SITE
for f in ("index.html", "success.html"):                  # подставляем адрес сервера, как сделает продавец
    p = site_dir / f; s = p.read_text("utf-8"); p.write_text(re.sub(r'API_URL: ""', f'API_URL: "{API_URL}"', s, count=1), "utf-8")
os.environ.update({
    "ROBOKASSA_URL": f"http://127.0.0.1:{rk.server_port}/Merchant/Index.aspx",
    "ROBOKASSA_OPSTATE": f"http://127.0.0.1:{rk.server_port}/Merchant/WebService/Service.asmx/OpStateExt",
    "ROBOKASSA_LOGIN": LOGIN, "ROBOKASSA_PASSWORD1": PW1, "ROBOKASSA_PASSWORD2": PW2, "ROBOKASSA_HASH": "sha256",
    "PRICE_PRO": "990", "SITE_URL": SITE, "ALLOWED_ORIGINS": SITE, "MAIL_FROM": "s@m.ru", "SMTP_HOST": "x", "SMTP_PASSWORD": "x",
    "MANDARIN_PRIVATE_KEY": base64.b64encode(k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())).decode()})
import sales
MAIL = []; sales.send_key_email = lambda e, key, ed, oid: MAIL.append((e, key))

from playwright.sync_api import sync_playwright
ok = 0
def check(name, cond, extra=""):
    global ok
    assert cond, f"{name} {extra}"; ok += 1; print("  ok ", name, extra)
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1280, "height": 900})
    errors = []; pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(SITE + "/index.html"); pg.wait_for_timeout(600)
    if SHOTS: pg.screenshot(path=str(SHOTS / "site_desktop.png"))
    # демо колеса: Ctrl-колесо для HEIC, клик по плитке
    pg.click("button[data-file=heic]"); pg.click("button[data-mode=tools]")
    tiles = pg.locator("#wheel .tile"); check("демо: колесо инструментов для фото", tiles.count() == 8)
    tiles.nth(3).click(); pg.wait_for_timeout(1400)
    check("демо: карточка «Готово»", "Готово" in pg.inner_text("#t-title"), pg.inner_text("#t-sub"))
    # перетаскивание файла на плитку
    pg.click("button[data-mode=convert]"); src = pg.locator("button[data-file=pdf]").bounding_box()
    tb = pg.locator("#wheel .tile").nth(1).bounding_box(); pg.wait_for_timeout(1700)
    pg.mouse.move(src["x"] + 20, src["y"] + 15); pg.mouse.down()
    for i in range(1, 16): pg.mouse.move(src["x"] + 20 + (tb["x"] + tb["width"] / 2 - src["x"] - 20) * i / 15, src["y"] + 15 + (tb["y"] + tb["height"] / 2 - src["y"] - 15) * i / 15)
    pg.mouse.up(); pg.wait_for_timeout(1300)
    check("демо: перетаскивание PDF на XLSX", "XLSX" in pg.inner_text("#t-title"), pg.inner_text("#t-sub"))
    # покупка
    pg.click("#pay-btn"); check("без почты — подсказка", "почты" in pg.inner_text("#form-msg"))
    pg.fill("#email", "Anna.Petrova@mail.ru"); pg.click("#pay-btn")
    check("без согласия с офертой — подсказка", "оферт" in pg.inner_text("#form-msg"))
    pg.check("#agree")
    with pg.expect_navigation(url=re.compile(r"success\.html")): pg.click("#pay-btn")
    pg.wait_for_selector("#key-box:not([hidden])", timeout=15000)
    key = pg.inner_text("#key").strip()
    if SHOTS: pg.screenshot(path=str(SHOTS / "site_success.png"), full_page=True)
    check("после оплаты ключ на экране", key.startswith("MNDR-"), pg.inner_text("#state"))
    check("тот же ключ ушёл на почту", MAIL and MAIL[-1] == ("Anna.Petrova@mail.ru", key))
    check("ключ активируется в программе", L.verify_key(key).email_tag == L.email_tag("anna.petrova@mail.ru").hex())
    check("почта не светится в адресе оплаты", "Anna.Petrova" not in json.dumps(list(STATE.keys())))
    # телефон
    m = b.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
    m.goto(SITE + "/index.html"); m.wait_for_timeout(500)
    sw = m.evaluate("document.documentElement.scrollWidth")
    check("телефон: нет горизонтальной прокрутки", sw <= 390, f"scrollWidth={sw}")
    if SHOTS:
        m.screenshot(path=str(SHOTS / "site_mobile.png"))
        d = b.new_page(viewport={"width": 1280, "height": 900}, color_scheme="dark"); d.goto(SITE + "/index.html"); d.wait_for_timeout(500)
        d.screenshot(path=str(SHOTS / "site_dark.png"))
        for doc in ("oferta.html", "privacy.html"):
            q = b.new_page(viewport={"width": 900, "height": 1100}); q.goto(f"{SITE}/{doc}"); q.screenshot(path=str(SHOTS / f"site_{doc[:-5]}.png"))
    check("без ошибок JavaScript", not errors, str(errors))
    b.close()
print(f"\nСайт и оплата: {ok} ok")
