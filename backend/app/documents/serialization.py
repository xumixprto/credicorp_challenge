"""Exclude tables already emitted as contextual rows from prose chunking."""

from dataclasses import dataclass

from docling_core.transforms.chunker.hierarchical_chunker import ChunkingDocSerializer
from docling_core.transforms.serializer.base import BaseSerializerProvider
from docling_core.types.doc import DoclingDocument
from pydantic import Field

from backend.app.documents.tables import rich_cell_refs


class ProseSerializer(ChunkingDocSerializer):
    contextual_table_refs: set[str] = Field(default_factory=set)

    def get_excluded_refs(self, **kwargs) -> set[str]:
        return super().get_excluded_refs(**kwargs) | self.contextual_table_refs


@dataclass
class ProseSerializerProvider(BaseSerializerProvider):
    table_refs: set[str]

    def get_serializer(self, doc: DoclingDocument) -> ProseSerializer:
        refs = set(self.table_refs)
        for table in doc.tables:
            if table.self_ref in self.table_refs:
                refs.update(rich_cell_refs(doc, table))
        return ProseSerializer(doc=doc, contextual_table_refs=refs)
