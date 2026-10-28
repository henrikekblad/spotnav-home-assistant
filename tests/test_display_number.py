"""Numbers the options form shows are rounded for display: no `28.749999999999996`."""

from __future__ import annotations

import pytest
from custom_components.spotnav.flows.labels import display_number


@pytest.mark.parametrize(
    ("value", "shown"),
    [(28.749999999999996, 28.7), (30.0, 30), (16, 16), (6.25, 6.2), (0.0, 0), (12.04, 12)],
)
def test_display_number_keeps_one_decimal_and_drops_a_zero_fraction(value, shown) -> None:
    result = display_number(value)
    assert result == shown
    assert str(result) == str(shown)

