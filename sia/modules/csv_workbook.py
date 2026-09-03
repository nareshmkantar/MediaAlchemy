"""Convert uploaded CSV files into single-sheet workbooks.

Guided setup reads sources through openpyxl (see ``VisualNormalizer``), so a raw
``.csv`` has no grid to scan: the source registry gets a sheet-less ``:default``
entry and Layout Demarcation reports no regions. Converting at upload time keeps
one ingest path for both formats.
"""

from __future__ import annotations

import csv
import io
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from openpyxl import Workbook

logger = logging.getLogger(__name__)

EXCEL_MAX_ROWS = 1_048_576
EXCEL_MAX_COLS = 16_384
SHEET_NAME_MAX_LEN = 31

# Excel rejects these in sheet titles.
_INVALID_SHEET_CHARS = str.maketrans({char: " " for char in "[]:*?/\\"})
_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")
_SNIFF_DELIMITERS = ",;\t|"
_SNIFF_SAMPLE_CHARS = 8192

# Beyond this, digit runs are identifiers (card/product codes) rather than
# quantities, and float64 would lose precision.
_MAX_NUMERIC_DIGITS = 15


class CsvConversionError(ValueError):
    """An uploaded CSV could not be turned into a workbook."""


def excel_safe_sheet_name(raw: Any, fallback: str = "Sheet1") -> str:
    """Sheet title Excel will accept: no reserved characters, 31 chars max."""
    name = str(raw or "").translate(_INVALID_SHEET_CHARS)
    name = " ".join(name.split()).strip("'")
    if not name:
        return fallback
    return name[:SHEET_NAME_MAX_LEN]


def _decode(path: Path) -> Tuple[str, str]:
    raw = path.read_bytes()
    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8/replace"


def _sniff_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=_SNIFF_DELIMITERS).delimiter
    except csv.Error:
        pass
    counts = {d: sample.count(d) for d in _SNIFF_DELIMITERS}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else ","


def read_csv_grid(csv_path: Union[str, Path]) -> Tuple[List[List[str]], str, str]:
    """Read a CSV as a raw grid: no header promotion, no NA coercion.

    Ragged and blank rows are preserved because the layout demarcator relies on
    title and separator rows that are narrower than the data block. ``csv.reader``
    is used rather than pandas, which rejects rows wider than the first one.

    Returns ``(rows, delimiter, encoding)``.
    """
    path = Path(csv_path)
    text, encoding = _decode(path)
    if not text.strip():
        raise CsvConversionError("the file is empty")

    delimiter = _sniff_delimiter(text[:_SNIFF_SAMPLE_CHARS])
    try:
        rows = [list(row) for row in csv.reader(io.StringIO(text), delimiter=delimiter)]
    except csv.Error as exc:
        raise CsvConversionError(f"malformed CSV ({exc})") from exc

    while rows and not any(str(value).strip() for value in rows[-1]):
        rows.pop()
    if not rows:
        raise CsvConversionError("no readable rows")
    return rows, delimiter, encoding


def _coerce_cell(value: Any) -> Any:
    """Store numbers as numbers so column typing sees what Excel would show."""
    text = str(value)
    stripped = text.strip()
    if not stripped:
        return None
    if "_" in stripped:  # int("1_000") == 1000 — not what the file says
        return text

    digits = stripped.lstrip("+-")
    if len(digits) > _MAX_NUMERIC_DIGITS:
        return text
    # Leading zeros carry meaning (zip codes, product codes).
    if digits[:1] == "0" and digits[1:2].isdigit():
        return text

    try:
        return int(stripped)
    except ValueError:
        pass
    try:
        number = float(stripped)
    except ValueError:
        return text
    if number != number or number in (float("inf"), float("-inf")):
        return text
    return number


def write_grid_to_xlsx(
    rows: Sequence[Sequence[Any]],
    dest_path: Union[str, Path],
    sheet_name: str,
) -> Path:
    """Write a raw grid to a single-sheet workbook, cells in file order."""
    if len(rows) > EXCEL_MAX_ROWS:
        raise CsvConversionError(
            f"{len(rows):,} rows exceeds the Excel limit of {EXCEL_MAX_ROWS:,}"
        )
    width = max((len(row) for row in rows), default=0)
    if width > EXCEL_MAX_COLS:
        raise CsvConversionError(
            f"{width:,} columns exceeds the Excel limit of {EXCEL_MAX_COLS:,}"
        )

    dest = Path(dest_path)
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet(title=sheet_name)
    for row in rows:
        sheet.append([_coerce_cell(value) for value in row])
    workbook.save(str(dest))
    return dest


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for suffix in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({suffix}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise CsvConversionError("could not allocate a destination filename")


def convert_csv_to_xlsx(
    csv_path: Union[str, Path],
    *,
    dest_path: Optional[Union[str, Path]] = None,
    sheet_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Convert a CSV to a one-sheet workbook next to it.

    The sheet is named after the CSV so it shows up as a real tab in guided setup.
    Returns conversion details including the written ``path`` and ``sheet_name``.
    """
    source = Path(csv_path)
    rows, delimiter, encoding = read_csv_grid(source)
    title = excel_safe_sheet_name(sheet_name or source.stem)
    dest = Path(dest_path) if dest_path else _unique_path(source.with_suffix(".xlsx"))
    write_grid_to_xlsx(rows, dest, title)

    details = {
        "path": dest,
        "sheet_name": title,
        "rows": len(rows),
        "columns": max((len(row) for row in rows), default=0),
        "delimiter": delimiter,
        "encoding": encoding,
    }
    logger.info(
        "Converted CSV %s -> %s (sheet=%r, %s rows x %s cols, delimiter=%r, encoding=%s)",
        source.name,
        dest.name,
        title,
        details["rows"],
        details["columns"],
        delimiter,
        encoding,
    )
    return details
