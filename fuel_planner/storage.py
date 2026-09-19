from __future__ import annotations

import io
import json
import os
import tempfile
import zipfile
from pathlib import Path

import pandas as pd

from .engine import Result
from .models import Plan

TABLES = ("yearly_balance", "source_schedule", "inventory_trace", "financial_breakdown", "constraint_checks", "risk_register", "contract_ledger", "constraint_ledger", "independent_controls", "roadmap", "stakeholders")


def load_plan(path: Path | str) -> Plan:
    return Plan.model_validate_json(Path(path).read_text(encoding="utf-8"))


def save_plan(plan: Plan, directory: Path | str) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{plan.plan_id}.json"
    fd, temporary = tempfile.mkstemp(prefix=".plan-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(plan.model_dump_json(indent=2))
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def export_bytes(result: Result, format: str = "json") -> bytes:
    payload = result.payload
    if format == "json":
        return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False).encode()
    metadata = {k: v for k, v in payload.items() if k not in TABLES}
    buffer = io.BytesIO()
    if format == "csv":
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))
            for name in TABLES:
                archive.writestr(f"{name}.csv", pd.DataFrame(payload[name]).to_csv(index=False).encode("utf-8-sig"))
    elif format == "xlsx":
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            meta_rows = []
            for key, value in metadata.items():
                text = json.dumps(value, ensure_ascii=False)
                # Avoid silently truncating a large saved plan or dataset in Excel.
                for part in range(0, len(text), 30000):
                    meta_rows.append(dict(field=key, part=part//30000, value=text[part:part+30000]))
            pd.DataFrame(meta_rows).to_excel(writer, sheet_name="metadata", index=False)
            for name in TABLES:
                # Excel limits text cells to 32767 characters; nested records stay in JSON.
                rows = [{k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for k, v in row.items()} for row in payload[name]]
                pd.DataFrame(rows).to_excel(writer, sheet_name=name, index=False)
    else:
        raise ValueError(f"Неизвестный формат {format}")
    return buffer.getvalue()
