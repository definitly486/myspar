
#!/usr/bin/env python3
# SPAR purchase history downloader
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


BASE_URL = "https://myspar.ru/api/"
APPV2_URL = "https://myspar.ru/api/appv2/"

AUTH_SIGNUP = "spar-auth/otp/signup"
AUTH_VERIFY = "spar-auth/otp/verify"

PURCHASE_LIST = "purchases/list/"
PURCHASE_DETAIL = "purchases/detail/"

DEFAULT_CONFIG = Path("~/.config/spar-api").expanduser()
DEFAULT_DB = Path("~/spar-purchases.sqlite3").expanduser()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def decode_jwt_exp(token: str) -> float:
    """Получить exp из JWT без проверки подписи."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return 0

        payload = parts[1]
        payload += "=" * (-len(payload) % 4)

        data = json.loads(
            base64.urlsafe_b64decode(payload.encode()).decode()
        )

        return float(data.get("exp", 0))

    except Exception:
        return 0


def token_expiry_epoch(token: str, token_life: str | None = None) -> float:
    """
    Сначала используем exp из JWT.
    Если его нет — пробуем token_life.
    """
    exp = decode_jwt_exp(token)

    if exp:
        return exp

    if token_life:
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
        ):
            try:
                dt = datetime.strptime(token_life, fmt)
                return dt.astimezone().timestamp()
            except ValueError:
                pass

    return 0


def read_json_response(resp: requests.Response) -> dict[str, Any]:
    try:
        obj = resp.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Ответ не JSON (HTTP {resp.status_code}): "
            f"{resp.text[:500]}"
        ) from exc

    if not isinstance(obj, dict):
        raise RuntimeError(
            f"Ожидался JSON-объект, получено {type(obj).__name__}"
        )

    if not resp.ok:
        raise RuntimeError(
            f"HTTP {resp.status_code}: "
            f"{json.dumps(obj, ensure_ascii=False)[:1200]}"
        )

    return obj


def unwrap(payload: dict[str, Any]) -> Any:
    if payload.get("result") is False or payload.get("error"):
        raise RuntimeError(
            "SPAR API: " +
            json.dumps(
                payload.get("error") or payload,
                ensure_ascii=False
            )
        )

    return payload.get("data", payload)


class SparClient:

    def __init__(
        self,
        token: str | None = None,
        timeout: int = 30
    ):
        self.http = requests.Session()

        self.http.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json; charset=UTF8",
            "Version": "2026-10-02_01",
            "User-Agent": (
                "Spar/Android/version-1.86Rustore/"
                "build-1256/sdk-35"
            ),
            "x-app-version": "2",
            "Bitrix": "new",
        })

        self.timeout = timeout
        self.token = token

        if token:
            self.http.headers["Authorization"] = f"Bearer {token}"

    def post(
        self,
        path: str,
        body: dict[str, Any],
        auth: bool = True,
        appv2: bool = False
    ) -> dict[str, Any]:

        headers = {}

        if not auth:
            headers["Authorization"] = ""

        base = APPV2_URL if appv2 else BASE_URL

        last_exc = None

        for attempt in range(5):
            try:
                resp = self.http.post(
                    base + path,
                    json=body,
                    headers=headers,
                    timeout=self.timeout
                )

                if resp.status_code == 401:
                    raise RuntimeError(
                        "HTTP 401: токен не принят или истёк. "
                        "Выполните: python3 spar_purchases.py login"
                    )

                if resp.status_code == 429:
                    raise RuntimeError(
                        "HTTP 429: SPAR ограничил количество запросов. "
                        "Повторите позже."
                    )

                if resp.status_code >= 500:
                    if attempt == 4:
                        raise RuntimeError(
                            f"HTTP {resp.status_code}: {resp.text[:500]}"
                        )

                    delay = min(2 ** attempt, 20)

                    print(
                        f"HTTP {resp.status_code}; "
                        f"повтор через {delay} сек.",
                        file=sys.stderr
                    )

                    time.sleep(delay)
                    continue

                return read_json_response(resp)

            except requests.RequestException as exc:
                last_exc = exc

                if attempt == 4:
                    break

                time.sleep(min(2 ** attempt, 20))

        raise RuntimeError(
            f"Сетевая ошибка для {path}: {last_exc}"
        )

    def request_sms(self, phone: str) -> dict[str, Any]:
        return unwrap(
            self.post(
                AUTH_SIGNUP,
                {"identifier": phone},
                auth=False,
                appv2=True
            )
        )

    def obtain_token(
        self,
        phone: str,
        otp_hash: str,
        code: str,
        device_id: str,
        push_token: str = ""
    ) -> dict[str, Any]:

        payload = unwrap(
            self.post(
                AUTH_VERIFY,
                {
                    "identifier": phone,
                    "hash": otp_hash,
                    "otp": code,
                    "device": {
                        "id": device_id,
                        "push_token": push_token,
                        "platform": "ANDROID",
                    },
                },
                auth=False,
                appv2=True
            )
        )

        info = (
            payload.get("tokenInfo")
            if isinstance(payload, dict)
            else None
        )

        if not isinstance(info, dict) or not info.get("token"):
            raise RuntimeError(
                "В ответе otp/verify нет tokenInfo.token: " +
                json.dumps(payload, ensure_ascii=False)[:1000]
            )

        return info

    def get_purchases(self, page: int) -> dict[str, Any]:
        return unwrap(
            self.post(
                PURCHASE_LIST,
                {"page": page}
            )
        )

    def get_purchase_detail(
        self,
        purchase_id: str
    ) -> dict[str, Any]:

        data = unwrap(
            self.post(
                PURCHASE_DETAIL,
                {"id": purchase_id}
            )
        )

        if isinstance(data, dict):
            if isinstance(data.get("item"), dict):
                return data["item"]

            if isinstance(data.get("purchaseInfo"), dict):
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
        device_file.write_text(
            str(uuid.uuid4()) + "\n",
            encoding="utf-8"
        )

        device_file.chmod(0o600)

    return config / "session.json", device_file


def save_session(
    path: Path,
    data: dict[str, Any]
) -> None:

    tmp = path.with_suffix(".tmp")

    tmp.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2
        ) + "\n",
        encoding="utf-8"
    )

    tmp.chmod(0o600)
    tmp.replace(path)
    path.chmod(0o600)


def load_session(path: Path) -> dict[str, Any]:

    if not path.is_file():
        raise RuntimeError(
            f"Нет сохранённой сессии: {path}\n"
            "Выполните: python3 spar_purchases.py login"
        )

    try:
        data = json.loads(
            path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise RuntimeError(
            f"Не удалось прочитать сессию {path}: {exc}"
        )

    if not isinstance(data, dict):
        raise RuntimeError("Некорректный формат session.json")

    token = data.get("access_token")

    if not token:
        raise RuntimeError(
            "В session.json отсутствует access_token"
        )

    expires_at = token_expiry_epoch(
        token,
        data.get("token_life")
    )

    if expires_at and time.time() >= expires_at:
        raise RuntimeError(
            "Срок действия access token истёк.\n"
            "Выполните повторную авторизацию:\n"
            "python3 spar_purchases.py login"
        )

    return data


def login(config: Path, timeout: int) -> None:

    session_file, device_file = ensure_config(config)

    phone = input(
        "Телефон SPAR (например 79255510833): "
    ).strip()

    if not phone:
        raise SystemExit("Телефон не указан")

    phone = "".join(
        ch for ch in phone
        if ch.isdigit()
    )

    api = SparClient(timeout=timeout)

    print(
        "Запрашиваю SMS-код через appv2/otp/signup…"
    )

    signup = api.request_sms(phone)

    otp_hash = (
        signup.get("hash")
        if isinstance(signup, dict)
        else None
    )

    if not otp_hash:
        raise RuntimeError(
            "otp/signup не вернул поле hash: " +
            json.dumps(signup, ensure_ascii=False)[:1000]
        )

    print("SMS отправлена.")

    code = getpass.getpass(
        "Код из SMS: "
    ).strip()

    if not code:
        raise SystemExit("SMS-код не указан")

    device_id = device_file.read_text(
        encoding="utf-8"
    ).strip()

    push_token = getpass.getpass(
        "FCM push_token (Enter — пропустить): "
    ).strip()

    token_info = api.obtain_token(
        phone,
        str(otp_hash),
        code,
        device_id,
        push_token
    )

    token = token_info["token"]
    token_life = token_info.get("token_life")

    expires_at = token_expiry_epoch(
        token,
        token_life
    )

    save_session(
        session_file,
        {
            "phone": phone,
            "access_token": token,
            "refresh_token": token_info.get(
                "refresh_token", ""
            ),
            "token_life": token_life,
            "expires_at": expires_at,
            "device_id": device_id,
            "push_token": push_token,
            "saved_at": utc_now(),
        }
    )

    print(
        f"Авторизация успешна. "
        f"Сессия сохранена в {session_file}"
    )

    if token_life:
        print(
            f"Срок действия access token: {token_life}"
        )


def open_db(path: Path) -> sqlite3.Connection:

    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    db = sqlite3.connect(path)

    db.execute("PRAGMA journal_mode=WAL")

    db.execute("""
        CREATE TABLE IF NOT EXISTS purchases (
            id TEXT PRIMARY KEY,
            date_create TEXT,
            status_id TEXT,
            price REAL,
            address TEXT,
            raw_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS purchase_products (
            purchase_id TEXT NOT NULL,
            article TEXT,
            name TEXT,
            price REAL,
            total REAL,
            measure TEXT,
            quantity REAL,
            raw_json TEXT NOT NULL,
            PRIMARY KEY (purchase_id, article, name),
            FOREIGN KEY (purchase_id) REFERENCES purchases(id)
        )
    """)

    db.commit()

    return db


def save_purchase(
    db: sqlite3.Connection,
    item: dict[str, Any]
) -> None:

    pid = item.get("id")

    if pid is None:
        return

    db.execute("""
        INSERT INTO purchases (
            id,
            date_create,
            status_id,
            price,
            address,
            raw_json,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            date_create=excluded.date_create,
            status_id=excluded.status_id,
            price=excluded.price,
            address=excluded.address,
            raw_json=excluded.raw_json,
            updated_at=excluded.updated_at
    """, (
        str(pid),
        item.get("date_create"),
        item.get("status_id"),
        item.get("price"),
        item.get("address"),
        json.dumps(item, ensure_ascii=False),
        utc_now()
    ))

    products = item.get("basket")

    if products is None:
        products = item.get("products")

    if not isinstance(products, list):
        return

    db.execute(
        "DELETE FROM purchase_products WHERE purchase_id=?",
        (str(pid),)
    )

    for product in products:

        if not isinstance(product, dict):
            continue

        db.execute("""
            INSERT OR REPLACE INTO purchase_products (
                purchase_id,
                article,
                name,
                price,
                total,
                measure,
                quantity,
                raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(pid),
            str(product.get("article") or ""),
            str(product.get("name") or ""),
            product.get("price"),
            product.get("sum"),
            product.get("measure"),
            product.get("quantity"),
            json.dumps(product, ensure_ascii=False)
        ))


def sync(
    config: Path,
    db_path: Path,
    timeout: int,
    max_pages: int,
    no_details: bool
) -> None:

    session_file, _ = ensure_config(config)

    # Используем уже сохранённую сессию.
    session = load_session(session_file)

    print(
        "Использую сохранённую сессию: "
        + str(session.get("phone", "номер неизвестен"))
    )

    api = SparClient(
        session["access_token"],
        timeout
    )

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
                raise RuntimeError(
                    "Неожиданный ответ purchases/list/: " +
                    json.dumps(data, ensure_ascii=False)[:1500]
                )

            # В актуальном ответе SPAR используется items.
            # purchases оставлен для совместимости.
            items = data.get("items")

            if items is None:
                items = data.get("purchases")

            if not isinstance(items, list):
                raise RuntimeError(
                    "В ответе purchases/list отсутствует "
                    "массив items/purchases: " +
                    json.dumps(data, ensure_ascii=False)[:1500]
                )

            paging = data.get("paging") or {}

            if isinstance(paging, dict):
                try:
                    if paging.get("pages") is not None:
                        total_pages = int(
                            paging["pages"]
                        )
                except (TypeError, ValueError):
                    pass

            for item in items:

                if not isinstance(item, dict):
                    continue

                save_purchase(db, item)

                if item.get("id") is not None:
                    ids.add(str(item["id"]))

            db.commit()

            pages += 1
            seen += len(items)

            suffix = (
                f" / {total_pages}"
                if total_pages is not None
                else ""
            )

            print(
                f"Страница {page}: "
                f"{len(items)} покупок{suffix}"
            )

            if not items:
                break

            if (
                total_pages is not None
                and page >= total_pages
            ):
                break

            if max_pages and pages >= max_pages:
                break

            page += 1

        details = 0

        if not no_details:

            for n, pid in enumerate(
                sorted(ids),
                1
            ):

                detail = api.get_purchase_detail(pid)

                if isinstance(detail, dict):
                    save_purchase(db, detail)
                    db.commit()
                    details += 1

                print(
                    f"Подробности: {n}/{len(ids)}"
                )

        print("\nСинхронизация завершена")

        print(f"Страниц: {pages}")
        print(f"Покупок в ответах списка: {seen}")
        print(f"Подробностей загружено: {details}")

        if seen == 0:
            print(
                "\nSPAR вернул пустую историю покупок "
                "для этого аккаунта."
            )

        print(
            "В SQLite покупок: "
            + str(
                db.execute(
                    "SELECT COUNT(*) FROM purchases"
                ).fetchone()[0]
            )
        )

        print(
            "Товарных строк: "
            + str(
                db.execute(
                    "SELECT COUNT(*) FROM purchase_products"
                ).fetchone()[0]
            )
        )

        print(f"База данных: {db_path}")

    finally:
        db.close()


def main() -> None:

    parser = argparse.ArgumentParser(
        description=(
            "SPAR: авторизация и загрузка "
            "истории покупок в SQLite"
        )
    )

    parser.add_argument(
        "command",
        choices=["login", "sync", "all"],
        nargs="?",
        default="sync",
        help=(
            "login — авторизация; "
            "sync — синхронизация сохранённой сессии; "
            "all — авторизация и синхронизация"
        )
    )

    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG
    )

    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=30
    )

    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="0 — загрузить все страницы"
    )

    parser.add_argument(
        "--no-details",
        action="store_true",
        help="не загружать подробности покупок"
    )

    args = parser.parse_args()

    config = args.config.expanduser()
    db_path = args.db.expanduser()

    if args.command in ("login", "all"):
        login(
            config,
            args.timeout
        )

    if args.command in ("sync", "all"):
        sync(
            config,
            db_path,
            args.timeout,
            args.max_pages,
            args.no_details
        )


if __name__ == "__main__":

    try:
        main()

    except KeyboardInterrupt:
        print(
            "\nОстановлено",
            file=sys.stderr
        )
        raise SystemExit(130)

    except Exception as exc:
        print(
            f"\nОШИБКА: {exc}",
            file=sys.stderr
        )
        raise SystemExit(1)
