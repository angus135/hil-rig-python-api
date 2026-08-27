"""Dependency-free OOXML export for the human-readable Excel workbook."""

from __future__ import annotations

import json
import re
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

from hilrig.models.execution import CompiledTestIR, IRScalar

_SHEET_NAMES = ("Test Summary", "Configurations", "Instructions", "Assertions")
_INVALID_XML_CHARACTERS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_CONTENT_TYPE_BASE = "application/vnd.openxmlformats-officedocument.spreadsheetml"


def write_human_readable_workbook(compiled: CompiledTestIR, path: str | Path) -> Path:
    """Write a formatted four-sheet ``.xlsx`` workbook."""
    output_path = Path(path).expanduser()
    if output_path.suffix.lower() != ".xlsx":
        raise ValueError("Excel workbook path must end in .xlsx")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    sheets = _workbook_rows(compiled)
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _write_text(archive, "[Content_Types].xml", _content_types_xml())
        _write_text(archive, "_rels/.rels", _package_relationships_xml())
        _write_text(archive, "docProps/app.xml", _app_properties_xml())
        _write_text(archive, "docProps/core.xml", _core_properties_xml(compiled.name))
        _write_text(archive, "xl/workbook.xml", _workbook_xml())
        _write_text(archive, "xl/_rels/workbook.xml.rels", _workbook_relationships_xml())
        _write_text(archive, "xl/styles.xml", _styles_xml())
        for index, (sheet_name, rows) in enumerate(sheets.items(), start=1):
            _write_text(
                archive,
                f"xl/worksheets/sheet{index}.xml",
                _worksheet_xml(sheet_name, rows),
            )
    return output_path.resolve()


def _workbook_rows(compiled: CompiledTestIR) -> dict[str, list[list[object]]]:
    summary = [
        ["HIL-RIG Compiled Test", ""],
        [],
        ["Field", "Value"],
        ["IR version", f"v{compiled.schema_version}"],
        ["Test ID", compiled.test_id_hex],
        ["Test name", compiled.name],
        ["Frequency mode", compiled.frequency_mode],
        ["Frequency (Hz)", compiled.frequency_hz],
        ["Expected tick count", compiled.expected_tick_count],
        ["Start mode", compiled.start_mode],
        ["Configurations", len(compiled.configurations)],
        ["Instructions", len(compiled.instructions)],
        ["Assertions", len(compiled.assertions)],
    ]

    configurations: list[list[object]] = [["Peripheral", "Channel", "Parameters"]]
    configurations.extend(
        [item.peripheral, item.channel, _format_arguments(item.parameters)]
        for item in compiled.configurations
    )
    if not compiled.configurations:
        configurations.append(["No peripheral configurations", "", ""])

    instructions: list[list[object]] = [
        ["Tick", "Instruction ID", "Peripheral", "Channel", "Instruction", "Arguments"]
    ]
    instructions.extend(
        [
            item.tick,
            item.instruction_id,
            item.peripheral,
            item.channel,
            item.operation,
            _format_arguments(item.arguments),
        ]
        for item in compiled.instructions
    )
    if not compiled.instructions:
        instructions.append(["No stimulus instructions", "", "", "", "", ""])

    assertions: list[list[object]] = [
        ["Assertion ID", "Peripheral", "Channel", "Assertion", "Arguments"]
    ]
    assertions.extend(
        [
            item.assertion_id,
            item.peripheral,
            item.channel,
            item.assertion,
            _format_arguments(item.arguments),
        ]
        for item in compiled.assertions
    )
    if not compiled.assertions:
        assertions.append(["No host-side assertions", "", "", "", ""])

    return {
        "Test Summary": summary,
        "Configurations": configurations,
        "Instructions": instructions,
        "Assertions": assertions,
    }


def _format_arguments(arguments: Mapping[str, IRScalar]) -> str:
    return "; ".join(
        f"{name}={json.dumps(value, ensure_ascii=False)}" for name, value in arguments.items()
    )


def _worksheet_xml(sheet_name: str, rows: Sequence[Sequence[object]]) -> bytes:
    worksheet = Element("worksheet", {"xmlns": _MAIN_NS})
    if sheet_name != "Test Summary":
        views = SubElement(worksheet, "sheetViews")
        view = SubElement(views, "sheetView", {"workbookViewId": "0"})
        SubElement(
            view,
            "pane",
            {
                "ySplit": "1",
                "topLeftCell": "A2",
                "activePane": "bottomLeft",
                "state": "frozen",
            },
        )

    widths = _column_widths(rows)
    columns = SubElement(worksheet, "cols")
    for index, width in enumerate(widths, start=1):
        SubElement(
            columns,
            "col",
            {"min": str(index), "max": str(index), "width": f"{width:.1f}", "customWidth": "1"},
        )

    sheet_data = SubElement(worksheet, "sheetData")
    for row_index, row in enumerate(rows, start=1):
        row_element = SubElement(sheet_data, "row", {"r": str(row_index)})
        if sheet_name == "Test Summary" and row_index == 1:
            row_element.set("ht", "26")
            row_element.set("customHeight", "1")
        for column_index, value in enumerate(row, start=1):
            style = _cell_style(sheet_name, row_index, column_index)
            _append_cell(row_element, row_index, column_index, value, style)

    if sheet_name == "Test Summary":
        merged = SubElement(worksheet, "mergeCells", {"count": "1"})
        SubElement(merged, "mergeCell", {"ref": "A1:B1"})
    else:
        last_column = _column_name(max((len(row) for row in rows), default=1))
        SubElement(worksheet, "autoFilter", {"ref": f"A1:{last_column}{len(rows)}"})

    SubElement(
        worksheet,
        "pageMargins",
        {
            "left": "0.4",
            "right": "0.4",
            "top": "0.6",
            "bottom": "0.6",
            "header": "0.3",
            "footer": "0.3",
        },
    )
    return _xml_bytes(worksheet)


