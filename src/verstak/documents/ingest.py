import re
from dataclasses import dataclass
from io import BytesIO, StringIO
from pathlib import PurePosixPath
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile

import pandas as pd  # type: ignore[import-untyped]
from pydantic import BaseModel
from pypdf import PdfReader
from pypdf.errors import PyPdfError

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARACTERS = 50_000
MAX_PDF_PAGES = 30
MAX_XLSX_EXPANDED_BYTES = 24 * 1024 * 1024
MAX_XLSX_ENTRIES = 200
MAX_SHEETS = 8
MAX_ROWS = 250
MAX_COLUMNS = 30
MEDIA_TYPES = {
    ".csv": "text/csv",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


class InvalidDocument(ValueError):
    """A public validation error whose message excludes input contents."""


class Sheet(BaseModel):
    sheet: str
    rows: list[dict[str, str]]


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    filename: str
    media_type: str
    size_bytes: int
    text: str
    tabular: list[Sheet] | None


def _decode(payload: bytes) -> str:
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InvalidDocument("CSV and text files must be UTF-8") from exc


def _extract_pdf(payload: bytes) -> str:
    try:
        reader = PdfReader(BytesIO(payload))
        if reader.is_encrypted:
            raise InvalidDocument("Encrypted PDFs are unsupported")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise InvalidDocument(f"PDF files may contain at most {MAX_PDF_PAGES} pages")
        pages: list[str] = []
        characters = 0
        for page in reader.pages:
            text = page.extract_text()
            characters += len(text) + 1
            if characters > MAX_TEXT_CHARACTERS:
                raise InvalidDocument("Extracted text exceeds the character limit")
            pages.append(text)
        return "\n".join(pages)
    except InvalidDocument:
        raise
    except (PyPdfError, KeyError, TypeError, ValueError, OSError, NotImplementedError) as exc:
        raise InvalidDocument("Could not read PDF") from exc


def _sheet_from_frame(name: str, frame: pd.DataFrame) -> Sheet:
    if len(frame.columns) > MAX_COLUMNS:
        raise InvalidDocument(f"Spreadsheets may contain at most {MAX_COLUMNS} columns")
    if len(frame) > MAX_ROWS:
        raise InvalidDocument(f"Spreadsheets may contain at most {MAX_ROWS} data rows in total")
    columns = [str(column) for column in frame.columns]
    rows = [
        dict(zip(columns, (str(cell) for cell in cells), strict=True))
        for cells in frame.itertuples(index=False, name=None)
    ]
    return Sheet(sheet=name, rows=rows)


def _extract_csv(payload: bytes) -> list[Sheet]:
    text = _decode(payload)
    if "\x00" in text:
        raise InvalidDocument("Document content contains a NUL character")
    try:
        frame = pd.read_csv(StringIO(text), dtype=str, keep_default_na=False, nrows=MAX_ROWS + 1)
    except (ValueError, TypeError, OSError) as exc:
        raise InvalidDocument("Could not read CSV") from exc
    if len(frame) and not isinstance(frame.index, pd.RangeIndex):
        raise InvalidDocument("CSV rows must match the header columns")
    return [_sheet_from_frame("CSV", frame)]


def _extract_workbook(payload: bytes) -> list[Sheet]:
    try:
        with ZipFile(BytesIO(payload)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_XLSX_ENTRIES:
                raise InvalidDocument("XLSX archive contains too many entries")
            if sum(entry.file_size for entry in entries) > MAX_XLSX_EXPANDED_BYTES:
                raise InvalidDocument("XLSX archive exceeds the expanded size limit")
        with pd.ExcelFile(BytesIO(payload), engine="openpyxl") as workbook:
            if len(workbook.sheet_names) > MAX_SHEETS:
                raise InvalidDocument(f"Workbooks may contain at most {MAX_SHEETS} sheets")
            sheets: list[Sheet] = []
            remaining_rows = MAX_ROWS
            for name in workbook.sheet_names:
                frame = pd.read_excel(
                    workbook,
                    sheet_name=name,
                    dtype=str,
                    keep_default_na=False,
                    nrows=remaining_rows + 1,
                )
                sheet = _sheet_from_frame(name, frame)
                remaining_rows -= len(sheet.rows)
                if remaining_rows < 0:
                    raise InvalidDocument(
                        f"Workbooks may contain at most {MAX_ROWS} data rows in total"
                    )
                sheets.append(sheet)
            return sheets
    except InvalidDocument:
        raise
    except (BadZipFile, OSError, ValueError, KeyError, TypeError, ParseError, RuntimeError) as exc:
        raise InvalidDocument("Could not read XLSX workbook") from exc


def _render_sheets(sheets: list[Sheet]) -> str:
    sections: list[str] = []
    for sheet in sheets:
        if sheet.rows:
            header = "\t".join(sheet.rows[0])
            rows = ["\t".join(row.values()) for row in sheet.rows]
            sections.append("\n".join([f"[{sheet.sheet}]", header, *rows]))
    return "\n\n".join(sections)


def parse_document(filename: str, payload: bytes) -> ParsedDocument:
    if len(payload) > MAX_UPLOAD_BYTES:
        raise InvalidDocument("Documents may contain at most 5 MiB")
    basename = PurePosixPath(filename.replace("\\", "/")).name
    basename = re.sub(r"[\x00-\x1f\x7f]", "", basename).strip()
    suffix = PurePosixPath(basename).suffix.lower()
    media_type = MEDIA_TYPES.get(suffix)
    if media_type is None:
        raise InvalidDocument("Use a PDF, CSV, XLSX, or TXT file")
    if len(basename) > 255:
        raise InvalidDocument("The file name exceeds the character limit")

    tabular = None
    match suffix:
        case ".pdf":
            text = _extract_pdf(payload)
        case ".csv":
            tabular = _extract_csv(payload)
            text = _render_sheets(tabular)
        case ".xlsx":
            tabular = _extract_workbook(payload)
            text = _render_sheets(tabular)
        case _:
            text = _decode(payload)

    text = text.strip()
    if not text:
        raise InvalidDocument("No extractable text was found")
    if "\x00" in text:
        raise InvalidDocument("Document content contains a NUL character")
    if len(text) > MAX_TEXT_CHARACTERS:
        raise InvalidDocument("Extracted text exceeds the character limit")
    return ParsedDocument(basename, media_type, len(payload), text, tabular)
