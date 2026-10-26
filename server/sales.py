"""
Mandarin — сервер продаж: Робокасса → ключ Pro покупателю. Без базы данных.

Как это работает
  1. Сайт: покупатель вводит почту → POST ?a=buy → сервер формирует ПОДПИСАННУЮ ссылку на оплату
     Робокассы и возвращает её. Цена задаётся ТОЛЬКО на сервере. Данные заказа (почта, редакция)
     едут в параметре Shp_d в зашифрованном виде — в открытом URL почта не светится, а подпись
     Робокассы не даёт их подменить.
  2. Робокасса после оплаты зовёт ResultURL → POST/GET ?a=webhook. Сервер не верит уведомлению:
     сверяет подпись (Password2), затем ЗАНОВО спрашивает статус платежа у Робокассы (OpStateExt),
     проверяет сумму, выпускает ключ и отправляет письмо. Ключ вычисляется из номера счёта (InvId),
     поэтому повторные уведомления дают тот же ключ (без дублей и без базы данных). В ответ отдаём
     ровно «OK<InvId>» — иначе Робокасса будет повторять уведомление.
  3. Покупатель возвращается на сайт (success.html) с параметрами Робокассы → GET ?a=order&… →
     сервер проверяет подпись (Password1) и статус оплаты и сразу показывает ключ на экране.

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
import xml.etree.ElementTree as ET
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import license as L  # noqa: E402  (для Yandex Cloud Functions положите license.py рядом с sales.py)

PAY_URL = os.environ.get("ROBOKASSA_URL", "https://auth.robokassa.ru/Merchant/Index.aspx")
OPSTATE_URL = os.environ.get(
    "ROBOKASSA_OPSTATE", "https://auth.robokassa.ru/Merchant/WebService/Service.asmx/OpStateExt")

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
HASHERS = {"md5": hashlib.md5, "sha1": hashlib.sha1, "sha256": hashlib.sha256,
           "sha384": hashlib.sha384, "sha512": hashlib.sha512}


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


# ───────────────────────── подпись и пароли Робокассы ─────────────────────────

def _hash(s: str) -> str:
    algo = env("ROBOKASSA_HASH", "sha256").lower()
    func = HASHERS.get(algo)
    if not func:
        raise RuntimeError(f"ROBOKASSA_HASH={algo}: поддерживаются {', '.join(HASHERS)}")
    return func(s.encode("utf-8")).hexdigest()


def _pw(n: int) -> str:
    """Пароль 1 (ссылка/SuccessURL) или 2 (ResultURL/OpState). В тестовом режиме — тестовые пароли."""
    test = env("ROBOKASSA_TEST", "0") == "1"
    return env(f"ROBOKASSA_{'TEST_' if test else ''}PASSWORD{n}", required=True)


def _shp_tail(shp: dict) -> str:
    """Пользовательские параметры Shp_* — по алфавиту, в хвосте подписи как :Shp_key=value."""
    return "".join(f":{k}={shp[k]}" for k in sorted(shp))


# ───────────────────────── упаковка данных заказа в Shp_d ─────────────────────────
# Почту и редакцию возим в Shp_d зашифрованными (AES-GCM на ключе из Password2): в URL видно
# только шифртекст, а подпись Робокассы защищает от подмены. Полностью без базы данных.

def _order_key() -> bytes:
    return hashlib.sha256(("mandarin.order." + _pw(2)).encode()).digest()


def _seal(data: dict) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    nonce = os.urandom(12)
    ct = AESGCM(_order_key()).encrypt(nonce, raw, None)
    return base64.urlsafe_b64encode(nonce + ct).decode().rstrip("=")


def _open(token: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not token:
        raise HttpError(400, "В заказе нет данных покупателя")
    try:
        blob = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        raw = AESGCM(_order_key()).decrypt(blob[:12], blob[12:], None)
        data = json.loads(raw.decode())
        if not isinstance(data, dict) or "ed" not in data:
            raise ValueError
        return data
    except Exception:
        raise HttpError(400, "Данные заказа повреждены")


# ───────────────────────── чек (необязательно) ─────────────────────────

def _receipt_enc(title: str, price) -> str:
    """URL-кодированный Receipt для фискализации «Робочеки» (ИП/ООО). Для самозанятого обычно не нужен —
    Робокасса сама регистрирует доход в «Мой налог» по подключённому счёту самозанятого."""
    receipt = {"items": [{
        "name": f"{title}: неисключительная лицензия"[:128],
        "quantity": 1,
        "sum": float(price),
        "payment_method": "full_payment",
        "payment_object": env("PAYMENT_OBJECT", "service"),
        "tax": env("RECEIPT_TAX", "none"),
    }]}
    sno = env("RECEIPT_SNO")
    if sno:
        receipt["sno"] = sno
    return urllib.parse.quote(json.dumps(receipt, ensure_ascii=True, separators=(",", ":")), safe="")


# ───────────────────────── создание платежа ─────────────────────────

def create_payment(edition_key: str) -> dict:
    eds = editions()
    if edition_key not in eds:
        raise HttpError(400, "Неизвестная редакция")
    code, price, title = eds[edition_key]
    inv = secrets.randbelow(2_000_000_000) + 1          # уникальный номер счёта (InvId), int32
    sealed = _seal({"ed": edition_key})
    shp = {"Shp_d": sealed}
    out = f"{float(price):.2f}"
    login = env("ROBOKASSA_LOGIN", required=True)

    parts = [login, out, str(inv)]
    receipt_enc = _receipt_enc(title, price) if env("RECEIPT", "none") == "robokassa" else None
    if receipt_enc:
        parts.append(receipt_enc)                        # Receipt входит в подпись в URL-кодированном виде
    parts.append(_pw(1))
    signature = _hash(":".join(parts) + _shp_tail(shp))

    params = {
        "MerchantLogin": login, "OutSum": out, "InvId": str(inv),
        "Description": f"{title} — лицензия"[:100],
        "SignatureValue": signature, "Culture": "ru", "Encoding": "utf-8",
        **shp,
    }
    if env("ROBOKASSA_TEST", "0") == "1":
        params["IsTest"] = "1"
    query = urllib.parse.urlencode(params)
    if receipt_enc:
        query += "&Receipt=" + receipt_enc               # уже URL-кодирован — повторно не кодируем
    return {"confirmation_url": f"{PAY_URL}?{query}", "payment_id": str(inv), "token": sealed}


# ───────────────────────── проверка статуса у Робокассы ─────────────────────────

def op_state(inv: str) -> tuple[int, float]:
    """Спрашиваем Робокассу о судьбе счёта. Возвращает (код состояния, зачисленная сумма).
    Код 100 — оплачено, 10 — отменено. Подпись запроса: hash(MerchantLogin:InvId:Password2)."""
    login = env("ROBOKASSA_LOGIN", required=True)
    sig = _hash(f"{login}:{inv}:{_pw(2)}")
    q = urllib.parse.urlencode({"MerchantLogin": login, "InvoiceID": str(inv), "Signature": sig})
    try:
        with urllib.request.urlopen(f"{OPSTATE_URL}?{q}", timeout=25) as r:
            xml = r.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        raise HttpError(502, f"Робокасса недоступна: {e}")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise HttpError(502, "Робокасса вернула неразборчивый ответ")
    rc = root.find(".//{*}Result/{*}Code")
    if rc is None or (rc.text or "").strip() != "0":
        raise HttpError(502, "Робокасса: не удалось проверить платёж")
    sc = root.find(".//{*}State/{*}Code")
    osum = root.find(".//{*}Info/{*}OutSum")
    state = int(sc.text) if sc is not None and (sc.text or "").strip().isdigit() else 0
    paid = float(osum.text) if osum is not None and osum.text else 0.0
    return state, paid


def confirm(inv: str, edition_key: str) -> tuple[str, int]:
    """Сверяет, что счёт действительно оплачен на нужную сумму. Возвращает (код редакции, код состояния)."""
    eds = editions()
    ed = eds.get(edition_key)
    if not ed:
        raise HttpError(400, "Неизвестная редакция")
    state, paid = op_state(inv)
    if state == 10:
        raise HttpError(409, "Платёж отменён")
    if state != 100:
        raise HttpError(409, "Платёж ещё не завершён")
    if paid + 0.001 < float(ed[1]):
        raise HttpError(400, "Сумма платежа меньше цены")
    return ed[0], state


def _verify_cb(params: dict, which: int) -> tuple[bool, str, dict]:
    """Проверка подписи callback'а: which=1 — SuccessURL (Password1), 2 — ResultURL (Password2)."""
    out = params.get("OutSum", params.get("OutSumm", ""))
    inv = params.get("InvId", "")
    got = (params.get("SignatureValue", "") or "").lower()
    shp = {k: v for k, v in params.items() if k.lower().startswith("shp_")}
    base = f"{out}:{inv}:{_pw(which)}" + _shp_tail(shp)
    ok = bool(got) and bool(inv) and hmac.compare_digest(got, _hash(base))
    return ok, inv, shp


