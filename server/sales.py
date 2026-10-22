"""
Mandarin — сервер продаж: ЮKassa → ключ Pro покупателю. Без базы данных.

Как это работает
  1. Сайт: покупатель вводит почту → POST ?a=buy → сервер создаёт платёж в ЮKassa и возвращает
     ссылку на оплату. Цена задаётся ТОЛЬКО на сервере.
  2. ЮKassa присылает уведомление payment.succeeded → POST ?a=webhook. Сервер не доверяет
     уведомлению: заново запрашивает платёж у ЮKassa, сверяет статус, сумму и валюту, выпускает
     ключ и отправляет письмо. Ключ вычисляется из номера платежа, поэтому повторные уведомления
     дают тот же ключ (без дублей и без базы данных).
  3. Покупатель возвращается на сайт (success.html) → GET ?a=order&id=…&t=… → ключ сразу на экране.
     t — случайный токен заказа из метаданных платежа: без него чужой ключ не получить.

Запуск
  * Yandex Cloud Functions: точка входа  sales.handler  (см. SALES.md).
  * Свой сервер / проверка:  python server/sales.py --port 8080

Настройки — переменные окружения (пример — server/env.example).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import smtplib
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import license as L  # noqa: E402  (для Yandex Cloud Functions положите license.py рядом с sales.py)

YOOKASSA_API = os.environ.get("YOOKASSA_API", "https://api.yookassa.ru/v3")

# подсети, с которых ЮKassa шлёт уведомления (https://yookassa.ru/developers/using-api/webhooks)
YOOKASSA_NETS = [ipaddress.ip_network(n) for n in (
    "185.71.76.0/27", "185.71.77.0/27", "77.75.153.0/25", "77.75.156.11/32", "77.75.156.35/32",
    "77.75.154.128/25", "2a02:5180::/32")]

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")


def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        raise RuntimeError(f"Не задана переменная окружения {name}")
    return v


def editions() -> dict:
    """Редакции и цены (рубли). PRICE_PRO обязательна, PRICE_BUSINESS — если продаёте вторую редакцию."""
    out = {"pro": (1, env("PRICE_PRO", required=True), "Mandarin Pro")}
    if env("PRICE_BUSINESS"):
        out["business"] = (2, env("PRICE_BUSINESS"), "Mandarin Pro для бизнеса")
    return out


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ───────────────────────── ЮKassa ─────────────────────────

def _yk(method: str, path: str, body: dict | None = None, idem: str | None = None) -> dict:
    shop, secret = env("YOOKASSA_SHOP_ID", required=True), env("YOOKASSA_SECRET_KEY", required=True)
    req = urllib.request.Request(f"{YOOKASSA_API}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{shop}:{secret}".encode()).decode())
    req.add_header("Content-Type", "application/json")
    if idem:
        req.add_header("Idempotence-Key", idem)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise HttpError(502, f"ЮKassa ответила {e.code}: {detail}")


def create_payment(email: str, edition_key: str) -> dict:
    eds = editions()
    if edition_key not in eds:
        raise HttpError(400, "Неизвестная редакция")
    code, price, title = eds[edition_key]
    token = secrets.token_urlsafe(18)
    site = env("SITE_URL", required=True).rstrip("/")
    body = {
        "amount": {"value": f"{float(price):.2f}", "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": f"{site}/success.html?t={token}"},
        "description": f"{title} — лицензия"[:128],
        "metadata": {"email": email, "token": token, "edition": edition_key, "product": "mandarin"},
    }
    if env("RECEIPT", "none") == "yookassa":
        # чеки 54-ФЗ через «Чеки от ЮKassa» (для ИП/ООО). Для самозанятых — RECEIPT=none:
        # ЮKassa сама регистрирует доход в «Мой налог», если это включено в личном кабинете.
        body["receipt"] = {
            "customer": {"email": email},
            "items": [{
                "description": f"{title}: неисключительная лицензия"[:128],
                "quantity": "1.00",
                "amount": body["amount"],
                "vat_code": int(env("VAT_CODE", "1")),          # 1 — без НДС
                "payment_subject": "intellectual_activity",
                "payment_mode": "full_payment",
            }],
        }
    p = _yk("POST", "/payments", body, idem=str(uuid.uuid4()))
    return {"payment_id": p["id"], "confirmation_url": p["confirmation"]["confirmation_url"], "token": token}


def fetch_payment(payment_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-fA-F-]{20,64}", payment_id or ""):
        raise HttpError(400, "Неверный номер платежа")
    return _yk("GET", f"/payments/{payment_id}")


def check_paid(p: dict) -> tuple[str, int]:
    """Возвращает (почта, код редакции) для оплаченного платежа Mandarin, иначе HttpError."""
    if p.get("status") != "succeeded" or not p.get("paid"):
        raise HttpError(409, "Платёж ещё не оплачен")
    meta = p.get("metadata") or {}
    if meta.get("product") != "mandarin":
        raise HttpError(400, "Платёж не относится к Mandarin")
    eds = editions()
    ed = eds.get(meta.get("edition", ""))
    if not ed:
        raise HttpError(400, "Неизвестная редакция")
    amount = p.get("amount") or {}
    if amount.get("currency") != "RUB" or float(amount.get("value", 0)) + 0.001 < float(ed[1]):
        raise HttpError(400, "Сумма платежа не совпадает с ценой")
    email = meta.get("email", "")
    if not EMAIL_RE.match(email):
        raise HttpError(400, "В платеже нет почты покупателя")
    return email, ed[0]


# ───────────────────────── ключ и письмо ─────────────────────────

def key_for(payment_id: str, email: str, edition: int) -> str:
    priv = base64.b64decode(env("MANDARIN_PRIVATE_KEY", required=True))
    lid = hashlib.sha256(("yookassa:" + payment_id).encode()).digest()[:12]
    return L.make_key(priv, lid, email, edition)


def send_key_email(email: str, key: str, edition: int, payment_id: str):
    name = L.EDITIONS.get(edition, "Pro")
    download = env("DOWNLOAD_URL", env("SITE_URL", "") + "/#download")
    support = env("SUPPORT_EMAIL", L.SUPPORT_EMAIL)
    msg = EmailMessage()
    msg["Subject"] = f"Ваш ключ Mandarin {name}"
    msg["From"] = formataddr((env("MAIL_FROM_NAME", "Mandarin"), env("MAIL_FROM", required=True)))
    msg["To"] = email
    msg["Reply-To"] = support
    text = (f"Спасибо за покупку Mandarin {name}!\n\nВаш ключ:\n\n{key}\n\n"
            "Как активировать: щёлкните значок Mandarin в трее → «Лицензия…» (или в окне программы "
            "«Купить Pro») → вставьте ключ → «Активировать».\n\n"
            f"Скачать программу: {download}\nНомер заказа: {payment_id}\n"
            f"Вопросы: {support} — просто ответьте на это письмо.\n")
    msg.set_content(text)
    k = html.escape(key)
    msg.add_alternative(f"""<!doctype html><html><body style="font-family:Segoe UI,Arial,sans-serif;color:#141418;
