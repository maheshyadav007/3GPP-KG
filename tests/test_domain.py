from datetime import date

import pytest
from pydantic import ValidationError

from threegpp_kg.domain import TemporalScope


def test_temporal_scope_allows_one_selector() -> None:
    assert TemporalScope(last_k_meetings=3).last_k_meetings == 3
    assert TemporalScope(duration_months=12).duration_months == 12
    assert TemporalScope(meeting_ids=["RAN2-133"]).meeting_ids == ["RAN2-133"]


def test_temporal_scope_rejects_conflicting_selectors() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        TemporalScope(last_k_meetings=3, duration_months=12)


def test_temporal_scope_rejects_reversed_dates() -> None:
    with pytest.raises(ValidationError, match="date_from"):
        TemporalScope(date_from=date(2026, 5, 2), date_to=date(2026, 5, 1))
