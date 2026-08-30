from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from threegpp_kg.config import ChunkingConfig, EvidenceBlockConfig
from threegpp_kg.constants import BlockKind
from threegpp_kg.domain import DocumentBlock, RetrievalChunk
from threegpp_kg.ingestion.blocks import coalesce_evidence_blocks
from threegpp_kg.ingestion.chunking import build_chunks
from threegpp_kg.retrieval import (
    cosine_similarity,
    lexical_score,
    rank_chunks,
    reciprocal_rank_fusion,
    tokenize,
)


def test_tokenization_and_lexical_scoring_preserve_technical_identifiers() -> None:
    assert tokenize("TS 38.331, R2-2600001 and NR_DC") == [
        "ts",
        "38.331",
        "r2-2600001",
        "and",
        "nr_dc",
    ]
    assert lexical_score("carrier aggregation", "Carrier aggregation is carrier") == 5.0
    assert lexical_score("", "text") == 0
    assert lexical_score("text", "") == 0


def test_hybrid_ranking_combines_lexical_vector_and_authority() -> None:
    chunks = [
        RetrievalChunk(
            id="lexical",
            document_id="R2-1",
            block_ids=["b1"],
            text="carrier aggregation scheduling",
            token_count=4,
            embedding=[1.0, 0.0],
            evidence_ids=["report"],
        ),
        RetrievalChunk(
            id="semantic",
            document_id="R2-2",
            block_ids=["b2"],
            text="uplink control behavior",
            token_count=4,
            embedding=[0.99, 0.01],
            evidence_ids=["tdoc"],
        ),
    ]
    result = rank_chunks(
        chunks,
        "carrier aggregation",
        top_k=2,
        query_embedding=[1.0, 0.0],
        authority_weights={"report": 1.2, "tdoc": 1.0},
    )
    assert [item.chunk_id for item in result] == ["lexical", "semantic"]
    assert result[0].evidence_ids == ["report"]
    assert result[0].document_id == "R2-1"
    assert result[0].text == "carrier aggregation scheduling"
    assert result[0].block_ids == ["b1"]
    assert result[0].score == pytest.approx((1 / 61 + 1 / 61) * 1.2)


def test_rrf_rejects_invalid_inputs_and_deduplicates() -> None:
    assert reciprocal_rank_fusion([(["a", "a", "b"], 1)], 10)["a"] == pytest.approx(1 / 11)
    fused = reciprocal_rank_fusion([(["a", "b"], 2), (["b", "a"], 0.5)], 10)
    assert fused["a"] == pytest.approx(2 / 11 + 0.5 / 12)
    assert fused["b"] == pytest.approx(2 / 12 + 0.5 / 11)
    with pytest.raises(ValueError, match="positive"):
        reciprocal_rank_fusion([], 0)
    with pytest.raises(ValueError, match="negative"):
        reciprocal_rank_fusion([(["a"], -1)])


def test_cosine_similarity_validates_dimensions_and_zero_vectors() -> None:
    assert cosine_similarity([0, 0], [1, 0]) == 0
    assert cosine_similarity([1, 0], [1, 0]) == pytest.approx(1)
    assert cosine_similarity([1, 0], [-1, 0]) == pytest.approx(-1)
    with pytest.raises(ValueError, match="dimensions"):
        cosine_similarity([1], [1, 2])


def test_rank_chunks_rejects_invalid_top_k_and_returns_no_unmatched_lexical_results() -> None:
    chunk = RetrievalChunk(
        id="one",
        document_id="R2-1",
        block_ids=["b1"],
        text="carrier aggregation",
        token_count=2,
    )
    with pytest.raises(ValueError, match="positive"):
        rank_chunks([chunk], "carrier", top_k=0)
    assert rank_chunks([chunk], "unrelated", top_k=1) == []


@given(st.lists(st.text(min_size=1), min_size=1, max_size=20))
def test_chunk_ids_are_stable_and_references_are_preserved(texts: list[str]) -> None:
    blocks = [
        DocumentBlock(
            id=f"block-{index}",
            document_id="R2-1",
            index=index,
            kind=BlockKind.PARAGRAPH,
            text=text,
            section_path=["Section"],
        )
        for index, text in enumerate(texts)
    ]
    config = ChunkingConfig(min_tokens=5, target_tokens=10, max_tokens=20)
    first = build_chunks(blocks, config)
    second = build_chunks(blocks, config)
    assert [chunk.id for chunk in first] == [chunk.id for chunk in second]
    assert set(identifier for chunk in first for identifier in chunk.block_ids) == {
        block.id for block in blocks if block.text.strip()
    }
    assert all(chunk.token_count <= config.max_tokens for chunk in first)


