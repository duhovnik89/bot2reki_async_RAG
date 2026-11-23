"""
Синхронизация данных: Excel -> SQLite FTS5.
Запуск: python -m bot.sync
"""

import sqlite3
import sys
from pathlib import Path

import openpyxl

from config.settings import DB_FILE_PATH, RULES_FILE_PATH


def _row_to_text(sheet_name: str, values: tuple) -> str:
    cells = [str(v).strip() for v in values if v is not None and str(v).strip()]
    if not cells:
        return ""
    return f"[{sheet_name}]\n" + "\n".join(cells)


def sync(excel_path: str = RULES_FILE_PATH, db_path: str = DB_FILE_PATH) -> int:
    xlsx = Path(excel_path)
    if not xlsx.exists():
        print(f"Файл не найден: {excel_path}", file=sys.stderr)
        return 0

    wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)

    db = Path(db_path)
    db.parent.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.executescript("""
        DROP TABLE IF EXISTS rules_fts;
        CREATE VIRTUAL TABLE rules_fts USING fts5(
            content,
            tokenize='unicode61'
        );
        DROP TABLE IF EXISTS rules_raw;
        CREATE TABLE rules_raw (
            id INTEGER PRIMARY KEY,
            sheet TEXT NOT NULL,
            row_order INTEGER NOT NULL,
            content TEXT NOT NULL
        );
    """)

    fts_records = []
    raw_records = []

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        for row_order, row in enumerate(ws.iter_rows(values_only=True)):
            text = _row_to_text(sheet_name, row)
            if text:
                fts_records.append((text,))
                raw_records.append((sheet_name, row_order, text))

    cur.executemany("INSERT INTO rules_fts(content) VALUES (?)", fts_records)
    cur.executemany(
        "INSERT INTO rules_raw(sheet, row_order, content) VALUES (?, ?, ?)",
        raw_records,
    )
    con.commit()
    con.close()
    wb.close()

    count = len(fts_records)
    print(f"Загружено строк: {count} -> {db_path}")
    return count


if __name__ == "__main__":
    excel = sys.argv[1] if len(sys.argv) > 1 else RULES_FILE_PATH
    db = sys.argv[2] if len(sys.argv) > 2 else DB_FILE_PATH
    sync(excel, db)
