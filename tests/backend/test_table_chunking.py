from pathlib import Path

import pytest
import tiktoken
from docling_core.types.doc import (
    BoundingBox,
    DocItemLabel,
    DoclingDocument,
    ProvenanceItem,
    RichTableCell,
    Size,
    TableCell,
    TableData,
)

from backend.app.documents.config import DocumentConfig
from backend.app.documents.processing import DoclingChunker, DoclingParser, DocumentProcessor

FIXTURE = Path(__file__).parents[1] / "fixtures" / "financial_table.html"


@pytest.fixture(scope="module")
def financial_document():
    return DoclingParser().parse(FIXTURE, "html", DocumentConfig())


def cost_rows(chunks):
    return [chunk for chunk in chunks if "Row: Cost of revenue" in chunk.text]


def test_sec_html_keeps_company_units_headers_and_values_together(financial_document):
    original = financial_document.model_dump_json()
    assert not any(c.column_header for c in financial_document.tables[0].data.table_cells)
    chunks = DoclingChunker().chunk(financial_document, DocumentConfig())
    rows = cost_rows(chunks)
    assert len(rows) == 1
    row = rows[0]
    for text in (
        "NVIDIA Corporation",
        "Statements of Income",
        "In millions",
        "Three Months Ended / Jul 26, 2026: 24,079",
        "Three Months Ended / Jul 27, 2025: 12,890",
        "Six Months Ended / Jul 26, 2026: 44,538",
        "Six Months Ended / Jul 27, 2025: 30,284",
    ):
        assert text in row.embedding_text
    assert sum("24,079" in chunk.text for chunk in chunks) == 1  # No native table duplicate.
    assert any("paragraph after the table" in chunk.text for chunk in chunks)
    assert any("Three Months Ended / Jul 26, 2026: $ 96,221" in c.text for c in chunks)
    research = next(c for c in chunks if "Row: Research and development" in c.text)
    assert "Table section: Operating expenses" in research.text
    assert row.references[0]["table_rows"] == [3]
    assert row.references[0]["cells"][0]["value_cells"] == [[3, 4, 3, 5]]
    assert row.references[0]["cells"][0]["header_cells"] == [[0, 1, 3, 12], [1, 2, 3, 6]]
    assert any(ref.get("role") == "table_context" for ref in row.references)
    assert financial_document.model_dump_json() == original


def test_wide_rows_split_between_complete_labeled_values(financial_document):
    config = DocumentConfig()
    config.chunking.max_tokens = 64
    chunks = DoclingChunker().chunk(financial_document, config)
    rows = cost_rows(chunks)
    assert len(rows) > 1
    encoder = tiktoken.get_encoding(config.chunking.tokenizer)
    for chunk in rows:
        assert len(encoder.encode(chunk.embedding_text)) <= 64
        assert chunk.token_count <= 64
        assert "Months Ended / Jul" in chunk.text
    combined = "\n".join(c.text for c in rows)
    for value in ("24,079", "12,890", "44,538", "30,284"):
        assert combined.count(value) == 1


@pytest.mark.parametrize("setting", ["native", "no_inference"])
def test_configuration_can_restore_native_table_chunking(financial_document, setting):
    config = DocumentConfig()
    if setting == "native":
        config.chunking.table_strategy = "docling"
    else:
        config.chunking.infer_table_headers = False
    chunks = DoclingChunker().chunk(financial_document, config)
    assert any("24,079" in c.text for c in chunks)
    assert not cost_rows(chunks)


def cell(text, row, col, *, columns=1, header=False, rows=1):
    return TableCell(
        text=text,
        start_row_offset_idx=row,
        end_row_offset_idx=row + rows,
        start_col_offset_idx=col,
        end_col_offset_idx=col + columns,
        row_span=rows,
        col_span=columns,
        column_header=header,
    )


def test_pdf_table_representation_retains_page_bounds_and_equal_values():
    document = DoclingDocument(name="PDF table")
    document.add_page(page_no=7, size=Size(width=600, height=800))
    document.add_heading("Acme income statement")
    document.add_text(label=DocItemLabel.TEXT, text="USD millions")
    document.add_table(
        data=TableData(
            num_rows=3,
            num_cols=3,
            table_cells=[
                cell("Metric", 0, 0, header=True),
                cell("2026", 0, 1, header=True),
                cell("2025", 0, 2, header=True),
                cell("Revenue", 1, 0, rows=2),
                cell("0", 1, 1),
                cell("0", 1, 2),
                cell("8", 2, 1),
                cell("9", 2, 2),
            ],
        ),
        prov=ProvenanceItem(page_no=7, bbox=BoundingBox(l=10, t=20, r=500, b=400), charspan=(0, 0)),
    )
    chunks = DoclingChunker().chunk(document, DocumentConfig())
    rows = [c for c in chunks if "table_rows" in c.references[0]]
    assert len(rows) == 2
    assert "2026: 0" in rows[0].text and "2025: 0" in rows[0].text
    assert "Row: Revenue" in rows[1].text
    for chunk in rows:
        assert chunk.pages == [7]
        assert chunk.headings == ["Acme income statement"]
        assert "USD millions" in chunk.text
        assert chunk.references[0]["provenance"][0]["bbox"]["l"] == 10


