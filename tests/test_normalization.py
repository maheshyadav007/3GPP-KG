from __future__ import annotations

import pytest

from threegpp_kg.constants import Conclusion
from threegpp_kg.ingestion.normalize import (
    normalize_column,
    normalize_organization,
    normalize_status,
    split_multi_value,
    split_organization_sources,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, Conclusion.UNKNOWN),
        ("  NOT   pursued ", Conclusion.NOT_PURSUED),
        ("not handled", Conclusion.NOT_TREATED),
        ("agreed", Conclusion.AGREED),
        ("approved", Conclusion.APPROVED),
        ("rejected", Conclusion.REJECTED),
        ("revised", Conclusion.REVISED),
        ("withdrawn", Conclusion.WITHDRAWN),
        ("unrecognized", Conclusion.UNKNOWN),
    ],
)
def test_status_normalization_is_exact(raw: str | None, expected: Conclusion) -> None:
    assert normalize_status(raw) == expected


def test_multi_value_split_deduplicates_preserves_order_and_ignores_placeholders() -> None:
    assert split_multi_value(None) == []
    assert split_multi_value("38.331, 38.306;38.331\n- ; 38.401") == [
        "38.331",
        "38.306",
        "38.401",
    ]


def test_organization_sources_handle_office_bullets_roles_and_corporate_suffixes() -> None:
    assert split_organization_sources(
        "Huawei (pen-holder), OPPO? China Mobile? ZTE? Rakuten Mobile, Inc."
    ) == ["Huawei", "OPPO", "China Mobile", "ZTE", "Rakuten Mobile, Inc"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("TD #", "tdoc"),
        ("TDoc #", "tdoc"),
        ("TDoc Number", "tdoc"),
        ("Doc For", "for"),
        ("Result", "tdoc_status"),
        ("Rel", "release"),
        ("Specification", "spec"),
        ("Work Item", "related_wis"),
        ("WorkItem", "related_wis"),
        ("WI", "related_wis"),
        ("CR #", "cr"),
        ("Rev", "cr_revision"),
        ("Cat", "cr_category"),
        ("Agenda Description", "agenda_description"),
    ],
)
def test_column_aliases_are_canonical(raw: str, expected: str) -> None:
    assert normalize_column(raw) == expected


def test_organization_normalization_collapses_space_and_applies_casefold_alias() -> None:
    aliases = {"qualcomm incorporated": "Qualcomm"}
    assert normalize_organization("  QUALCOMM   Incorporated, ", aliases) == "Qualcomm"
    assert normalize_organization(" New   Company; ", aliases) == "New Company"
