"""
Сервер продаж с имитацией Робокассы: покупка → ResultURL-уведомление → ключ на почту и на экране.
Проверка статуса идёт через поддельный OpStateExt-сервис. python tests/test_sales.py
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
    "PRICE_PRO": "990", "SITE_URL": "https://mandarin-app.ru", "MAIL_FROM": "sales@mandarin-app.ru",
    "SMTP_HOST": "x", "SMTP_PASSWORD": "x",
    "MANDARIN_PRIVATE_KEY": base64.b64encode(k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())).decode(),
})
import sales
SENT = []
sales.send_key_email = lambda email, key, ed, oid: SENT.append((email, key, oid))
ok = 0


def check(name, cond, extra=""):
    global ok
    assert cond, f"{name} {extra}"
    ok += 1; print("  ok ", name, extra)


def call(method, q, body=None, origin="https://mandarin-app.ru"):
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


# ── покупка ──
st, out, hd = call("POST", {"a": "buy"}, {"email": "Ivan@mail.ru", "agree": True})
check("покупка даёт подписанную ссылку Робокассы", st == 200 and out["confirmation_url"].startswith("https://auth.robokassa.ru"))
check("CORS для своего сайта", hd.get("Access-Control-Allow-Origin") == "https://mandarin-app.ru")
inv, sealed = out["payment_id"], out["token"]
check("цена берётся с сервера (OutSum=990.00)", "OutSum=990.00" in out["confirmation_url"])
check("почта не светится в URL — только шифртекст Shp_d", "Ivan%40mail.ru" not in out["confirmation_url"] and "Shp_d=" in out["confirmation_url"])
check("без согласия с офертой — отказ", call("POST", {"a": "buy"}, {"email": "a@b.ru"})[0] == 400)
check("кривая почта — отказ", call("POST", {"a": "buy"}, {"email": "nope", "agree": True})[0] == 400)

# ── до оплаты ключа нет ──
STATE[inv] = (5, 0.0)
st, out, _ = call("GET", {"a": "order", **cb("990.00", inv, sealed, PW1)})
check("до оплаты — pending", st == 200 and out == {"status": "pending"})

# ── ResultURL: подделка подписи и неоплаченный счёт ──
bad = {"OutSum": "990.00", "InvId": inv, "Shp_d": sealed, "SignatureValue": "deadbeef"}
check("уведомление с неверной подписью отклонено", call("POST", {"a": "webhook", **bad})[0] == 403 and not SENT)
st, body, _ = call("POST", {"a": "webhook", **cb("990.00", inv, sealed, PW2)})
check("подпись верна, но счёт не оплачен — ключа нет", st == 409 and not SENT)

# ── оплата прошла ──
STATE[inv] = (100, 990.0)
st, body, _ = call("POST", {"a": "webhook", **cb("990.00", inv, sealed, PW2)})
check("после оплаты ответ ровно OK<InvId>", st == 200 and body == f"OK{inv}")
check("ключ ушёл на почту", len(SENT) == 1 and SENT[0][0] == "Ivan@mail.ru")
lic = L.verify_key(SENT[0][1])
check("ключ из письма действителен и привязан к почте", lic.email_tag == L.email_tag("ivan@mail.ru").hex())
call("POST", {"a": "webhook", **cb("990.00", inv, sealed, PW2)})
check("повторное уведомление даёт тот же ключ", SENT[1][1] == SENT[0][1])

# ── страница «спасибо» ──
st, out, _ = call("GET", {"a": "order", **cb("990.00", inv, sealed, PW1)})
check("страница «спасибо» показывает тот же ключ", st == 200 and out.get("key") == SENT[0][1], out.get("email", ""))
tampered = cb("990.00", inv, sealed, PW1); tampered["SignatureValue"] = "00bad00"
check("без верной подписи SuccessURL ключ не выдаётся", call("GET", {"a": "order", **tampered})[1] == {"status": "pending"})

# ── отменённый платёж ──
STATE[inv] = (10, 0.0)
check("отменённый платёж виден как canceled", call("GET", {"a": "order", **cb("990.00", inv, sealed, PW1)})[1] == {"status": "canceled"})

# ── оплата меньшей суммы (подмена цены) — ключ не выдаётся ──
st, out, _ = call("POST", {"a": "buy"}, {"email": "cheap@mail.ru", "agree": True})
inv2, sealed2 = out["payment_id"], out["token"]
STATE[inv2] = (100, 1.0)
check("оплата меньшей суммы отклоняется", call("POST", {"a": "webhook", **cb("1.00", inv2, sealed2, PW2)})[0] == 400)

# ── режим чеков «Робочеки» ──
os.environ["RECEIPT"] = "robokassa"
st, out, _ = call("POST", {"a": "buy"}, {"email": "ip@mail.ru", "agree": True})
check("режим чеков добавляет Receipt в ссылку оплаты", "Receipt=" in out["confirmation_url"])
os.environ["RECEIPT"] = "none"

check("health", call("GET", {"a": "health"})[1]["editions"] == {"pro": "990"})
print(f"\nСервер продаж: {ok} ok")
srv.shutdown()
