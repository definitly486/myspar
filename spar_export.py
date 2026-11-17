
#!/usr/bin/env python3
# Экспорт истории SPAR из SQLite в CSV и JSON.
# Не обращается к API и не изменяет базу.

import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path
from datetime import datetime


DEFAULT_DB = Path("~/spar-purchases.sqlite3").expanduser()


def get_columns(db, table):
    return [
        row[1]
        for row in db.execute(f'PRAGMA table_info("{table}")').fetchall()
    ]


def get_rows(db, table):
    if not get_columns(db, table):
        return []

    db.row_factory = sqlite3.Row
    return [
        dict(row)
        for row in db.execute(f'SELECT * FROM "{table}"').fetchall()
    ]


def parse_raw(row):
    raw = row.get("raw_json")
    if not raw:
        return {}

    try:
        result = json.loads(raw)
        return result if isinstance(result, dict) else {"data": result}
    except (ValueError, TypeError):
        return {"raw_json": raw}


def write_csv(path, rows):
    if not rows:
        print(f"Нет данных для {path.name}")
        return

    # Собираем все встречающиеся поля, даже если строки отличаются.
    fields = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=fields,
            delimiter=";",
            extrasaction="ignore"
        )
        writer.writeheader()

        for row in rows:
            writer.writerow({
                key: (
                    json.dumps(value, ensure_ascii=False)
                    if isinstance(value, (dict, list))
                    else value
                )
                for key, value in row.items()
            })

    print(f"Создан: {path}")


def export_json(path, purchases, products):
    products_by_purchase = {}

    for product in products:
        pid = str(product.get("purchase_id") or "")
        products_by_purchase.setdefault(pid, []).append({
            **product,
            "data": parse_raw(product)
        })

    result = []

    for purchase in purchases:
        pid = str(purchase.get("id") or "")

        result.append({
            "id": pid,
            "purchase": purchase,
            "data": parse_raw(purchase),
            "products": products_by_purchase.get(pid, [])
        })

    with path.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"Создан: {path}")


def show_stats(purchases, products):
    print("\n========== SPAR: СТАТИСТИКА ==========")
    print(f"Покупок: {len(purchases)}")
    print(f"Товарных строк: {len(products)}")

    if purchases:
        dates = [
            str(p.get("date_create"))
            for p in purchases
            if p.get("date_create")
        ]

        if dates:
            print(f"Первая дата: {min(dates)}")
            print(f"Последняя дата: {max(dates)}")

        total = 0.0
        for p in purchases:
            try:
                total += float(p.get("price") or 0)
            except (ValueError, TypeError):
                pass

        print(f"Сумма покупок по полю price: {total:.2f}")

    print("======================================\n")


def main():
    parser = argparse.ArgumentParser(
        description="Экспорт истории покупок SPAR из SQLite"
    )

    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help="Путь к SQLite"
    )

    parser.add_argument(
        "--out",
        type=Path,
        default=Path("~/spar-export").expanduser(),
        help="Каталог экспорта"
    )

    parser.add_argument(
        "--format",
        choices=["all", "csv", "json"],
        default="all",
        help="Формат экспорта"
    )

    parser.add_argument(
        "--stats",
        action="store_true",
        help="Только показать статистику"
    )

    args = parser.parse_args()

    db_path = args.db.expanduser()

    if not db_path.is_file():
        raise SystemExit(f"База не найдена: {db_path}")

    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    try:
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

        if "purchases" not in tables:
            raise RuntimeError("В базе отсутствует таблица purchases")

        purchases = get_rows(db, "purchases")
        products = (
            get_rows(db, "purchase_products")
            if "purchase_products" in tables
            else []
        )

    finally:
        db.close()

    show_stats(purchases, products)

    if args.stats:
        return

    out = args.out.expanduser()
    out.mkdir(parents=True, exist_ok=True)

    if args.format in ("all", "csv"):
        write_csv(out / "purchases.csv", purchases)
        write_csv(out / "products.csv", products)

    if args.format in ("all", "json"):
        export_json(out / "purchases.json", purchases, products)

    print("\nЭкспорт завершён.")
    print(f"Каталог: {out}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nОстановлено", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"\nОШИБКА: {exc}", file=sys.stderr)
        sys.exit(1)
