from __future__ import annotations

import re
from collections.abc import Mapping

from ..constants import STATUS_ALIASES, Conclusion


def normalize_status(value: str | None) -> Conclusion:
    normalized = re.sub(r"\s+", " ", (value or "").strip().lower())
    return STATUS_ALIASES.get(normalized, Conclusion.UNKNOWN)


def split_multi_value(value: str | None) -> list[str]:
    if not value:
        return []
    parts = re.split(r"[,;\n]+", value)
    return list(
        dict.fromkeys(part.strip() for part in parts if part.strip() and part.strip() != "-")
    )


def split_organization_sources(value: str | None) -> list[str]:
    if not value:
        return []
    # Some 3GPP exports render Office list bullets as '?' characters.
    normalized = re.sub(r"[\u2022\u00b7\uf0b7?]+", ",", value)
    raw_parts = re.split(r"[,;\n]+", normalized)
    organizations: list[str] = []
    corporate_suffixes = {"inc", "inc.", "ltd", "ltd.", "llc", "n.v.", "b.v."}
    for raw_part in raw_parts:
        part = re.sub(r"(?i)\s*\(pen[- ]holder\)\s*", "", raw_part)
        part = part.strip(" []()\t\n.")
        if not part or part == "-":
            continue
        if organizations and part.casefold() in corporate_suffixes:
            organizations[-1] = f"{organizations[-1]}, {part}"
        else:
            organizations.append(part)
    return list(dict.fromkeys(organizations))


def normalize_column(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", value.strip().lower())
    aliases = {
        "td_": "tdoc",
        "tdoc_": "tdoc",
        "tdoc_number": "tdoc",
        "doc_for": "for",
        "result": "tdoc_status",
        "rel": "release",
        "specification": "spec",
        "work_item": "related_wis",
        "workitem": "related_wis",
        "wi": "related_wis",
        "cr_": "cr",
        "rev": "cr_revision",
        "cat": "cr_category",
    }
    return aliases.get(value, value).strip("_")


def normalize_organization(value: str, aliases: Mapping[str, str]) -> str:
    normalized = re.sub(r"\s+", " ", value).strip(" ,;\t\n")
    return aliases.get(normalized.casefold(), normalized)