def test_ambiguous_merged_value_falls_back_without_dropping_content():
    document = DoclingDocument(name="ambiguous table")
    document.add_table(
        data=TableData(
            num_rows=2,
            num_cols=3,
            table_cells=[
                cell("Metric", 0, 0, header=True),
                cell("2026", 0, 1, header=True),
                cell("2025", 0, 2, header=True),
                cell("Revenue", 1, 0),
                cell("ambiguous-value", 1, 1, columns=2),
            ],
        )
    )
    chunks = DoclingChunker().chunk(document, DocumentConfig())
    assert any("ambiguous-value" in c.text for c in chunks)
    assert all("table_rows" not in ref for c in chunks for ref in c.references)


def test_table_context_does_not_cross_another_table():
    document = DoclingDocument(name="separate tables")
    document.add_text(label=DocItemLabel.TEXT, text="First company confidential context")
    for label in ("First table", "Second table"):
        document.add_table(
            data=TableData(
                num_rows=2,
                num_cols=2,
                table_cells=[
                    cell("Metric", 0, 0, header=True),
                    cell("2026", 0, 1, header=True),
                    cell(label, 1, 0),
                    cell("123", 1, 1),
                ],
            )
        )
    chunks = DoclingChunker().chunk(document, DocumentConfig())
    second = next(c for c in chunks if "Row: Second table" in c.text)
    assert "First company" not in second.text


def test_new_table_settings_invalidate_chunk_cache(tmp_path):
    processor = DocumentProcessor(tmp_path)
    old = DocumentConfig()
    old.chunking.table_strategy = "docling"
    old_revision, old_chunks = processor.process(FIXTURE, "fixture-hash", "html", old)
    new_revision, new_chunks = processor.process(FIXTURE, "fixture-hash", "html", DocumentConfig())
    assert old_revision != new_revision
    assert not cost_rows(old_chunks)
    assert cost_rows(new_chunks)
    assert len(list((tmp_path / "parsed").glob("*.json"))) == 1  # Reuse unchanged parsing.


def test_text_only_rich_header_cells_are_supported_without_duplicate_children():
    document = DoclingDocument(name="rich headers")
    table = document.add_table(
        data=TableData(
            num_rows=2,
            num_cols=2,
            table_cells=[
                cell("Metric", 0, 0, header=True),
                cell("2026", 0, 1, header=True),
                cell("Revenue", 1, 0),
                cell("456", 1, 1),
            ],
        )
    )
    group = document.add_group(parent=table)
    header = document.add_text(label=DocItemLabel.TEXT, text="2026", parent=group)
    table.data.table_cells[1] = RichTableCell(
        **table.data.table_cells[1].model_dump(), ref=group.get_ref()
    )
    chunks = DoclingChunker().chunk(document, DocumentConfig())
    row = next(chunk for chunk in chunks if "Row: Revenue" in chunk.text)
    assert "2026: 456" in row.text
    assert row.references[0]["table_rows"] == [1]
    assert all(ref["ref"] != header.self_ref for chunk in chunks for ref in chunk.references)


def test_leading_body_section_is_not_mistaken_for_a_column_header(tmp_path):
    source = FIXTURE.read_text(encoding="utf-8").replace(
        '<tr><td colspan="3">Revenue',
        '<tr><td colspan="3">Net sales:</td><td colspan="21"></td></tr><tr><td colspan="3">Revenue',
    )
    original = tmp_path / "section.html"
    original.write_text(source, encoding="utf-8")
    document = DoclingParser().parse(original, "html", DocumentConfig())
    chunks = DoclingChunker().chunk(document, DocumentConfig())
    row = next(chunk for chunk in chunks if "Row: Revenue" in chunk.text)
    assert "Table section: Net sales:" in row.text
    assert "Three Months Ended / Jul 26, 2026: $ 96,221" in row.text
