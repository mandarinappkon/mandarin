"""
Сервер продаж с имитацией ЮKassa: покупка → уведомление → ключ на почту и на экране.
    python tests/test_sales.py
"""
import base64, json, os, sys, threading, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "server"))
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import license as L

k = Ed25519PrivateKey.generate()
L.PUBLIC_KEY_B64 = base64.b64encode(k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
PAYMENTS = {}

class FakeYooKassa(BaseHTTPRequestHandler):
    def _auth_ok(self):
        return self.headers.get("Authorization") == "Basic " + base64.b64encode(b"777:test_secret").decode()
    def do_POST(self):
        assert self._auth_ok() and self.headers.get("Idempotence-Key")
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        pid = str(uuid.uuid4())
        PAYMENTS[pid] = {"id": pid, "status": "pending", "paid": False, "amount": body["amount"],
                         "metadata": body["metadata"], "receipt_sent": "receipt" in body,
                         "confirmation": {"type": "redirect", "confirmation_url": f"https://yoomoney.ru/checkout/{pid}"}}
        self._json(PAYMENTS[pid])
    def do_GET(self):
        assert self._auth_ok()
        pid = self.path.rsplit("/", 1)[-1]
        self._json(PAYMENTS[pid]) if pid in PAYMENTS else self._json({"type": "error"}, 404)
    def _json(self, obj, code=200):
        data = json.dumps(obj).encode(); self.send_response(code)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data)))
        self.end_headers(); self.wfile.write(data)
    def log_message(self, *a): pass

srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeYooKassa)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ.update({
    "YOOKASSA_API": f"http://127.0.0.1:{srv.server_port}/v3", "YOOKASSA_SHOP_ID": "777", "YOOKASSA_SECRET_KEY": "test_secret",
    "PRICE_PRO": "990", "SITE_URL": "https://mandarin-app.ru", "MAIL_FROM": "sales@mandarin-app.ru",
    "SMTP_HOST": "x", "SMTP_PASSWORD": "x",
    "MANDARIN_PRIVATE_KEY": base64.b64encode(k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())).decode(),
})
import sales
SENT = []
sales.send_key_email = lambda email, key, ed, pid: SENT.append((email, key, pid))
YK_IP, BAD_IP = "185.71.76.5", "8.8.8.8"
ok = 0
def check(name, cond, extra=""):
    global ok
    assert cond, f"{name} {extra}"
    ok += 1; print("  ok ", name, extra)

def call(method, q, body=None, ip=None, origin="https://mandarin-app.ru"):
    ev = {"httpMethod": method, "queryStringParameters": q, "headers": {"Origin": origin},
          "body": json.dumps(body) if body is not None else "", "isBase64Encoded": False,
          "requestContext": {"identity": {"sourceIp": ip or "1.2.3.4"}}}
    r = sales.handler(ev, None)
    return r["statusCode"], json.loads(r["body"]) if r["body"] else {}, r["headers"]

st, out, hd = call("POST", {"a": "buy"}, {"email": "Ivan@mail.ru", "agree": True})
check("покупка создаёт платёж и даёт ссылку оплаты", st == 200 and out["confirmation_url"].startswith("https://yoomoney"))
check("CORS для своего сайта", hd.get("Access-Control-Allow-Origin") == "https://mandarin-app.ru")
pid, token = out["payment_id"], out["token"]
check("цена берётся с сервера", PAYMENTS[pid]["amount"] == {"value": "990.00", "currency": "RUB"})
check("без согласия с офертой — отказ", call("POST", {"a": "buy"}, {"email": "a@b.ru"})[0] == 400)
check("кривая почта — отказ", call("POST", {"a": "buy"}, {"email": "nope", "agree": True})[0] == 400)

st, out, _ = call("GET", {"a": "order", "id": pid, "t": token})
check("до оплаты ключа нет", st == 200 and out == {"status": "pending"})

fake = {"type": "notification", "event": "payment.succeeded", "object": {"id": pid, "status": "succeeded"}}
st, out, _ = call("POST", {"a": "webhook"}, fake, ip=YK_IP)
check("поддельное уведомление (платёж не оплачен) не даёт ключ", st == 409 and not SENT)
check("уведомление не с адреса ЮKassa отклонено", call("POST", {"a": "webhook"}, fake, ip=BAD_IP)[0] == 403)

PAYMENTS[pid].update(status="succeeded", paid=True)          # покупатель оплатил
st, out, _ = call("POST", {"a": "webhook"}, fake, ip=YK_IP)
check("после оплаты ключ ушёл на почту", st == 200 and len(SENT) == 1 and SENT[0][0] == "Ivan@mail.ru")
lic = L.verify_key(SENT[0][1])
check("ключ из письма действителен и привязан к почте", lic.email_tag == L.email_tag("ivan@mail.ru").hex())
call("POST", {"a": "webhook"}, fake, ip=YK_IP)
check("повторное уведомление даёт тот же ключ", SENT[1][1] == SENT[0][1])

st, out, _ = call("GET", {"a": "order", "id": pid, "t": token})
check("страница «спасибо» показывает тот же ключ", st == 200 and out["key"] == SENT[0][1], out.get("email", ""))
check("чужой токен — ключ не выдаётся", call("GET", {"a": "order", "id": pid, "t": "guess"})[0] == 403)

# платёж на меньшую сумму (например, подменили цену в чужом магазине) — ключ не выдаётся
st, out, _ = call("POST", {"a": "buy"}, {"email": "cheap@mail.ru", "agree": True})
p2 = out["payment_id"]; PAYMENTS[p2].update(status="succeeded", paid=True, amount={"value": "1.00", "currency": "RUB"})
check("оплата меньшей суммы отклоняется", call("POST", {"a": "webhook"}, {"event": "payment.succeeded", "object": {"id": p2}}, ip=YK_IP)[0] == 400)

os.environ["RECEIPT"] = "yookassa"
st, out, _ = call("POST", {"a": "buy"}, {"email": "ip@mail.ru", "agree": True})
check("режим чеков 54-ФЗ добавляет чек в платёж", PAYMENTS[out["payment_id"]]["receipt_sent"])
check("health", call("GET", {"a": "health"})[1]["editions"] == {"pro": "990"})
print(f"\nСервер продаж: {ok} ok")
srv.shutdown()
