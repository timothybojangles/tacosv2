"""Shared CSV/XLSX input and output helpers.

The public ``open_csv`` name is retained for compatibility with the validators;
it presents either format as a text stream suitable for :mod:`csv` readers.
"""

from __future__ import annotations

from contextlib import contextmanager
import csv
import io
from pathlib import Path
import re
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile


class CSVEncodingError(ValueError):
    """A CSV cannot be decoded as UTF-8 without losing data."""

    def __init__(self, path: str, error: UnicodeDecodeError):
        raw = Path(path).read_bytes()
        line = raw.count(b"\n", 0, error.start) + 1
        line_start = raw.rfind(b"\n", 0, error.start) + 1
        column = error.start - line_start + 1
        bad = raw[error.start : error.end]
        preview = raw[line_start : raw.find(b"\n", error.start) if b"\n" in raw[error.start:] else len(raw)]
        preview_text = preview.decode("utf-8", errors="replace")[:160]
        self.path = path
        self.line = line
        self.column = column
        self.bad_bytes = bad
        super().__init__(
            f"CSV encoding problem in '{Path(path).name}', line {line}, column {column}: "
            f"byte(s) {bad.hex(' ').upper()} are not valid UTF-8.\n"
            f"Affected data: {preview_text!r}\n"
            "Save/export the file as UTF-8, or choose the automatic repair option to "
            "replace each undecodable byte sequence with '?'."
        )


def check_csv_encoding(path: str) -> None:
    """Raise :class:`CSVEncodingError` with the exact location of bad input."""
    if Path(path).suffix.lower() == ".xlsx":
        return
    try:
        Path(path).read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CSVEncodingError(path, exc) from exc


def repair_csv_encoding(path: str) -> str:
    """Write a UTF-8 copy, replacing undecodable sequences with question marks."""
    source = Path(path)
    if source.suffix.lower() == ".xlsx":
        raise ValueError("XLSX workbooks are binary and do not need UTF-8 repair")
    repaired = source.with_name(f"{source.stem}.utf8-fixed{source.suffix}")
    text = source.read_bytes().decode("utf-8-sig", errors="replace").replace("\ufffd", "?")
    repaired.write_text(text, encoding="utf-8", newline="")
    return str(repaired)


def _xlsx_rows(path: str) -> list[list[object]]:
    """Read the first worksheet of a standard XLSX workbook using the stdlib."""
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with ZipFile(path) as workbook:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in workbook.namelist():
            root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
            shared = ["".join(node.itertext()) for node in root.findall(f"{ns}si")]
        sheet_names = sorted(
            name for name in workbook.namelist()
            if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)
        )
        if not sheet_names:
            return []
        root = ET.fromstring(workbook.read(sheet_names[0]))
        result: list[list[object]] = []
        for row in root.findall(f".//{ns}sheetData/{ns}row"):
            values: list[object] = []
            for cell in row.findall(f"{ns}c"):
                ref = cell.get("r", "A1")
                letters = re.match(r"[A-Z]+", ref)
                column = 0
                for char in letters.group(0) if letters else "A":
                    column = column * 26 + ord(char) - 64
                while len(values) < column - 1:
                    values.append("")
                kind = cell.get("t")
                value_node = cell.find(f"{ns}v")
                if kind == "inlineStr":
                    inline = cell.find(f"{ns}is")
                    value: object = "" if inline is None else "".join(inline.itertext())
                elif value_node is None:
                    value = ""
                elif kind == "s":
                    value = shared[int(value_node.text or 0)]
                elif kind == "b":
                    value = "TRUE" if value_node.text == "1" else "FALSE"
                else:
                    value = value_node.text or ""
                values.append(value)
            result.append(values)
        return result


def _column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _write_xlsx(path: str, rows: list[list[str]]) -> None:
    """Write a portable, single-sheet XLSX workbook without optional packages."""
    def xml_text(value: object) -> str:
        text = str(value if value is not None else "")
        return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))

    sheet_rows = []
    for row_number, row in enumerate(rows, 1):
        cells = []
        for column, value in enumerate(row, 1):
            ref = f"{_column_name(column)}{row_number}"
            cells.append(
                f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">'
                f"{xml_text(value)}</t></is></c>"
            )
        sheet_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    content_types = '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>'
    relationships = '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
    workbook = '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>'
    workbook_rels = '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
    sheet = '<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + "".join(sheet_rows) + '</sheetData></worksheet>'
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relationships)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)


@contextmanager
def open_csv(path: str):
    """Open CSV or XLSX data as a CSV-compatible UTF-8 text stream."""
    if Path(path).suffix.lower() == ".xlsx":
        stream = io.StringIO(newline="")
        csv.writer(stream).writerows(_xlsx_rows(path))
        stream.seek(0)
        try:
            yield stream
        finally:
            stream.close()
        return
    check_csv_encoding(path)
    with open(path, "r", newline="", encoding="utf-8-sig") as handle:
        yield handle


@contextmanager
def open_table(path: str, mode: str = "w", newline: str = ""):
    """Open a CSV/XLSX destination as a stream accepted by :mod:`csv` writers."""
    if "w" not in mode:
        raise ValueError("open_table currently supports write mode only")
    if Path(path).suffix.lower() != ".xlsx":
        with open(path, mode, newline=newline, encoding="utf-8") as handle:
            yield handle
        return
    stream = io.StringIO(newline="")
    try:
        yield stream
        stream.seek(0)
        _write_xlsx(path, list(csv.reader(stream)))
    finally:
        stream.close()