# ───────────────────────── ключ ─────────────────────────
# Почту не собираем: ключ покупатель видит на странице после оплаты и восстанавливает по номеру
# заказа. Поэтому оператором персональных данных продавец не становится.

def key_for(inv: str, edition: int) -> str:
    priv = base64.b64decode(env("MANDARIN_PRIVATE_KEY", required=True))
    lid = hashlib.sha256(("robokassa:" + str(inv)).encode()).digest()[:12]
    return L.make_key(priv, lid, "", edition)           # без привязки к почте


def _edition_by_sum(paid: float) -> int:
    """Код редакции по оплаченной сумме (на случай второй редакции); по умолчанию — Pro."""
    for key, (code, price, title) in editions().items():
        if abs(float(price) - paid) < 0.5:
            return code
    return 1


# ───────────────────────── маршруты ─────────────────────────

def route_buy(body: dict) -> dict:
    if not body.get("agree"):
        raise HttpError(400, "Нужно согласие с условиями оферты")
    return create_payment(str(body.get("edition", "pro")))


def route_webhook(params: dict) -> str:
    """ResultURL Робокассы. Сверяем подпись и факт оплаты, отвечаем «OK<InvId>». Ключ покупатель
    получает на сайте, письмо не отправляется (почту не собираем)."""
    ok, inv, shp = _verify_cb(params, 2)
    if not ok:
        raise HttpError(403, "Подпись уведомления не совпадает")
    data = _open(shp.get("Shp_d", ""))
    confirm(inv, data["ed"])                            # проверяем, что оплачено на нужную сумму
    return f"OK{inv}"


