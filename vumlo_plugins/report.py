"""Report display convention v1 (Contract 2): what a plugin writes so the web app can show it.

`Report.build(...)` returns the body of `PUT /plugin-api/{submissions|assignments|courses}/{id}/reports/{key}`:
`{"summary": str, "data": {..., "display": {...}}}`. The web panel renders plain text only; the core refuses a
`data` over 16 KiB, so a table is trimmed to fit and says so in a fact.
"""

from __future__ import annotations

import json
import math
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

LEVELS = ("ok", "info", "warn", "alert")
MAX_SUMMARY = 500
MAX_TITLE = 80
MAX_ROWS = 200
MAX_FACTS = 20
MAX_CELL = 200
MAX_DATA_BYTES = 16 * 1024
PLUGIN_PATH = "/api/v1/plugins/{plugin_id}"


def _text(value: Any, limit: int) -> str:
    text = "".join(" " if unicodedata.category(c) == "Cc" else c for c in str(value))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _cell(value: Any) -> str | int | float:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return ""
        return value
    if isinstance(value, Decimal):
        return float(value)
    return _text(value, MAX_CELL)


def json_size(value: Any) -> int:
    """Bytes of `value` as the core measures it: compact JSON, UTF-8 (`serde_json::to_string`)."""
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode())


@dataclass
class Report:
    summary: str
    data: dict[str, Any] = field(default_factory=dict)

    def body(self) -> dict[str, Any]:
        return {"summary": self.summary, "data": self.data}

    @classmethod
    def build(
        cls, summary: str, *, level: str | None = None, title: str | None = None,
        columns: list[str] | None = None, rows: list[list[Any]] | None = None,
        facts: list[tuple[str, Any]] | None = None, links: list[tuple[str, str]] | None = None,
        plugin_id: str | None = None, data: dict[str, Any] | None = None,
    ) -> Report:
        """`links` are `(title, path)`: the path on the plugin's own server (`/export/<course>/xlsx`), which this
        turns into the path through the core, `/api/v1/plugins/<plugin_id>/export/...`. `data` is anything else a
        program may want; `display` is reserved."""
        summary = _text(summary, MAX_SUMMARY).strip()
        if not summary:
            raise ValueError("a report needs a summary")
        extra = dict(data or {})
        if "display" in extra:
            raise ValueError("`display` is reserved for the display convention")
        display: dict[str, Any] = {}
        if level is not None:
            if level not in LEVELS:
                raise ValueError(f"level must be one of {', '.join(LEVELS)}")
            display["level"] = level
        if title is not None:
            if not title or len(title) > MAX_TITLE:
                raise ValueError(f"title must be 1 to {MAX_TITLE} characters")
            display["title"] = title
        facts_out = [{"label": _text(k, 60), "value": _text(_cell(v), MAX_CELL)} for k, v in (facts or [])]
        if links:
            if not plugin_id:
                raise ValueError("links need the plugin_id")
            if any(".." in p.split("/") or "\\" in p or "//" in p for _, p in links):
                raise ValueError("a link path stays inside the plugin: no `..`, `//` or `\\`")
            prefix = PLUGIN_PATH.format(plugin_id=plugin_id)
            display["links"] = [{"title": _text(t, 60), "path": prefix + (p if p.startswith("/") else "/" + p)}
                                for t, p in links]
        if columns is not None:
            width = len(columns)
            table_rows = []
            for row in rows or []:
                if len(row) != width:
                    raise ValueError("every row needs one cell for each column")
                table_rows.append([_cell(c) for c in row])
            display["table"] = {"columns": [_text(c, 60) for c in columns], "rows": table_rows}
        elif rows:
            raise ValueError("rows need columns")
        if len(facts_out) > MAX_FACTS:
            raise ValueError(f"at most {MAX_FACTS} facts")
        if facts_out:
            display["facts"] = facts_out
        if display:
            extra["display"] = display
        report = cls(summary, extra)
        report._fit()
        return report

    def _fit(self) -> None:
        """Trim the table (200 rows, 16 KiB) and say how many rows were left out."""
        display = self.data.get("display", {})
        table = display.get("table")
        if table is None:
            return
        rows = table["rows"]
        total = len(rows)
        del rows[MAX_ROWS:]
        # Binary search the most rows that fit: the size grows with the rows.
        facts = display.setdefault("facts", [])
        if json_size(self.data) > MAX_DATA_BYTES or total > len(rows):
            spare = {"label": "More rows", "value": "0"}
            if len(facts) < MAX_FACTS:
                facts.append(spare)
            low, high = 0, len(rows)
            while low < high:
                middle = (low + high + 1) // 2
                trial = rows[:middle]
                table["rows"] = trial
                spare["value"] = str(total - middle)
                if json_size(self.data) <= MAX_DATA_BYTES:
                    low = middle
                else:
                    high = middle - 1
            table["rows"] = rows[:low]
            spare["value"] = str(total - low)
            if total == low and spare in facts:
                facts.remove(spare)
        if not facts:
            display.pop("facts")
        if json_size(self.data) > MAX_DATA_BYTES:
            raise ValueError("the report does not fit in 16 KiB even without rows")
