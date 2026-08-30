from __future__ import annotations

import hashlib

from ..config import ChunkingConfig
from ..domain import DocumentBlock, RetrievalChunk


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text.split()) * 1.3)) if text.strip() else 0


def build_chunks(blocks: list[DocumentBlock], config: ChunkingConfig) -> list[RetrievalChunk]:
    chunks: list[RetrievalChunk] = []
    current: list[DocumentBlock] = []
    current_tokens = 0

    def flush() -> None:
        nonlocal current, current_tokens
        if not current:
            return
        section = current[-1].section_path
        text = "\n".join(block.text for block in current if block.text.strip())
        digest = hashlib.sha256("|".join(block.id for block in current).encode()).hexdigest()[:20]
        chunks.append(
            RetrievalChunk(
                id=f"chunk-{digest}",
                document_id=current[0].document_id,
                block_ids=[block.id for block in current],
                text=text,
                section_path=section,
                token_count=estimate_tokens(text),
            )
        )
        current = []
        current_tokens = 0

    for block in blocks:
        if not block.text.strip():
            continue
        block_tokens = estimate_tokens(block.text)
        if block_tokens > config.max_tokens:
            flush()
            words = block.text.split()
            max_words = max(1, int(config.max_tokens / 1.3))
            for part_number, start in enumerate(range(0, len(words), max_words), start=1):
                text = " ".join(words[start : start + max_words])
                digest = hashlib.sha256(
                    f"{block.id}|part:{part_number}|{text}".encode()
                ).hexdigest()[:20]
                chunks.append(
                    RetrievalChunk(
                        id=f"chunk-{digest}",
                        document_id=block.document_id,
                        block_ids=[block.id],
                        text=text,
                        section_path=block.section_path,
                        token_count=estimate_tokens(text),
                    )
                )
            continue
        changed_section = bool(current and block.section_path != current[-1].section_path)
        candidate_text = "\n".join(item.text for item in [*current, block] if item.text.strip())
        candidate_tokens = estimate_tokens(candidate_text)
        would_overflow = candidate_tokens > config.max_tokens
        if current and (
            would_overflow or (changed_section and current_tokens >= config.min_tokens)
        ):
            flush()
        current.append(block)
        current_tokens = estimate_tokens(
            "\n".join(item.text for item in current if item.text.strip())
        )
        if current_tokens >= config.target_tokens:
            flush()
    flush()
    return chunks