def route_order(q: dict) -> dict:
    ok, inv, shp = _verify_cb(q, 1)
    if not ok:
        return {"status": "pending"}                    # зашли на страницу без корректных параметров оплаты
    data = _open(shp.get("Shp_d", ""))
    state, paid = op_state(inv)
    if state == 10:
        return {"status": "canceled"}
    if state != 100:
        return {"status": "pending"}
    ed = editions().get(data["ed"])
    if not ed or paid + 0.001 < float(ed[1]):
        return {"status": "pending"}
    return {"status": "succeeded", "key": key_for(inv, ed[0]),
            "edition": L.EDITIONS.get(ed[0], "Pro"), "order": str(inv)}


def route_recover(q: dict) -> dict:
    """Восстановление ключа по номеру заказа (InvId). Персональных данных не требует: спрашиваем у
    Робокассы, что счёт с этим номером оплачен, и отдаём тот же ключ."""
    inv = (q.get("id") or "").strip()
    if not inv.isdigit():
        raise HttpError(400, "Укажите номер заказа цифрами")
    state, paid = op_state(inv)
    if state != 100:
        raise HttpError(404, "Оплаченный заказ с таким номером не найден")
    code = _edition_by_sum(paid)
    return {"status": "succeeded", "key": key_for(inv, code),
            "edition": L.EDITIONS.get(code, "Pro"), "order": str(inv)}


def dispatch(method: str, query: dict, body_text: str, content_type: str, origin: str | None):
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

    # ResultURL: Робокасса шлёт form-urlencoded или query; отвечаем простым текстом «OK<InvId>».
    if action == "webhook":
        params = dict(query)
        if body_text and "form-urlencoded" in (content_type or "").lower():
            params.update(dict(urllib.parse.parse_qsl(body_text)))
        elif body_text:
            params.update(dict(urllib.parse.parse_qsl(body_text)))
        txt = {"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store"}
        try:
            return 200, txt, route_webhook(params)
        except HttpError as e:
            return e.status, txt, f"error: {e.message}"
        except Exception as e:
            print("ERROR webhook", type(e).__name__, e, file=sys.stderr)
            return 500, txt, "error: server"

    try:
        if action == "buy" and method == "POST":
            body = json.loads(body_text) if body_text else {}
            if not isinstance(body, dict):
                raise HttpError(400, "Неверный запрос")
            out = route_buy(body)
        elif action == "order" and method == "GET":
            out = route_order(query)
        elif action == "recover" and method == "GET":
            out = route_recover(query)
        elif action == "health":
            out = {"ok": True, "editions": {k: v[1] for k, v in editions().items()}}
        else:
            raise HttpError(404, "Нет такого адреса")
        return 200, headers, json.dumps(out, ensure_ascii=False)
    except HttpError as e:
        return e.status, headers, json.dumps({"error": e.message}, ensure_ascii=False)
    except json.JSONDecodeError:
        return 400, headers, json.dumps({"error": "Неверный запрос"}, ensure_ascii=False)
    except Exception as e:
        print("ERROR", type(e).__name__, e, file=sys.stderr)
        return 500, headers, json.dumps({"error": "Ошибка сервера, попробуйте позже"}, ensure_ascii=False)


# ───────────────────────── Yandex Cloud Functions ─────────────────────────

def handler(event, context):
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8", errors="replace")
    hdrs = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    status, headers, out = dispatch(event.get("httpMethod", "GET").upper(),
                                    event.get("queryStringParameters") or {},
                                    body, hdrs.get("content-type", ""), hdrs.get("origin"))
    return {"statusCode": status, "headers": headers, "body": out, "isBase64Encoded": False}


# ───────────────────────── свой сервер / проверка ─────────────────────────

def serve(port: int):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def _go(self):
            u = urllib.parse.urlsplit(self.path)
            q = dict(urllib.parse.parse_qsl(u.query))
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n).decode("utf-8", errors="replace") if n else ""
            st, hd, out = dispatch(self.command, q, body, self.headers.get("Content-Type", ""),
                                   self.headers.get("Origin"))
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
    a = ap.parse_args()
    serve(a.port)