def _cell_style(sheet_name: str, row: int, column: int) -> int:
    if sheet_name == "Test Summary" and row == 1:
        return 1
    if (sheet_name == "Test Summary" and row == 3) or (sheet_name != "Test Summary" and row == 1):
        return 2
    if sheet_name == "Test Summary" and row >= 4 and column == 1:
        return 3
    return 4


def _append_cell(
    row: Element, row_index: int, column_index: int, value: object, style: int
) -> None:
    reference = f"{_column_name(column_index)}{row_index}"
    attributes = {"r": reference, "s": str(style)}
    if isinstance(value, bool):
        cell = SubElement(row, "c", {**attributes, "t": "b"})
        SubElement(cell, "v").text = "1" if value else "0"
    elif isinstance(value, (int, float)):
        cell = SubElement(row, "c", attributes)
        SubElement(cell, "v").text = str(value)
    else:
        cell = SubElement(row, "c", {**attributes, "t": "inlineStr"})
        inline = SubElement(cell, "is")
        text = SubElement(inline, "t")
        text.text = _safe_text(str(value))


def _column_widths(rows: Sequence[Sequence[object]]) -> list[float]:
    column_count = max((len(row) for row in rows), default=1)
    widths: list[float] = []
    for column_index in range(column_count):
        content_width = max(
            (len(str(row[column_index])) for row in rows if column_index < len(row)),
            default=10,
        )
        widths.append(float(min(max(content_width + 2, 12), 60)))
    return widths


def _column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _safe_text(value: str) -> str:
    return _INVALID_XML_CHARACTERS.sub("�", value)[:32_767]


def _content_types_xml() -> bytes:
    root = Element(
        "Types", {"xmlns": "http://schemas.openxmlformats.org/package/2006/content-types"}
    )
    SubElement(
        root,
        "Default",
        {
            "Extension": "rels",
            "ContentType": "application/vnd.openxmlformats-package.relationships+xml",
        },
    )
    SubElement(root, "Default", {"Extension": "xml", "ContentType": "application/xml"})
    SubElement(
        root,
        "Override",
        {
            "PartName": "/xl/workbook.xml",
            "ContentType": f"{_CONTENT_TYPE_BASE}.sheet.main+xml",
        },
    )
    SubElement(
        root,
        "Override",
        {
            "PartName": "/xl/styles.xml",
            "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml",
        },
    )
    SubElement(
        root,
        "Override",
        {
            "PartName": "/docProps/core.xml",
            "ContentType": "application/vnd.openxmlformats-package.core-properties+xml",
        },
    )
    SubElement(
        root,
        "Override",
        {
            "PartName": "/docProps/app.xml",
            "ContentType": "application/vnd.openxmlformats-officedocument.extended-properties+xml",
        },
    )
    for index in range(1, 5):
        SubElement(
            root,
            "Override",
            {
                "PartName": f"/xl/worksheets/sheet{index}.xml",
                "ContentType": f"{_CONTENT_TYPE_BASE}.worksheet+xml",
            },
        )
    return _xml_bytes(root)


def _package_relationships_xml() -> bytes:
    root = Element(
        "Relationships", {"xmlns": "http://schemas.openxmlformats.org/package/2006/relationships"}
    )
    SubElement(
        root,
        "Relationship",
        {
            "Id": "rId1",
            "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
            "Target": "xl/workbook.xml",
        },
    )
    SubElement(
        root,
        "Relationship",
        {
            "Id": "rId2",
            "Type": "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties",
            "Target": "docProps/core.xml",
        },
    )
    SubElement(
        root,
        "Relationship",
        {
            "Id": "rId3",
            "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties",
            "Target": "docProps/app.xml",
        },
    )
    return _xml_bytes(root)


def _workbook_xml() -> bytes:
    root = Element("workbook", {"xmlns": _MAIN_NS, "xmlns:r": _REL_NS})
    sheets = SubElement(root, "sheets")
    for index, name in enumerate(_SHEET_NAMES, start=1):
        SubElement(
            sheets,
            "sheet",
            {"name": name, "sheetId": str(index), f"{{{_REL_NS}}}id": f"rId{index}"},
        )
    return _xml_bytes(root)


