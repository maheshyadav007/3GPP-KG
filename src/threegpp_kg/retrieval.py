from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence

from .domain import Passage, RetrievalChunk

TOKEN_PATTERN = re.compile(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*", re.IGNORECASE)


def tokenize(text: str) -> list[str]:
    return [match.group(0).lower() for match in TOKEN_PATTERN.finditer(text)]


def lexical_score(query: str, text: str) -> float:
    query_tokens = tokenize(query)
    if not query_tokens:
        return 0.0
    document_tokens = tokenize(text)
    if not document_tokens:
        return 0.0
    frequencies = Counter(document_tokens)
    unique_query = list(dict.fromkeys(query_tokens))
    matched = sum(min(frequencies[token], 3) for token in unique_query)
    coverage = sum(token in frequencies for token in unique_query) / len(unique_query)
    phrase_bonus = 1.0 if query.strip().lower() in text.lower() else 0.0
    return matched + coverage + phrase_bonus


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("vectors must have the same non-zero dimensions")
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def reciprocal_rank_fusion(
    rankings: Iterable[tuple[Sequence[str], float]], rrf_k: int = 60
) -> dict[str, float]:
    if rrf_k < 1:
        raise ValueError("rrf_k must be positive")
    fused: dict[str, float] = {}
    for identifiers, weight in rankings:
        if weight < 0:
            raise ValueError("ranking weight cannot be negative")
        seen: set[str] = set()
        for rank, identifier in enumerate(identifiers, start=1):
            if identifier in seen:
                continue
            seen.add(identifier)
            fused[identifier] = fused.get(identifier, 0.0) + weight / (rrf_k + rank)
    return fused


def rank_chunks(
    chunks: Sequence[RetrievalChunk],
    query: str,
    *,
    top_k: int,
    query_embedding: Sequence[float] | None = None,
    lexical_weight: float = 1.0,
    vector_weight: float = 1.0,
    rrf_k: int = 60,
    authority_weights: Mapping[str, float] | None = None,
) -> list[Passage]:
    if top_k < 1:
        raise ValueError("top_k must be positive")
    lexical = sorted(
        ((chunk.id, lexical_score(query, chunk.text)) for chunk in chunks),
        key=lambda item: (-item[1], item[0]),
    )
    lexical_ids = [identifier for identifier, score in lexical if score > 0]
    rankings: list[tuple[Sequence[str], float]] = [(lexical_ids, lexical_weight)]
    if query_embedding is not None:
        semantic = sorted(
            (
                (chunk.id, cosine_similarity(query_embedding, chunk.embedding))
                for chunk in chunks
                if chunk.embedding is not None
            ),
            key=lambda item: (-item[1], item[0]),
        )
        rankings.append(([identifier for identifier, _ in semantic], vector_weight))
    fused = reciprocal_rank_fusion(rankings, rrf_k)
    by_id = {chunk.id: chunk for chunk in chunks}
    authority = authority_weights or {}
    for chunk in chunks:
        if chunk.id not in fused:
            continue
        multiplier = max((authority.get(item, 1.0) for item in chunk.evidence_ids), default=1.0)
        fused[chunk.id] *= multiplier
    ordered = sorted(fused, key=lambda identifier: (-fused[identifier], identifier))[:top_k]
    return [
        Passage(
            chunk_id=identifier,
            document_id=by_id[identifier].document_id,
            text=by_id[identifier].text,
            section_path=by_id[identifier].section_path,
            block_ids=by_id[identifier].block_ids,
            score=fused[identifier],
            evidence_ids=by_id[identifier].evidence_ids,
        )
        for identifier in ordered
    ]
