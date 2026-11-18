#!/usr/bin/env python3
# Показ истории покупок SPAR из локальной SQLite.
# Не обращается к API и не изменяет базу.

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

DEFAULT_DB = Path("~/spar-purchases.sqlite3").expanduser()


def money(value: Any) -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def number(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        n = float(value)
        return str(int(n)) if n.is_integer() else f"{n:g}"
    except (TypeError, ValueError):
        return str(value)


def parse_json(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        obj = json.loads(value)
        return obj if isinstance(obj, dict) else {}
    except (TypeError, ValueError):
        return {}


def columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}


def value_from(raw: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = raw.get(key)
        if value not in (None, ""):
            return value
    return None


def payment_details(raw: dict[str, Any]) -> dict[str, Any]:
    """Извлекает сведения об оплате из разных вариантов структуры ответа SPAR."""
    method_keys = {"paymentmethod", "paymenttype", "methodname", "paymenttypename",
                   "paymentmethodname", "paytype", "paytypename"}
    card_keys = {"card", "cardinfo", "bankcard", "paymentcard", "carddetails"}
    masked_keys = {"maskedpan", "maskedcard", "maskednumber", "cardmask",
                   "cardnumbermasked", "panmasked", "maskedcardnumber"}
    last4_keys = {"last4", "lastfour", "cardlast4", "cardnumberlast4"}
    bank_keys = {"bankname", "issuername", "paymentprovider", "providername"}
    result: dict[str, Any] = {}
    seen: set[int] = set()

    def walk(value: Any):
        if isinstance(value, dict) and id(value) not in seen:
            seen.add(id(value))
            for key, item in value.items():
                normalized = "".join(ch for ch in str(key).lower() if ch.isalnum())
                if normalized in method_keys and isinstance(item, (str, int, float)):
                    result.setdefault("method", str(item))
                elif normalized in masked_keys and isinstance(item, (str, int, float)):
                    result.setdefault("card", str(item))
                elif normalized in last4_keys and isinstance(item, (str, int, float)):
                    digits = "".join(ch for ch in str(item) if ch.isdigit())
                    if digits:
                        result.setdefault("card_last4", digits[-4:])
                elif normalized in bank_keys and isinstance(item, (str, int, float)):
                    result.setdefault("bank", str(item))
                elif normalized in card_keys and isinstance(item, dict):
                    # Обходим объект карты, не выводя токены и полные реквизиты.
                    walk(item)
                elif isinstance(item, (dict, list)):
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(raw)
    return result


def load_history(db_path: Path):
    if not db_path.is_file():
        raise RuntimeError(f"База не найдена: {db_path}")
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "purchases" not in tables:
            raise RuntimeError("В базе отсутствует таблица purchases")
        purchase_cols = columns(db, "purchases")
        order = "date_create DESC" if "date_create" in purchase_cols else "rowid DESC"
        purchases = [dict(r) for r in db.execute(f"SELECT * FROM purchases ORDER BY {order}")]
        products = {}
        if "purchase_products" in tables:
            for row in db.execute(
                "SELECT * FROM purchase_products ORDER BY purchase_id, name"
            ):
                item = dict(row)
                products.setdefault(str(item.get("purchase_id") or ""), []).append(item)
        return purchases, products
    finally:
        db.close()


def show(purchases, products, limit: int):
    if limit > 0:
        purchases = purchases[:limit]
    print("=" * 68)
    print(f"ИСТОРИЯ ПОКУПОК SPAR — показано {len(purchases)}")
    print("=" * 68)
    if not purchases:
        print("Покупок в базе пока нет. Сначала выполните: python3 spar_purchases.py sync")
        return

    for index, purchase in enumerate(purchases, 1):
        raw = parse_json(purchase.get("raw_json"))
        pid = str(purchase.get("id") or "")
        date = value_from(purchase, "date_create") or value_from(raw, "date_create", "date", "created_at") or "Дата не указана"
        total = value_from(purchase, "price") or value_from(raw, "price", "total", "sum")
        address = value_from(purchase, "address") or value_from(raw, "address", "shop_address", "store_address")
        status = value_from(purchase, "status_id") or value_from(raw, "status", "status_name")
        print()
        print(f"{index}. Покупка от {date}")
        print(f"   Номер: {pid or '—'}")
        print(f"   Сумма: {money(total)}")
        payment = payment_details(raw)
        if payment:
            parts = []
            if payment.get("method"):
                parts.append(payment["method"])
            if payment.get("card"):
                parts.append(f"карта {payment['card']}")
            elif payment.get("card_last4"):
                parts.append(f"карта ****{payment['card_last4']}")
            if payment.get("bank"):
                parts.append(payment["bank"])
            if parts:
                print("   Оплата: " + ", ".join(parts))
        if status is not None:
            print(f"   Статус: {status}")
        if address:
            print(f"   Магазин: {address}")

        rows = products.get(pid, [])
        # На случай, если список товаров находится только во вложенном JSON.
        if not rows:
            embedded = value_from(raw, "basket", "products", "items")
            if isinstance(embedded, list):
                rows = [p for p in embedded if isinstance(p, dict)]

        if rows:
            print("   Товары:")
            for product in rows:
                name = value_from(product, "name", "title", "product_name") or "Товар без названия"
                qty = value_from(product, "quantity", "count", "amount")
                measure = value_from(product, "measure", "unit")
                unit_price = value_from(product, "price", "price_per_unit")
                line_total = value_from(product, "total", "sum", "total_price")
                qty_text = number(qty)
                if measure:
                    qty_text += f" {measure}"
                details = []
                if qty_text:
                    details.append(qty_text)
                if unit_price is not None:
                    details.append(f"цена {money(unit_price)}")
                if line_total is not None:
                    details.append(f"сумма {money(line_total)}")
                suffix = f" ({'; '.join(details)})" if details else ""
                print(f"     • {name}{suffix}")
        else:
            print("   Состав покупки не сохранён в базе (нет подробностей по товарам).")

    print()
    print("-" * 68)
    print(f"Всего покупок в базе: {sum(1 for _ in purchases) if limit <= 0 else 'см. без --limit'}")


def main():
    parser = argparse.ArgumentParser(description="Показ истории покупок SPAR из SQLite")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Путь к SQLite")
    parser.add_argument("--limit", type=int, default=0, help="Сколько последних покупок показать; 0 — все")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit не может быть отрицательным")
    purchases, products = load_history(args.db.expanduser())
    show(purchases, products, args.limit)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nОстановлено", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"\nОШИБКА: {exc}", file=sys.stderr)
        raise SystemExit(1)