def _workbook_relationships_xml() -> bytes:
    root = Element(
        "Relationships", {"xmlns": "http://schemas.openxmlformats.org/package/2006/relationships"}
    )
    for index in range(1, 5):
        SubElement(
            root,
            "Relationship",
            {
                "Id": f"rId{index}",
                "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet",
                "Target": f"worksheets/sheet{index}.xml",
            },
        )
    SubElement(
        root,
        "Relationship",
        {
            "Id": "rId5",
            "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles",
            "Target": "styles.xml",
        },
    )
    return _xml_bytes(root)


def _styles_xml() -> bytes:
    root = Element("styleSheet", {"xmlns": _MAIN_NS})
    fonts = SubElement(root, "fonts", {"count": "4"})
    for bold, color, size in (
        (False, None, 11),
        (True, "FFFFFF", 11),
        (True, "FFFFFF", 16),
        (True, "1F1F1F", 11),
    ):
        font = SubElement(fonts, "font")
        if bold:
            SubElement(font, "b")
        SubElement(font, "sz", {"val": str(size)})
        if color:
            SubElement(font, "color", {"rgb": f"FF{color}"})
        SubElement(font, "name", {"val": "Calibri"})
        SubElement(font, "family", {"val": "2"})
    fills = SubElement(root, "fills", {"count": "5"})
    SubElement(SubElement(fills, "fill"), "patternFill", {"patternType": "none"})
    SubElement(SubElement(fills, "fill"), "patternFill", {"patternType": "gray125"})
    for color in ("1F4E78", "0F6B78", "DDEBF7"):
        pattern = SubElement(SubElement(fills, "fill"), "patternFill", {"patternType": "solid"})
        SubElement(pattern, "fgColor", {"rgb": f"FF{color}"})
        SubElement(pattern, "bgColor", {"indexed": "64"})
    borders = SubElement(root, "borders", {"count": "2"})
    _border(borders, styled=False)
    _border(borders, styled=True)
    SubElement(root, "cellStyleXfs", {"count": "1"}).append(
        Element("xf", {"numFmtId": "0", "fontId": "0", "fillId": "0", "borderId": "0"})
    )
    cell_xfs = SubElement(root, "cellXfs", {"count": "5"})
    SubElement(
        cell_xfs,
        "xf",
        {"numFmtId": "0", "fontId": "0", "fillId": "0", "borderId": "0", "xfId": "0"},
    )
    _xf(cell_xfs, font="2", fill="2", alignment={"horizontal": "center", "vertical": "center"})
    _xf(
        cell_xfs,
        font="1",
        fill="3",
        alignment={"horizontal": "center", "vertical": "center", "wrapText": "1"},
    )
    _xf(cell_xfs, font="3", fill="4", alignment={"vertical": "top", "wrapText": "1"})
    _xf(cell_xfs, font="0", fill="0", alignment={"vertical": "top", "wrapText": "1"})
    styles = SubElement(root, "cellStyles", {"count": "1"})
    SubElement(styles, "cellStyle", {"name": "Normal", "xfId": "0", "builtinId": "0"})
    return _xml_bytes(root)


def _border(parent: Element, *, styled: bool) -> None:
    border = SubElement(parent, "border")
    for edge_name in ("left", "right", "top", "bottom"):
        edge = SubElement(border, edge_name, {"style": "thin"} if styled else {})
        if styled:
            SubElement(edge, "color", {"rgb": "FFD9E2F3"})
    SubElement(border, "diagonal")


def _xf(parent: Element, *, font: str, fill: str, alignment: dict[str, str]) -> None:
    xf = SubElement(
        parent,
        "xf",
        {
            "numFmtId": "0",
            "fontId": font,
            "fillId": fill,
            "borderId": "1",
            "xfId": "0",
            "applyFont": "1",
            "applyFill": "1",
            "applyBorder": "1",
            "applyAlignment": "1",
        },
    )
    SubElement(xf, "alignment", alignment)


def _app_properties_xml() -> bytes:
    root = Element(
        "Properties",
        {
            "xmlns": "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties",
            "xmlns:vt": "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes",
        },
    )
    SubElement(root, "Application").text = "HIL-RIG Python API"
    SubElement(root, "AppVersion").text = "1.0"
    return _xml_bytes(root)


def _core_properties_xml(title: str) -> bytes:
    root = Element(
        "cp:coreProperties",
        {
            "xmlns:cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
            "xmlns:dc": "http://purl.org/dc/elements/1.1/",
        },
    )
    SubElement(root, "dc:title").text = _safe_text(title)
    SubElement(root, "dc:creator").text = "HIL-RIG Python API"
    return _xml_bytes(root)


def _xml_bytes(element: Element) -> bytes:
    return b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + tostring(
        element, encoding="utf-8"
    )


def _write_text(archive: zipfile.ZipFile, name: str, content: bytes) -> None:
    archive.writestr(name, content)