def test_evidence_blocks_coalesce_body_elements_with_stable_bounds() -> None:
    source = [
        DocumentBlock(
            id=f"source-{index}",
            document_id="R2-1",
            index=index,
            kind=BlockKind.PARAGRAPH if index % 2 == 0 else BlockKind.LIST_ITEM,
            text=" ".join(f"word-{index}-{word}" for word in range(20)),
            section_path=["5 Mobility"],
        )
        for index in range(12)
    ]
    config = ChunkingConfig(min_tokens=50, target_tokens=100, max_tokens=150)

    first = coalesce_evidence_blocks(source, config)
    second = coalesce_evidence_blocks(source, config)

    assert len(first) < len(source)
    assert [block.id for block in first] == [block.id for block in second]
    assert all(block.section_path == ["5 Mobility"] for block in first)
    assert all(len(block.text.split()) * 1.3 <= config.max_tokens for block in first)
    assert " ".join(block.text for block in source).split() == " ".join(
        block.text for block in first
    ).split()


def test_evidence_blocks_preserve_semantic_and_section_boundaries() -> None:
    source = [
        DocumentBlock(
            id="heading",
            document_id="R2-1",
            index=0,
            kind=BlockKind.HEADING,
            text="5 Mobility",
            section_path=["5 Mobility"],
        ),
        DocumentBlock(
            id="paragraph",
            document_id="R2-1",
            index=1,
            kind=BlockKind.PARAGRAPH,
            text="Background discussion.",
            section_path=["5 Mobility"],
        ),
        DocumentBlock(
            id="agreement",
            document_id="R2-1",
            index=2,
            kind=BlockKind.AGREEMENT,
            text="Agreement: adopt option A.",
            section_path=["5 Mobility"],
        ),
        DocumentBlock(
            id="next-section",
            document_id="R2-1",
            index=3,
            kind=BlockKind.PARAGRAPH,
            text="A different section.",
            section_path=["6 Measurements"],
        ),
    ]

    blocks = coalesce_evidence_blocks(
        source, EvidenceBlockConfig(target_tokens=100, max_tokens=150)
    )

    assert [block.kind for block in blocks] == [
        BlockKind.PARAGRAPH,
        BlockKind.AGREEMENT,
        BlockKind.PARAGRAPH,
    ]
    assert blocks[1].text == "Agreement: adopt option A."
    assert blocks[2].section_path == ["6 Measurements"]
    assert all(block.kind != BlockKind.HEADING for block in blocks)


def test_large_evidence_blocks_are_split_into_smaller_retrieval_chunks() -> None:
    source = [
        DocumentBlock(
            id=f"source-{index}",
            document_id="R2-1",
            index=index,
            kind=BlockKind.PARAGRAPH,
            text=" ".join(f"word-{index}-{word}" for word in range(180)),
            section_path=["5 Mobility"],
        )
        for index in range(5)
    ]
    blocks = coalesce_evidence_blocks(
        source, EvidenceBlockConfig(target_tokens=1000, max_tokens=1400)
    )
    chunks = build_chunks(
        blocks, ChunkingConfig(min_tokens=300, target_tokens=500, max_tokens=700)
    )

    assert len(blocks) == 1
    assert len(chunks) == 2
    assert all(chunk.block_ids == [blocks[0].id] for chunk in chunks)
    assert all(chunk.token_count <= 700 for chunk in chunks)


def test_evidence_blocks_group_table_rows_without_losing_first_row_reference() -> None:
    source = [
        DocumentBlock(
            id=f"row-{index}",
            document_id="R2-1",
            index=index,
            kind=BlockKind.TABLE_ROW,
            text=f"RIL-{index} | agreed",
            section_path=["Resolutions"],
            table_row=index + 10,
        )
        for index in range(8)
    ]

    blocks = coalesce_evidence_blocks(
        source, ChunkingConfig(min_tokens=5, target_tokens=20, max_tokens=30)
    )

    assert len(blocks) < len(source)
    assert all(block.kind == BlockKind.TABLE_ROW for block in blocks)
    assert blocks[0].table_row == 10
    assert "RIL-0 | agreed\nRIL-1 | agreed" in blocks[0].text
