from io import BytesIO
from uuid import uuid4

import pytest
from pypdf import PdfWriter

from erp_assistant.orchestration.fake_providers import FakeEmbeddingProvider
from erp_assistant.rag.chunker import chunk_pages
from erp_assistant.rag.errors import OcrRequiredError, PdfValidationError
from erp_assistant.rag.pdf_parser import ParsedPage, PypdfParser
from tests.pdf_factory import make_pdf


def test_pdf_parser_removes_repeated_margins_and_keeps_page_numbers() -> None:
    content = make_pdf(
        [
            "ERP Manual\nInventory policy page one contains enough useful text for parsing.\nPage",
            "ERP Manual\nPurchase policy page two contains enough useful text for parsing.\nPage",
            (
                "ERP Manual\nLogistics policy page three contains enough useful text "
                "for parsing.\nPage"
            ),
        ]
    )
    parsed = PypdfParser().parse(content, minimum_text_per_page=10)
    assert [page.page_number for page in parsed.pages] == [1, 2, 3]
    assert all("ERP Manual" not in page.text for page in parsed.pages)
    assert all("\n\nPage" not in page.text for page in parsed.pages)


def test_pdf_parser_marks_image_only_pdf_for_ocr() -> None:
    with pytest.raises(OcrRequiredError) as exc_info:
        PypdfParser().parse(make_pdf([""]), minimum_text_per_page=20)
    assert exc_info.value.code == "OCR_REQUIRED"


def test_pdf_parser_rejects_encrypted_and_invalid_pdf() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt("secret")
    output = BytesIO()
    writer.write(output)
    with pytest.raises(PdfValidationError, match="加密"):
        PypdfParser().inspect(output.getvalue(), max_pages=200)
    with pytest.raises(PdfValidationError, match="损坏"):
        PypdfParser().inspect(b"%PDF-invalid", max_pages=200)


def test_chunker_respects_page_boundary_and_max_size() -> None:
    document_id = uuid4()
    pages = [
        ParsedPage(page_number=1, text="Inventory Policy\n\n" + "stock " * 220),
        ParsedPage(page_number=2, text="Purchase Policy\n\n" + "order " * 180),
    ]
    chunks = chunk_pages(document_id, pages, target_size=300, max_size=400, overlap=50)
    assert chunks
    assert all(chunk.page_start == chunk.page_end for chunk in chunks)
    assert all(len(chunk.content) <= 400 for chunk in chunks)
    assert len({chunk.id for chunk in chunks}) == len(chunks)


def test_chunker_does_not_emit_overlap_only_tail() -> None:
    chunks = chunk_pages(
        uuid4(),
        [ParsedPage(page_number=1, text="a" * 310)],
        target_size=300,
        max_size=400,
        overlap=50,
    )
    assert len(chunks) == 1
    assert len(chunks[0].content) == 310


@pytest.mark.asyncio
async def test_fake_embedding_has_lexical_similarity_and_fixed_dimension() -> None:
    provider = FakeEmbeddingProvider()
    vectors = await provider.embed(["amazon inventory stock", "amazon inventory stock", "refund"])
    assert provider.model == "fake-lexical-v1"
    assert len(vectors[0]) == 256
    assert vectors[0] == vectors[1]
    assert vectors[0] != vectors[2]
