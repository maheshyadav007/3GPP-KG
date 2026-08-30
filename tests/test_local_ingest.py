from __future__ import annotations

from threegpp_kg.local_ingest import _select_document_entries


def test_document_entry_selection_prefers_docs_and_preserves_source_aliases() -> None:
    agenda = {
        "filename": "R2-1.zip",
        "url": "https://www.3gpp.org/meeting/Agenda/R2-1.zip",
    }
    body = {
        "filename": "R2-1.zip",
        "url": "https://www.3gpp.org/meeting/Docs/R2-1.zip",
    }

    selected, duplicates = _select_document_entries([agenda, body])

    assert selected == {"R2-1": body}
    assert duplicates == [agenda]
