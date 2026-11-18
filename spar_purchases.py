#!/usr/bin/env python3
# SPAR purchase-history client based on the decompiled Android application.
# API: https://myspar.ru/api/

from __future__ import annotations
import argparse
import base64
import getpass
import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from spar_history import load_history, payment_details, show as show_history

BASE_URL = "https://myspar.ru/api/"
APPV2_URL = "https://myspar.ru/api/appv2/"
AUTH_SIGNUP = "spar-auth/otp/signup"
AUTH_VERIFY = "spar-auth/otp/verify"
AUTH_REFRESH = "spar-auth/token/refresh"
PURCHASE_LIST = "purchases/list/"
PURCHASE_DETAIL = "purchases/detail/"
DEFAULT_CONFIG = Path("~/.config/spar-api").expanduser()
DEFAULT_DB = Path("~/spar-purchases.sqlite3").expanduser()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json_response(resp: requests.Response) -> dict[str, Any]:
    try:
        obj = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"Ответ не JSON (HTTP {resp.status_code}): {resp.text[:500]}") from exc
    if not isinstance(obj, dict):
        raise RuntimeError(f"Ожидался JSON-объект, получено {type(obj).__name__}")
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status_code}: {json.dumps(obj, ensure_ascii=False)[:1200]}")
    return obj


def unwrap(payload: dict[str, Any]) -> Any:
    # BasicResponse<T> in the APK: result, data, error, warnings.
    if payload.get("result") is False or payload.get("error"):
        raise RuntimeError("SPAR API: " + json.dumps(payload.get("error") or payload, ensure_ascii=False))
    return payload.get("data", payload)


class SparClient:
    def __init__(self, token: str | None = None, timeout: int = 30, verbose: bool = False):
        self.http = requests.Session()
        self.http.headers.update({
            "Accept": "application/json",
            "Version": "2026-10-02_01",
            "User-Agent": "Spar/Android/version-1.86Rustore/build-1256/sdk-35",
            "x-app-version": "2",
            "Bitrix": "new",
            # Официальный клиент отправляет JSON именно с charset=UTF8.
            "Content-Type": "application/json; charset=UTF8",
        })
        self.timeout = timeout
        self.verbose = verbose
        self.token = token
        if token:
            self.http.headers["Authorization"] = f"Bearer {token}"

    @staticmethod
    def _safe_log(value: Any) -> Any:
        secret_keys = {"token", "access_token", "refresh_token", "push_token", "otp", "hash", "authorization"}
        if isinstance(value, dict):
            return {
                key: ("<скрыто>" if key.lower() in secret_keys else
                      ("***" + str(item)[-3:] if key.lower() == "identifier" and item else
                       SparClient._safe_log(item)))
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [SparClient._safe_log(item) for item in value]
        return value

    def post(self, path: str, body: dict[str, Any], auth: bool = True, appv2: bool = False) -> dict[str, Any]:
        headers = {} if auth else {"Authorization": ""}
        base = APPV2_URL if appv2 else BASE_URL
        url = base + path
        if self.verbose:
            print(f"\n[API] POST {url}", file=sys.stderr)
            print("[API] Запрос: " + json.dumps(self._safe_log(body), ensure_ascii=False, indent=2), file=sys.stderr)
        last_exc = None
        for attempt in range(5):
            try:
                started = time.monotonic()
                r = self.http.post(url, json=body, headers=headers, timeout=self.timeout)
                elapsed = time.monotonic() - started
                if self.verbose:
                    print(f"[API] HTTP {r.status_code} за {elapsed:.3f} с", file=sys.stderr)
                if self.verbose:
                    try:
                        logged_response = self._safe_log(r.json())
                        print("[API] Ответ: " + json.dumps(logged_response, ensure_ascii=False, indent=2), file=sys.stderr)
                    except ValueError:
                        print("[API] Ответ (не JSON): " + r.text[:4000], file=sys.stderr)
                if r.status_code == 401:
                    raise RuntimeError("HTTP 401: токен не принят или истёк. Выполните login заново.")
                if r.status_code == 429 or r.status_code >= 500:
                    if attempt == 4:
                        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:500]}")
                    delay = min(2 ** attempt, 20)
                    print(f"HTTP {r.status_code}; повтор через {delay} сек.", file=sys.stderr)
                    time.sleep(delay)
                    continue
                return read_json_response(r)
            except requests.RequestException as exc:
                last_exc = exc
                if attempt == 4:
                    break
                time.sleep(min(2 ** attempt, 20))
        raise RuntimeError(f"Сетевая ошибка для {path}: {last_exc}")

    def request_sms(self, phone: str) -> dict[str, Any]:
        # Decompiled appv2 AuthSignupRequest serializes phone as "identifier".
        return unwrap(self.post(AUTH_SIGNUP, {"identifier": phone}, auth=False, appv2=True))

    def obtain_token(self, phone: str, otp_hash: str, code: str, device_id: str,
                     push_token: str = "") -> dict[str, Any]:
        # AuthVerifyRequest: identifier, hash, otp, device{id,push_token,platform}.
        payload = unwrap(self.post(AUTH_VERIFY, {
            "identifier": phone,
            "hash": otp_hash,
            "otp": code,
            "device": {
                "id": device_id,
                "push_token": push_token,
                "platform": "ANDROID",
            },
        }, auth=False, appv2=True))
        info = payload.get("tokenInfo") if isinstance(payload, dict) else None
        if not isinstance(info, dict) or not info.get("token"):
            raise RuntimeError("В ответе otp/verify нет tokenInfo.token: " + json.dumps(payload, ensure_ascii=False)[:1000])
        return info

    def refresh_access_token(self, refresh_token: str) -> dict[str, Any]:
        # Android использует PRO_LOYALTY_API_ENDPOINT_PROD,
        # который в конфигурации приложения указывает на /api/appv2/.
        # Запрос refresh выполняется без Bearer Authorization.
        payload = unwrap(self.post(
            AUTH_REFRESH,
            {"refresh_token": refresh_token},
            auth=False,
            appv2=True,
        ))
        info = payload.get("tokenInfo") if isinstance(payload, dict) else None
        if not isinstance(info, dict) or not info.get("token"):
            raise RuntimeError("В ответе token/refresh нет tokenInfo.token: " + json.dumps(payload, ensure_ascii=False)[:1000])
        return info

    def get_purchases(self, page: int) -> dict[str, Any]:
        return unwrap(self.post(PURCHASE_LIST, {"page": page}))

    def get_purchase_detail(self, purchase_id: str) -> dict[str, Any]:
        data = unwrap(self.post(PURCHASE_DETAIL, {"id": purchase_id}))
        if isinstance(data, dict) and isinstance(data.get("item"), dict):
            return data["item"]
        if isinstance(data, dict) and isinstance(data.get("purchaseInfo"), dict):
            return data["purchaseInfo"]
        return data


