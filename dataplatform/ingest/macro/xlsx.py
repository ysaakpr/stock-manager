"""A minimal, standard-library reader for the cell values of an `.xlsx` workbook.

Two government publishers in the macro register (the Office of the Economic Adviser's WPI files and
GSTN's collection statistics) publish only as `.xlsx`. The platform carries no spreadsheet
dependency, and these files need nothing a spreadsheet library adds: an `.xlsx` is a zip of XML
parts, and the only parts that hold values are the shared-string table and one XML file per sheet.

What it reads: every sheet's cells as their *stored text* — a shared string resolved to its text, a
number exactly as written in the XML (`"172738.89553549999"`), never coerced through `float`. A
caller that wants a `Decimal` builds it from that text, so this module can never be the place a
binary rounding error enters a series.

What it never does: evaluate a formula (the cached value is what the publisher saw and saved), apply
a number format, or guess a date from a serial number — those are the caller's decisions, made with
the publisher's layout in hand.
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Final
from xml.etree import ElementTree

__all__ = ["Sheet", "column_index", "read_workbook"]

_NS: Final = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS: Final = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL_NS: Final = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_CELL_REF: Final = re.compile(r"^([A-Z]+)(\d+)$")

#: One sheet: its name and a sparse grid of `(row, column) -> text`, both 1-based.
Sheet = tuple[str, dict[tuple[int, int], str]]


def column_index(letters: str) -> int:
    """`A` → 1, `Z` → 26, `AA` → 27: the spreadsheet column letters as a 1-based index."""
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index


def read_workbook(payload: bytes) -> tuple[Sheet, ...]:
    """Every sheet of an `.xlsx` payload, in workbook order, as sparse text grids.

    Raises `ValueError` when the payload is not a zip, or is a zip without a workbook part — a
    publisher that served an HTML error page with a 200 lands here rather than as an empty sheet.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as error:
        raise ValueError(f"payload is not an xlsx (not a zip archive): {error}") from error
    names = set(archive.namelist())
    if "xl/workbook.xml" not in names:
        raise ValueError("payload is a zip but carries no xl/workbook.xml — not an xlsx workbook")

    shared = _shared_strings(archive) if "xl/sharedStrings.xml" in names else []
    targets = _sheet_targets(archive)
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    sheets_node = workbook.find(f"{_NS}sheets")
    if sheets_node is None:
        raise ValueError("xlsx workbook declares no <sheets>")

    sheets: list[Sheet] = []
    for node in sheets_node:
        name = node.get("name") or ""
        rel_id = node.get(f"{_REL_NS}id") or ""
        target = targets.get(rel_id)
        if target is None:
            raise ValueError(f"xlsx sheet {name!r} names relationship {rel_id!r}, which is absent")
        sheets.append((name, _cells(ElementTree.fromstring(archive.read(target)), shared)))
    return tuple(sheets)


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    return [
        "".join(t.text or "" for t in item.iter(f"{_NS}t")) for item in root.findall(f"{_NS}si")
    ]


def _sheet_targets(archive: zipfile.ZipFile) -> dict[str, str]:
    """Relationship id → the sheet XML part it points at, from the workbook's relationships."""
    root = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets: dict[str, str] = {}
    for rel in root.findall(f"{_PKG_REL_NS}Relationship"):
        target = rel.get("Target") or ""
        target = target.lstrip("/")
        if not target.startswith("xl/"):
            target = f"xl/{target}"
        targets[rel.get("Id") or ""] = target
    return targets


def _cells(root: ElementTree.Element, shared: list[str]) -> dict[tuple[int, int], str]:
    grid: dict[tuple[int, int], str] = {}
    data = root.find(f"{_NS}sheetData")
    if data is None:
        return grid
    for cell in data.iter(f"{_NS}c"):
        match = _CELL_REF.match(cell.get("r") or "")
        if match is None:
            continue
        kind = cell.get("t")
        if kind == "inlineStr":
            text = "".join(t.text or "" for t in cell.iter(f"{_NS}t"))
        else:
            value = cell.find(f"{_NS}v")
            if value is None or value.text is None:
                continue
            text = shared[int(value.text)] if kind == "s" else value.text
        if text.strip():
            grid[(int(match.group(2)), column_index(match.group(1)))] = text
    return grid