background:#f4f4f5;padding:24px"><div style="max-width:560px;margin:auto;background:#fff;border-radius:16px;padding:28px">
<div style="font-size:22px;font-weight:700">🍊 Спасибо за покупку Mandarin {html.escape(name)}!</div>
<p style="color:#6f6f7a">Ваш ключ (скопируйте его целиком):</p>
<div style="font-family:Consolas,monospace;font-size:13px;background:#fff1e8;border:1px solid #ffd2b8;border-radius:10px;
padding:14px;word-break:break-all">{k}</div>
<p><b>Как активировать:</b> значок Mandarin в трее → «Лицензия…» → вставьте ключ → «Активировать».</p>
<p><a href="{html.escape(download)}" style="background:#ff5a0a;color:#fff;padding:10px 18px;border-radius:10px;
text-decoration:none;font-weight:600">Скачать Mandarin</a></p>
<p style="color:#6f6f7a;font-size:13px">Номер заказа: {html.escape(payment_id)}<br>Вопросы — просто ответьте на это письмо
или напишите на {html.escape(support)}.</p></div></body></html>""", subtype="html")
    host, port = env("SMTP_HOST", required=True), int(env("SMTP_PORT", "465"))
    user, pw = env("SMTP_USER", env("MAIL_FROM")), env("SMTP_PASSWORD", required=True)
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as s:
            s.login(user, pw)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls(context=ctx)
            s.login(user, pw)
            s.send_message(msg)


# ───────────────────────── маршруты ─────────────────────────

def route_buy(body: dict) -> dict:
    email = str(body.get("email", "")).strip()
    if not EMAIL_RE.match(email) or len(email) > 120:
        raise HttpError(400, "Проверьте адрес почты")
    if not body.get("agree"):
        raise HttpError(400, "Нужно согласие с условиями оферты")
    return create_payment(email, str(body.get("edition", "pro")))


def route_webhook(body: dict, source_ip: str | None) -> dict:
    if env("CHECK_WEBHOOK_IP", "1") == "1" and source_ip:
        try:
            ip = ipaddress.ip_address(source_ip)
            if not any(ip in n for n in YOOKASSA_NETS):
                raise HttpError(403, "Уведомление не от ЮKassa")
        except ValueError:
            raise HttpError(403, "Неизвестный адрес")
    if body.get("event") != "payment.succeeded":
        return {"ok": True, "ignored": body.get("event")}
    pid = (body.get("object") or {}).get("id", "")
    p = fetch_payment(pid)                     # не верим уведомлению — спрашиваем ЮKassa сами
    email, edition = check_paid(p)
    key = key_for(p["id"], email, edition)
    send_key_email(email, key, edition, p["id"])   # исключение → 500 → ЮKassa повторит (ключ тот же)
    return {"ok": True}


def route_order(q: dict) -> dict:
    p = fetch_payment(q.get("id", ""))
    token = (p.get("metadata") or {}).get("token", "")
    if not token or not hmac.compare_digest(token, q.get("t", "")):
        raise HttpError(403, "Заказ не найден")
    if p.get("status") in ("pending", "waiting_for_capture"):
        return {"status": "pending"}
    if p.get("status") == "canceled":
        return {"status": "canceled"}
    email, edition = check_paid(p)
    user, _, domain = email.partition("@")
    return {"status": "succeeded", "key": key_for(p["id"], email, edition),
            "edition": L.EDITIONS.get(edition, "Pro"), "email": (user[:2] + "•••@" + domain)}


def dispatch(method: str, query: dict, body_text: str, source_ip: str | None, origin: str | None):
    """Общая логика для всех способов запуска → (статус, заголовки, тело)."""
    allowed = [o.strip() for o in env("ALLOWED_ORIGINS", env("SITE_URL", "")).split(",") if o.strip()]
    headers = {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}
    if origin and (origin in allowed or "*" in allowed):
        headers.update({"Access-Control-Allow-Origin": origin, "Vary": "Origin",
                        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                        "Access-Control-Allow-Headers": "Content-Type"})
    if method == "OPTIONS":
        return 204, headers, ""
    action = query.get("a", "")
    try:
        body = json.loads(body_text) if body_text else {}
        if not isinstance(body, dict):
            raise ValueError
    except ValueError:
        return 400, headers, json.dumps({"error": "Неверный запрос"}, ensure_ascii=False)
    try:
        if action == "buy" and method == "POST":
            out = route_buy(body)
        elif action == "webhook" and method == "POST":
            out = route_webhook(body, source_ip)
        elif action == "order" and method == "GET":
            out = route_order(query)
        elif action == "health":
            out = {"ok": True, "editions": {k: v[1] for k, v in editions().items()}}
        else:
            raise HttpError(404, "Нет такого адреса")
        return 200, headers, json.dumps(out, ensure_ascii=False)
    except HttpError as e:
        return e.status, headers, json.dumps({"error": e.message}, ensure_ascii=False)
    except Exception as e:
        print("ERROR", type(e).__name__, e, file=sys.stderr)
        return 500, headers, json.dumps({"error": "Ошибка сервера, попробуйте позже"}, ensure_ascii=False)


# ───────────────────────── Yandex Cloud Functions ─────────────────────────

def handler(event, context):
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8", errors="replace")
    hdrs = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    ip = ((event.get("requestContext") or {}).get("identity") or {}).get("sourceIp")
    status, headers, out = dispatch(event.get("httpMethod", "GET").upper(), event.get("queryStringParameters") or {},
                                    body, ip, hdrs.get("origin"))
    return {"statusCode": status, "headers": headers, "body": out, "isBase64Encoded": False}


# ───────────────────────── свой сервер / проверка ─────────────────────────

def serve(port: int, trust_proxy: bool = False):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def _go(self):
            u = urllib.parse.urlsplit(self.path)
            q = dict(urllib.parse.parse_qsl(u.query))
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n).decode("utf-8", errors="replace") if n else ""
            ip = self.client_address[0]
            if trust_proxy and self.headers.get("X-Forwarded-For"):
                ip = self.headers["X-Forwarded-For"].split(",")[0].strip()
            st, hd, out = dispatch(self.command, q, body, ip, self.headers.get("Origin"))
            self.send_response(st)
            for k, v in hd.items():
                self.send_header(k, v)
            data = out.encode()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_OPTIONS = _go

        def log_message(self, fmt, *a):
            print("[sales]", self.address_string(), fmt % a, file=sys.stderr)

    print(f"Mandarin sales: http://127.0.0.1:{port}/?a=health", file=sys.stderr)
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--trust-proxy", action="store_true", help="брать IP из X-Forwarded-For (за nginx)")
    a = ap.parse_args()
    serve(a.port, a.trust_proxy)
