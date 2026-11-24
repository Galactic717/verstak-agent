from collections.abc import AsyncIterator
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
import pytest
from fastapi import FastAPI
from openpyxl import Workbook
from pypdf import PdfWriter
from pypdf.generic import ContentStream, DecodedStreamObject, DictionaryObject, NameObject

from verstak.documents import router
from verstak.documents.ingest import MAX_UPLOAD_BYTES, InvalidDocument, parse_document
from verstak.documents.repository import embedding_literal


def test_csv_keeps_identifiers_and_cell_values_as_strings() -> None:
    result = parse_document("invoices.csv", b"Code,Amount,Details\n0012,10.00,\n")
    assert result.tabular is not None
    assert result.tabular[0].sheet == "CSV"
    assert result.tabular[0].rows == [{"Code": "0012", "Amount": "10.00", "Details": ""}]
    assert "0012" in result.text


def test_excel_reads_named_sheets_and_their_own_headers() -> None:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Invoices"
    sheet.append(["Code", "Details"])
    sheet.append(["00001", "Overdue"])
    payments = workbook.create_sheet("Payments")
    payments.append(["Amount"])
    payments.append([12])
    file = BytesIO()
    workbook.save(file)
    result = parse_document("budget.xlsx", file.getvalue())
    assert result.tabular is not None
    assert [sheet.sheet for sheet in result.tabular] == ["Invoices", "Payments"]
    assert result.tabular[1].rows == [{"Amount": "12"}]
    assert "Overdue" in result.text


def test_rejects_scanned_or_blank_pdf_without_extractable_text() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    file = BytesIO()
    writer.write(file)
    with pytest.raises(InvalidDocument, match="No extractable text"):
        parse_document("blank.pdf", file.getvalue())


def test_pdf_extracts_invoice_text_without_ocr() -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/InvoiceFont"): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /InvoiceFont 12 Tf 50 720 Td (Invoice INV-42 USD 100.00) Tj ET")
    page.replace_contents(ContentStream(stream, writer))
    file = BytesIO()
    writer.write(file)
    result = parse_document("invoice.pdf", file.getvalue())
    assert result.media_type == "application/pdf"
    assert result.text == "Invoice INV-42 USD 100.00"
    assert result.tabular is None


def test_limits_expanded_xlsx_members_before_parsing() -> None:
    file = BytesIO()
    with ZipFile(file, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("xl/sharedStrings.xml", b"x" * (24 * 1024 * 1024 + 1))
    with pytest.raises(InvalidDocument, match="expanded size"):
        parse_document("oversized.xlsx", file.getvalue())


def test_malformed_csv_with_implicit_index_is_rejected() -> None:
    with pytest.raises(InvalidDocument, match="CSV"):
        parse_document("bad.csv", b"a,b\nx,y,z\n")


def test_rejects_input_larger_than_limit_and_non_utf8_text() -> None:
    with pytest.raises(InvalidDocument, match="5 MiB"):
        parse_document("large.txt", b"0" * (5 * 1024 * 1024 + 1))
    with pytest.raises(InvalidDocument, match="UTF-8"):
        parse_document("invalid.txt", b"\xff\xff")


def test_empty_or_nul_text_is_rejected() -> None:
    with pytest.raises(InvalidDocument, match="No extractable text"):
        parse_document("empty.txt", b"   \n")
    with pytest.raises(InvalidDocument, match="NUL"):
        parse_document("nul.txt", b"bad\x00data")


def test_embedding_is_stable_case_insensitive_and_nonzero_for_words() -> None:
    first = embedding_literal("Invoice invoice review")
    assert embedding_literal("INVOICE Review invoice") == first
    assert embedding_literal("Invoice") != first
    assert embedding_literal("!?") is None
    assert first is not None
    values = [float(number) for number in first.removeprefix("[").removesuffix("]").split(",")]
    assert len(values) == 256
    assert sum(value**2 for value in values) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_upload_rejects_oversize_stream_without_content_length() -> None:
    app = FastAPI()
    app.include_router(router)

    async def chunks() -> AsyncIterator[bytes]:
        yield b'--upload\r\nContent-Disposition: form-data; name="file"; filename="big.txt"\r\n\r\n'
        for _ in range(MAX_UPLOAD_BYTES // (64 * 1024) + 2):
            yield b"a" * (64 * 1024)
        yield b"\r\n--upload--\r\n"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as client:
        response = await client.post(
            "/documents",
            headers={"Content-Type": "multipart/form-data; boundary=upload"},
            content=chunks(),
        )
    assert response.status_code == 413
