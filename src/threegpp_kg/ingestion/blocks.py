from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from ..config import ChunkingConfig, EvidenceBlockConfig
from ..constants import BlockKind
from ..domain import DocumentBlock
from .chunking import estimate_tokens


@dataclass(slots=True)
class _BlockGroup:
    document_id: str
    section_path: list[str]
    group_type: str
    blocks: list[DocumentBlock] = field(default_factory=list)
    token_count: int = 0


def coalesce_evidence_blocks(
    source_blocks: list[DocumentBlock],
    config: ChunkingConfig | EvidenceBlockConfig,
) -> list[DocumentBlock]:
    """Convert parser-level elements into bounded, section-aware evidence blocks."""
    result: list[DocumentBlock] = []
    current: _BlockGroup | None = None

    def flush() -> None:
        nonlocal current
        if current is None or not current.blocks:
            current = None
            return
        separator = "\n" if current.group_type == "table" else "\n\n"
        text = separator.join(block.text.strip() for block in current.blocks if block.text.strip())
        kind = _coalesced_kind(current.blocks)
        result.append(
            _evidence_block(
                current.document_id,
                len(result),
                kind,
                text,
                current.section_path,
                table_row=current.blocks[0].table_row if kind == BlockKind.TABLE_ROW else None,
            )
        )
        current = None

    for source in source_blocks:
        if not source.text.strip():
            continue
        if source.kind == BlockKind.HEADING:
            flush()
            # Heading text is represented by section_path and the section tree.
            continue
        for part in _split_oversized(source, config.max_tokens):
            part_tokens = estimate_tokens(part.text)
            group_type = _group_type(part.kind)
            changed_group = bool(
                current
                and (
                    current.document_id != part.document_id
                    or current.section_path != part.section_path
                    or current.group_type != group_type
                )
            )
            would_overflow = bool(
                current and current.token_count + part_tokens > config.max_tokens
            )
            if changed_group or would_overflow:
                flush()
            if current is None:
                current = _BlockGroup(
                    document_id=part.document_id,
                    section_path=list(part.section_path),
                    group_type=group_type,
                )
            current.blocks.append(part)
            current.token_count += part_tokens
            if current.token_count >= config.target_tokens:
                flush()
    flush()
    return result


def _split_oversized(block: DocumentBlock, max_tokens: int) -> list[DocumentBlock]:
    if estimate_tokens(block.text) <= max_tokens:
        return [block]
    words = block.text.split()
    max_words = max(1, int(max_tokens / 1.3))
    return [
        block.model_copy(update={"text": " ".join(words[start : start + max_words])})
        for start in range(0, len(words), max_words)
    ]


def _group_type(kind: BlockKind) -> str:
    if kind == BlockKind.TABLE_ROW:
        return "table"
    if kind in {
        BlockKind.DISCUSSION,
        BlockKind.AGREEMENT,
        BlockKind.CONCLUSION,
        BlockKind.NOTE,
    }:
        return kind.value
    return "body"


def _coalesced_kind(blocks: list[DocumentBlock]) -> BlockKind:
    kinds = {block.kind for block in blocks}
    if len(kinds) == 1:
        return blocks[0].kind
    return BlockKind.PARAGRAPH


def _evidence_block(
    document_id: str,
    index: int,
    kind: BlockKind,
    text: str,
    section_path: list[str],
    *,
    table_row: int | None,
) -> DocumentBlock:
    identity = f"{document_id}|{index}|{kind.value}|{'/'.join(section_path)}|{text}"
    digest = hashlib.sha256(identity.encode()).hexdigest()[:20]
    return DocumentBlock(
        id=f"block-{digest}",
        document_id=document_id,
        index=index,
        kind=kind,
        text=text,
        section_path=list(section_path),
        table_row=table_row,
    )
