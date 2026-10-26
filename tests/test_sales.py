"""
Сервер продаж с имитацией Робокассы: покупка → оплата → ключ на экране, восстановление по номеру
заказа. Почта и персональные данные не собираются. python tests/test_sales.py
"""
import base64, hashlib, json, os, sys, threading, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "server"))
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import license as L

k = Ed25519PrivateKey.generate()
L.PUBLIC_KEY_B64 = base64.b64encode(k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()

LOGIN, PW1, PW2 = "mandarin", "pass-one", "pass-two"
STATE = {}   # InvId → (код состояния, зачисленная сумма); 100 — оплачено, 10 — отменено


def rk_sig(*parts):
    return hashlib.sha256(":".join(str(p) for p in parts).encode()).hexdigest()


class FakeRobokassa(BaseHTTPRequestHandler):   # имитация OpStateExt
    def do_GET(self):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
        inv = q.get("InvoiceID", "")
        assert q.get("Signature", "").lower() == rk_sig(LOGIN, inv, PW2), "OpState: неверная подпись запроса"
        code, out = STATE.get(inv, (5, 0.0))   # нет записи → счёт лишь выставлен
        xml = ('<?xml version="1.0" encoding="utf-8"?>'
               '<OperationStateResponse xmlns="http://auth.robokassa.ru/Merchant/WebService/Service">'
               '<Result><Code>0</Code></Result>'
               f'<State><Code>{code}</Code></State>'
               f'<Info><IncCurrLabel>Card</IncCurrLabel><OutSum>{out}</OutSum></Info>'
               '</OperationStateResponse>').encode()
        self.send_response(200); self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(xml))); self.end_headers(); self.wfile.write(xml)

    def log_message(self, *a): pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeRobokassa)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ.update({
    "ROBOKASSA_OPSTATE": f"http://127.0.0.1:{srv.server_port}/",
    "ROBOKASSA_LOGIN": LOGIN, "ROBOKASSA_PASSWORD1": PW1, "ROBOKASSA_PASSWORD2": PW2, "ROBOKASSA_HASH": "sha256",
    "PRICE_PRO": "199", "SITE_URL": "https://mandarinappkon.github.io/mandarin",
    "ALLOWED_ORIGINS": "https://mandarinappkon.github.io",
    "MANDARIN_PRIVATE_KEY": base64.b64encode(k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())).decode(),
})
import sales
ok = 0


def check(name, cond, extra=""):
    global ok
    assert cond, f"{name} {extra}"
    ok += 1; print("  ok ", name, extra)


def call(method, q, body=None, origin="https://mandarinappkon.github.io"):
    ev = {"httpMethod": method, "queryStringParameters": q, "headers": {"Origin": origin},
          "body": json.dumps(body) if body is not None else "", "isBase64Encoded": False}
    r = sales.handler(ev, None)
    txt = r["body"] or ""
    try:
        parsed = json.loads(txt)
    except ValueError:
        parsed = txt
    return r["statusCode"], parsed, r["headers"]


def cb(out, inv, sealed, pw):   # параметры callback'а Робокассы с корректной подписью
    return {"OutSum": out, "InvId": inv, "Shp_d": sealed,
            "SignatureValue": rk_sig(f"{out}:{inv}:{pw}:Shp_d={sealed}")}


# ── покупка (без почты) ──
st, out, hd = call("POST", {"a": "buy"}, {"agree": True})
check("покупка даёт подписанную ссылку Робокассы", st == 200 and out["confirmation_url"].startswith("https://auth.robokassa.ru"))
check("CORS для своего сайта", hd.get("Access-Control-Allow-Origin") == "https://mandarinappkon.github.io")
inv, sealed = out["payment_id"], out["token"]
check("цена берётся с сервера (OutSum=199.00)", "OutSum=199.00" in out["confirmation_url"])
check("в заказе нет персональных данных (в Shp_d только редакция)", sales._open(sealed) == {"ed": "pro"})
check("без согласия с офертой — отказ", call("POST", {"a": "buy"}, {})[0] == 400)

# ── до оплаты ключа нет ──
STATE[inv] = (5, 0.0)
st, out, _ = call("GET", {"a": "order", **cb("199.00", inv, sealed, PW1)})
check("до оплаты — pending", st == 200 and out == {"status": "pending"})

# ── ResultURL: подделка подписи и неоплаченный счёт ──
bad = {"OutSum": "199.00", "InvId": inv, "Shp_d": sealed, "SignatureValue": "deadbeef"}
check("уведомление с неверной подписью отклонено", call("POST", {"a": "webhook", **bad})[0] == 403)
st, body, _ = call("POST", {"a": "webhook", **cb("199.00", inv, sealed, PW2)})
check("подпись верна, но счёт не оплачен — 409", st == 409)

# ── оплата прошла ──
STATE[inv] = (100, 199.0)
st, body, _ = call("POST", {"a": "webhook", **cb("199.00", inv, sealed, PW2)})
check("после оплаты ResultURL отвечает ровно OK<InvId>", st == 200 and body == f"OK{inv}")

# ── страница «спасибо» показывает ключ ──
st, out, _ = call("GET", {"a": "order", **cb("199.00", inv, sealed, PW1)})
check("страница «спасибо» выдаёт ключ и номер заказа", st == 200 and out["key"].startswith("MNDR-") and out["order"] == inv)
KEY = out["key"]
check("ключ из страницы действителен", L.verify_key(KEY).edition == 1)
check("в ответе нет почты/персональных данных", "email" not in out)
tampered = cb("199.00", inv, sealed, PW1); tampered["SignatureValue"] = "00bad00"
check("без верной подписи SuccessURL ключ не выдаётся", call("GET", {"a": "order", **tampered})[1] == {"status": "pending"})

# ── восстановление по номеру заказа ──
st, out, _ = call("GET", {"a": "recover", "id": inv})
check("восстановление по номеру заказа даёт тот же ключ", st == 200 and out["key"] == KEY)
check("восстановление: номер не цифрами — отказ", call("GET", {"a": "recover", "id": "abc"})[0] == 400)
check("восстановление: неоплаченный номер — не найден", call("GET", {"a": "recover", "id": "123456789"})[0] == 404)

# ── отменённый платёж ──
STATE[inv] = (10, 0.0)
check("отменённый платёж виден как canceled", call("GET", {"a": "order", **cb("199.00", inv, sealed, PW1)})[1] == {"status": "canceled"})

# ── оплата меньшей суммы (подмена цены) — ключ на экране не выдаётся ──
st, out, _ = call("POST", {"a": "buy"}, {"agree": True})
inv2, sealed2 = out["payment_id"], out["token"]
STATE[inv2] = (100, 1.0)
check("оплата меньшей суммы — страница не выдаёт ключ", call("GET", {"a": "order", **cb("1.00", inv2, sealed2, PW1)})[1] == {"status": "pending"})

# ── режим чеков «Робочеки» ──
os.environ["RECEIPT"] = "robokassa"
st, out, _ = call("POST", {"a": "buy"}, {"agree": True})
check("режим чеков добавляет Receipt в ссылку оплаты", "Receipt=" in out["confirmation_url"])
os.environ["RECEIPT"] = "none"

check("health", call("GET", {"a": "health"})[1]["editions"] == {"pro": "199"})
print(f"\nСервер продаж: {ok} ok")
srv.shutdown()
