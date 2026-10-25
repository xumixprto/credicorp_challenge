"""Contextual table rows from Docling cells, shared by HTML and PDF conversion.

Original documents and cell grids are never mutated. Ambiguous/oversized tables
fall back to Docling's serializer, rather than guessing a value's column header.
"""

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import tiktoken

from backend.app.documents.config import ChunkingConfig
from backend.app.documents.models import Chunk

if TYPE_CHECKING:
    from docling_core.types.doc import DocItem, DoclingDocument, TableCell, TableItem

logger = logging.getLogger("credicorp.backend.documents.tables")
_NUMBER = re.compile(r"^[\s$€£¥()+\-−–—\d.,%]+$")
_PERIOD = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?"
    r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
    r"|(?:19|20)\d{2}|months? ended|years? ended|quarters? ended)\b",
    re.I,
)
_SYMBOLS = {"$", "€", "£", "¥", "%", "(", ")"}


def clean(text: str) -> str:
    return " ".join(text.split())


def coordinates(cell: "TableCell") -> tuple[int, int, int, int]:
    return (
        cell.start_row_offset_idx,
        cell.end_row_offset_idx,
        cell.start_col_offset_idx,
        cell.end_col_offset_idx,
    )


def source_reference(item: "DocItem", **extra) -> dict:
    return {
        "ref": item.self_ref,
        "label": item.label.value,
        "provenance": [entry.model_dump(mode="json") for entry in item.prov],
        **extra,
    }


def _rows(table: "TableItem") -> list[list["TableCell"]]:
    # grid resolves row/column spans; deduplicate by origin, never by text/value.
    return [
        list({coordinates(cell): cell for cell in row if clean(cell.text)}.values())
        for row in table.data.grid
    ]


def _numeric(text: str) -> bool:
    return bool(_NUMBER.fullmatch(clean(text)))


def _header_end(rows: list[list["TableCell"]], config: ChunkingConfig) -> int | None:
    nonempty = [(i, row) for i, row in enumerate(rows) if row]
    if not nonempty:
        return None
    explicit = []
    for i, row in nonempty:
        if any(cell.column_header for cell in row):
            explicit.append(i)
        else:
            break
    if explicit:
        return explicit[-1] + 1
    if not config.infer_table_headers:
        return None
    for i, row in nonempty:
        if i >= config.table_header_max_rows:
            return None
        if (
            len(row) >= 2
            and not _numeric(row[0].text)
            and any(_numeric(cell.text) and clean(cell.text) not in _SYMBOLS for cell in row[1:])
        ):
            candidates = [r for _, r in nonempty if r and r[0].start_row_offset_idx < i]
            # Recognize date/period headers, or a single conventional textual header row.
            dated = [
                index
                for index, preceding in nonempty
                if index < i
                and len(preceding) > 1
                and any(_PERIOD.search(cell.text) for cell in preceding)
            ]
            if dated:
                # A label-only row such as "Net sales:" after the dates is a body section.
                return max(dated) + 1
            if (
                len(candidates) == 1
                and len(candidates[0]) >= 2
                and all(not _numeric(cell.text) for cell in candidates[0])
            ):
                return i
            return None
    return None


@dataclass
class Field:
    header: str
    value: str
    header_cells: list["TableCell"]
    value_cells: list["TableCell"]


def _fields(cells: list["TableCell"], headers: list[list["TableCell"]]) -> list[Field] | None:
    fields: list[Field] = []
    for cell in cells:
        chain = []
        for row in headers:
            overlapping = [
                header
                for header in row
                if header.start_col_offset_idx < cell.end_col_offset_idx
                and header.end_col_offset_idx > cell.start_col_offset_idx
            ]
            if len(overlapping) > 1:
                return None  # A value spans conflicting headers: retain the original table instead.
            if overlapping:
                chain.append(overlapping[0])
        if not chain:
            return None
        identity = [coordinates(header) for header in chain]
        if fields and identity == [coordinates(h) for h in fields[-1].header_cells]:
            # Currency/percent sign columns share a header with their value.
            if clean(cell.text) not in _SYMBOLS and fields[-1].value not in _SYMBOLS:
                return None  # Two independent values under indistinguishable headers.
            fields[-1].value += " " + clean(cell.text)
            fields[-1].value_cells.append(cell)
        else:
            fields.append(
                Field(
                    header=" / ".join(dict.fromkeys(clean(h.text) for h in chain)),
                    value=clean(cell.text),
                    header_cells=chain,
                    value_cells=[cell],
                )
            )
    return fields


def _context(
    items: list["DocItem"], encoding: tiktoken.Encoding, limit: int
) -> tuple[list[str], list[dict]]:
    text, refs = [], []
    for item in reversed(items):
        value = clean(getattr(item, "text", ""))
        if not value or value.isdigit() or value in text:
            continue
        # Keep whole context items. Do not cut a title or silently rewrite units.
        if len(encoding.encode("\n".join([value, *text]), disallowed_special=())) <= limit:
            text.insert(0, value)
            refs.insert(0, source_reference(item, role="table_context"))
    return text, refs