def ensure_config(config: Path) -> tuple[Path, Path]:
    config.mkdir(parents=True, exist_ok=True)
    try:
        config.chmod(0o700)
    except OSError:
        pass
    device_file = config / "device_id"
    if not device_file.exists():
        device_file.write_text(str(uuid.uuid4()) + "\n", encoding="utf-8")
        device_file.chmod(0o600)
    return config / "session.json", device_file


def save_session(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    path.chmod(0o600)


def load_session(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"Нет сохранённой сессии: {path}. Сначала выполните login.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("access_token"):
        raise RuntimeError("В session.json отсутствует access_token")
    return data


def token_expiry_epoch(token_life: str | None, access_token: str | None = None) -> float:
    # Prefer exp from JWT: it is an absolute Unix timestamp and avoids timezone
    # ambiguity in token_life. Decode only for scheduling; server validates token.
    if access_token:
        try:
            parts = access_token.split(".")
            if len(parts) >= 2:
                payload = parts[1] + "=" * (-len(parts[1]) % 4)
                claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
                if isinstance(claims.get("exp"), (int, float)):
                    return float(claims["exp"])
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
            pass
    if not token_life:
        return 0
    try:
        # appv2 token_life is a server timestamp without an offset; treat it as UTC.
        # astimezone() on a naive datetime incorrectly interprets it as host-local time.
        dt = datetime.strptime(token_life, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return 0


def login(config: Path, timeout: int, verbose: bool = False) -> None:
    session_file, device_file = ensure_config(config)
    phone = input("Телефон SPAR (например 79255510833): ").strip()
    if not phone:
        raise SystemExit("Телефон не указан")
    phone = "".join(ch for ch in phone if ch.isdigit())
    api = SparClient(timeout=timeout, verbose=verbose)
    print("Запрашиваю SMS-код через appv2/otp/signup…")
    signup = api.request_sms(phone)
    otp_hash = signup.get("hash") if isinstance(signup, dict) else None
    if not otp_hash:
        raise RuntimeError("otp/signup не вернул поле hash: " + json.dumps(signup, ensure_ascii=False)[:1000])
    print("SMS отправлена.")
    code = getpass.getpass("Код из SMS: ").strip()
    if not code:
        raise SystemExit("SMS-код не указан")
    device_id = device_file.read_text(encoding="utf-8").strip()
    push_token = getpass.getpass("FCM push_token (Enter — пропустить): ").strip()
    token_info = api.obtain_token(phone, str(otp_hash), code, device_id, push_token)
    token_life = token_info.get("token_life")
    expires_at = token_expiry_epoch(token_life, token_info["token"])
    save_session(session_file, {
        "phone": phone,
        "access_token": token_info["token"],
        "refresh_token": token_info.get("refresh_token", ""),
        "token_life": token_life,
        "expires_at": expires_at,
        "device_id": device_id,
        "push_token": push_token,
        "saved_at": utc_now(),
    })
    print(f"Авторизация успешна. Сессия сохранена в {session_file}")
    if token_life:
        print(f"Срок действия access token: {token_life}")


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS purchases (
        id TEXT PRIMARY KEY, date_create TEXT, status_id TEXT, price REAL,
        address TEXT, raw_json TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS purchase_products (
        purchase_id TEXT NOT NULL, article TEXT, name TEXT, price REAL,
        total REAL, measure TEXT, quantity REAL, raw_json TEXT NOT NULL,
        PRIMARY KEY (purchase_id, article, name),
        FOREIGN KEY (purchase_id) REFERENCES purchases(id))""")
    db.commit()
    return db


def save_purchase(db: sqlite3.Connection, item: dict[str, Any]) -> None:
    pid = item.get("id")
    if pid is None:
        return
    db.execute("""INSERT INTO purchases(id,date_create,status_id,price,address,raw_json,updated_at)
        VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
        date_create=excluded.date_create,status_id=excluded.status_id,price=excluded.price,
        address=excluded.address,raw_json=excluded.raw_json,updated_at=excluded.updated_at""",
        (str(pid), item.get("date_create"), item.get("status_id"), item.get("price"),
         item.get("address"), json.dumps(item, ensure_ascii=False), utc_now()))
    products = item.get("basket")
    if products is None:
        products = item.get("products")
    if isinstance(products, list):
        db.execute("DELETE FROM purchase_products WHERE purchase_id=?", (str(pid),))
        for p in products:
            if not isinstance(p, dict):
                continue
            db.execute("""INSERT OR REPLACE INTO purchase_products
                (purchase_id,article,name,price,total,measure,quantity,raw_json)
                VALUES(?,?,?,?,?,?,?,?)""", (
                str(pid), str(p.get("article") or ""), str(p.get("name") or ""),
                p.get("price"), p.get("sum"), p.get("measure"), p.get("quantity"),
                json.dumps(p, ensure_ascii=False)))


def sync(config: Path, db_path: Path, timeout: int, max_pages: int, no_details: bool, verbose: bool = False, json_output: bool = False) -> None:
    session_file, _ = ensure_config(config)
    session = load_session(session_file)
    # Recalculate for sessions created by older script versions (which treated
    # the server's timezone-less token_life as host-local time).
    calculated_expiry = token_expiry_epoch(session.get("token_life"), session.get("access_token"))
    if calculated_expiry:
        session["expires_at"] = calculated_expiry
        save_session(session_file, session)
    # Как и Android-клиент, обновляем токен заранее — за 60 секунд до
    # token_life/JWT exp, чтобы запросы не успели уйти с истекающим токеном.
    expires_at = float(session.get("expires_at") or 0)
    refresh_before = 60
    if expires_at and time.time() >= expires_at - refresh_before:
        refresh = session.get("refresh_token")
        if not refresh:
            raise RuntimeError("Срок действия access token подходит к концу, refresh_token отсутствует. Выполните login.")
        print("Access token скоро истечёт; обновляю через spar-auth/token/refresh…", file=sys.stderr if json_output else sys.stdout)
        refresh_api = SparClient(timeout=timeout, verbose=verbose)
        info = refresh_api.refresh_access_token(refresh)

        # AuthTokenResponse -> BasicResponse.data -> tokenInfo.
        # Refresh token может ротироваться: сохраняем новый, если он пришёл.
        new_access_token = info.get("token")
        if not new_access_token:
            raise RuntimeError("SPAR не вернул новый access token при обновлении.")
        session["access_token"] = new_access_token
        session["refresh_token"] = info.get("refresh_token") or refresh
        session["token_life"] = info.get("token_life")
        session["expires_at"] = token_expiry_epoch(info.get("token_life"), new_access_token)
        session["saved_at"] = utc_now()
        save_session(session_file, session)
        if not session["expires_at"]:
            print("Предупреждение: не удалось определить срок действия нового токена; при следующем запуске проверка будет повторена.", file=sys.stderr)
        else:
            remaining = max(0, int(session["expires_at"] - time.time()))
            print(f"Токен обновлён; срок действия примерно {remaining // 60} мин.", file=sys.stderr if json_output else sys.stdout)
    api = SparClient(session["access_token"], timeout, verbose=verbose)
    db = open_db(db_path)
    ids: set[str] = set()
    pages = 0
    seen = 0
    try:
        page = 1
        total_pages = None
        while True:
            data = api.get_purchases(page)
            if not isinstance(data, dict):
                raise RuntimeError("Неожиданный ответ purchases/list/: " + json.dumps(data, ensure_ascii=False)[:1500])

            # Фактический ответ API приложения использует ключ `items`,
            # а не `purchases`: {items: [...], paging: {...}}.
            # Поддерживаем оба варианта на случай различий версий API.
            items = data.get("items")
            if items is None:
                items = data.get("purchases")
            if not isinstance(items, list):
                raise RuntimeError("В ответе purchases/list/ нет массива items: " + json.dumps(data, ensure_ascii=False)[:1500])
            paging = data.get("paging") or {}
            try:
                if paging.get("pages") is not None:
                    total_pages = int(paging["pages"])
            except (TypeError, ValueError):
                pass
            for item in items:
                if isinstance(item, dict):
                    save_purchase(db, item)
                    if item.get("id") is not None:
                        ids.add(str(item["id"]))
            db.commit()
            pages += 1
            seen += len(items)
            print(f"Страница {page}: {len(items)} покупок" + (f" / {total_pages}" if total_pages else ""), file=sys.stderr if json_output else sys.stdout)
            if not items or (total_pages is not None and page >= total_pages) or (max_pages and pages >= max_pages):
                break
            page += 1
        details = 0
        if not no_details:
            for n, pid in enumerate(sorted(ids), 1):
                detail = api.get_purchase_detail(pid)
                if isinstance(detail, dict):
                    save_purchase(db, detail)
                    db.commit()
                    details += 1
                print(f"Подробности: {n}/{len(ids)}", file=sys.stderr if json_output else sys.stdout)
        api_total = None
        if isinstance(paging, dict) and paging.get("items_all") is not None:
            try:
                api_total = int(paging["items_all"])
            except (TypeError, ValueError):
                pass
        total_note = f"; всего по API: {api_total}" if api_total is not None else ""
        purchases, products = load_history(db_path)
        if json_output:
            output = []
            for purchase in purchases:
                pid = str(purchase.get("id") or "")
                try:
                    purchase_raw = json.loads(purchase.get("raw_json") or "{}")
                except (TypeError, ValueError):
                    purchase_raw = {}
                purchase_data = {k: v for k, v in purchase.items() if k != "raw_json"}
                purchase_data["raw"] = purchase_raw
                purchase_data["payment"] = payment_details(purchase_raw)
                product_rows = []
                for product in products.get(pid, []):
                    product_data = {k: v for k, v in product.items() if k != "raw_json"}
                    try:
                        product_data["raw"] = json.loads(product.get("raw_json") or "{}")
                    except (TypeError, ValueError):
                        product_data["raw"] = {}
                    product_rows.append(product_data)
                if not product_rows:
                    embedded = purchase_raw.get("basket") or purchase_raw.get("products")
                    if isinstance(embedded, list):
                        product_rows = embedded
                purchase_data["products"] = product_rows
                output.append(purchase_data)
            result = {
                "database": str(db_path),
                "sync": {
                    "pages": pages,
                    "purchases_in_list_responses": seen,
                    "api_total": api_total,
                    "details": details,
                },
                "purchase_count": len(purchases),
                "purchases": output,
            }
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        else:
            print("\nСинхронизация завершена")
            print(f"Страниц: {pages}; покупок в ответах списка: {seen}{total_note}; подробностей: {details}")
            print(f"В SQLite покупок: {db.execute('SELECT COUNT(*) FROM purchases').fetchone()[0]}")
            print(f"Товарных строк: {db.execute('SELECT COUNT(*) FROM purchase_products').fetchone()[0]}")
            print(f"База данных: {db_path}")
            show_history(purchases, products, 0)
    finally:
        db.close()


def main() -> None:
    p = argparse.ArgumentParser(description="SPAR: авторизация по SMS и выгрузка истории покупок")
    p.add_argument("command", choices=["login", "sync", "all"], nargs="?", default="sync")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--max-pages", type=int, default=0, help="0 — все страницы")
    p.add_argument("--no-details", action="store_true")
    p.add_argument("--json", action="store_true", help="вывести результат синхронизации в JSON (без текстового вывода)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="подробный журнал API-запросов и ответов (секреты скрываются)")
    args = p.parse_args()
    if args.command in ("login", "all"):
        login(args.config.expanduser(), args.timeout, args.verbose)
    if args.command in ("sync", "all"):
        sync(args.config.expanduser(), args.db.expanduser(), args.timeout, args.max_pages, args.no_details, args.verbose, args.json)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nОстановлено", file=sys.stderr)
        raise SystemExit(130)
    except Exception as e:
        print(f"\nОШИБКА: {e}", file=sys.stderr)
        raise SystemExit(1)
