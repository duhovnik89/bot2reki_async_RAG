"""
Синхронизация данных: Excel -> rules.json.
Запуск: python -m bot.sync

RulesEngine (bot/search.py) сам режет записи на фрагменты и
держит индекс в памяти, ему нужен только плоский список записей.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import openpyxl

from config.settings import RULES_FILE_PATH, RULES_JSON_PATH

# На этом листе первая колонка — дата решения; без неё строка (шапка и т.п.)
# в архив не попадает.
_CHRONOLOGY_SHEET = "Все решения (хронология)"


def _row_to_record(sheet_name: str, order: int, values: tuple) -> dict | None:
    cells = [v for v in values if v is not None and str(v).strip()]
    if not cells:
        return None

    date = None
    if isinstance(cells[0], datetime):
        date = cells[0].strftime("%d.%m.%Y")
        cells = cells[1:]

    text = "\n".join(str(v).strip() for v in cells)
    if not text:
        return None
    if sheet_name == _CHRONOLOGY_SHEET and not date:
        return None

    return {"sheet": sheet_name, "order": order, "date": date, "text": text}


def sync(excel_path: str = RULES_FILE_PATH, json_path: str = RULES_JSON_PATH) -> int:
    xlsx = Path(excel_path)
    if not xlsx.exists():
        print(f"Файл не найден: {excel_path}", file=sys.stderr)
        return 0

    wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)

    records = []
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        for order, row in enumerate(ws.iter_rows(values_only=True)):
            record = _row_to_record(sheet_name, order, row)
            if record:
                records.append(record)

    wb.close()

    out = Path(json_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "records": records,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"Загружено записей: {len(records)} -> {json_path}")
    return len(records)


if __name__ == "__main__":
    excel = sys.argv[1] if len(sys.argv) > 1 else RULES_FILE_PATH
    out = sys.argv[2] if len(sys.argv) > 2 else RULES_JSON_PATH
    sync(excel, out)