def table_chunks(
    table: "TableItem",
    context: list["DocItem"],
    headings: list[str],
    config: ChunkingConfig,
    encoding: tiktoken.Encoding,
) -> list[Chunk] | None:
    from docling_core.types.doc import RichTableCell

    if any(isinstance(cell, RichTableCell) for cell in table.data.table_cells):
        return None  # Rich/nested tables retain their native subtree serialization.
    rows = _rows(table)
    header_end = _header_end(rows, config)
    if header_end is None or header_end >= len(rows):
        return None
    headers = [row for row in rows[:header_end] if row]
    if not headers:
        return None
    context_text, context_refs = _context(
        context, encoding, min(config.table_context_max_tokens, config.max_tokens // 3)
    )
    prefix_lines = list(dict.fromkeys([*headings, *context_text]))
    prefix = "\n".join(prefix_lines)
    chunks: list[Chunk] = []
    section = ""

    def count(text: str) -> int:
        return len(encoding.encode(text, disallowed_special=()))

    def emit(row_index: int, label: "TableCell", fields: list[Field], text: str) -> None:
        chunks.append(
            Chunk(
                index=0,
                text=text,
                embedding_text=text,
                token_count=count(text),
                headings=list(headings),
                pages=sorted({p.page_no for p in table.prov}),
                references=[
                    source_reference(
                        table,
                        table_rows=[row_index],
                        row_label_cell=list(coordinates(label)),
                        cells=[
                            {
                                "value_cells": [list(coordinates(c)) for c in f.value_cells],
                                "header_cells": [list(coordinates(c)) for c in f.header_cells],
                                "column_header": f.header,
                            }
                            for f in fields
                        ],
                    ),
                    *context_refs,
                ],
            )
        )

    for row_index in range(header_end, len(rows)):
        row = rows[row_index]
        if not row:
            continue
        label, *values = row
        if not values:
            section = clean(label.text)
            text = "\n".join(filter(None, [prefix, f"Table section: {section}"]))
            if count(text) > config.max_tokens:
                return None
            emit(row_index, label, [], text)
            continue
        fields = _fields(values, headers)
        if fields is None:
            return None
        row_prefix = "\n".join(
            filter(
                None,
                [
                    prefix,
                    f"Table section: {section}" if section else "",
                    f"Row: {clean(label.text)}",
                ],
            )
        )
        group: list[Field] = []
        text = row_prefix
        for field in fields:
            line = f"{field.header}: {field.value}"
            if count(row_prefix + "\n" + line) > config.max_tokens:
                return None  # Never split/truncate a single labeled value to force it to fit.
            candidate = text + "\n" + line
            if count(candidate) > config.max_tokens:
                emit(row_index, label, group, text)
                group, text = [], row_prefix
            group.append(field)
            text += "\n" + line
        if group:
            emit(row_index, label, group, text)
    return chunks or None


def contextual_tables(
    document: "DoclingDocument",
    config: ChunkingConfig,
    encoding: tiktoken.Encoding,
) -> dict[str, list[Chunk]]:
    from docling_core.types.doc import SectionHeaderItem, TableItem, TextItem, TitleItem

    result = {}
    cell_refs = set().union(*(rich_cell_refs(document, table) for table in document.tables))
    previous: list[DocItem] = []
    heading_by_level: dict[int, str] = {}
    for item, _ in document.iterate_items():
        if item.self_ref in cell_refs:
            continue
        if isinstance(item, (TitleItem, SectionHeaderItem)):
            level = item.level if isinstance(item, SectionHeaderItem) else 0
            heading_by_level = {k: v for k, v in heading_by_level.items() if k < level}
            heading_by_level[level] = item.text
        if isinstance(item, TableItem):
            captions = [ref.resolve(document) for ref in item.captions]
            plain = plain_table(document, item)
            chunks = (
                table_chunks(
                    plain, [*previous, *captions], list(heading_by_level.values()), config, encoding
                )
                if plain is not None
                else None
            )
            if chunks:
                result[item.self_ref] = chunks
            previous = []  # Never borrow context across another table.
        elif isinstance(item, TextItem) and config.table_context_items:
            previous = [*previous, item][-config.table_context_items :]
    logger.info(
        "tables_contextualized",
        extra={"table_count": len(document.tables), "normalized_table_count": len(result)},
    )
    return result


def rich_cell_refs(document: "DoclingDocument", table: "TableItem") -> set[str]:
    from docling_core.types.doc import RichTableCell

    return {
        node.self_ref
        for cell in table.data.table_cells
        if isinstance(cell, RichTableCell)
        for node, _ in document.iterate_items(root=cell.ref.resolve(document), with_groups=True)
    }


def plain_table(document: "DoclingDocument", table: "TableItem") -> "TableItem | None":
    """Allow rich cells only when their stored text faithfully covers a text-only subtree."""
    from docling_core.types.doc import GroupItem, RichTableCell, TableCell, TextItem

    cells = []
    for cell in table.data.table_cells:
        if isinstance(cell, RichTableCell):
            nodes = [
                node
                for node, _ in document.iterate_items(
                    root=cell.ref.resolve(document), with_groups=True
                )
            ]
            text = " ".join(node.text for node in nodes if isinstance(node, TextItem))
            if any(not isinstance(node, (GroupItem, TextItem)) for node in nodes):
                return None
            if clean(text) != clean(cell.text):
                return None
            cells.append(TableCell.model_validate(cell.model_dump(exclude={"ref"})))
        else:
            cells.append(cell)
    return table.model_copy(update={"data": table.data.model_copy(update={"table_cells": cells})})
