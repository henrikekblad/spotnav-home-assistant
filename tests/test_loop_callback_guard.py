"""The loop-callback guard (see `loop_callback_guard` in conftest) must actually cover the integration."""

from __future__ import annotations

import ast
from pathlib import Path

from homeassistant.core import HomeAssistant

from tests.conftest import _GUARDED_HELPERS

PACKAGE = Path(__file__).parent.parent / "custom_components" / "spotnav"


def test_every_timer_and_listener_helper_the_integration_uses_is_guarded() -> None:
    """A new `async_track_*` / `async_call_*` helper in use must be added to the guard's list."""
    used: set[str] = set()
    for path in PACKAGE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("homeassistant.helpers.event"):
                used.update(a.name for a in node.names if a.name.startswith(("async_track_", "async_call_")))
            elif isinstance(node, ast.Attribute) and node.attr.startswith(("async_track_", "async_call_")):
                used.add(node.attr)
    assert used, "the scan found nothing: the test is broken"
    assert used <= set(_GUARDED_HELPERS), sorted(used - set(_GUARDED_HELPERS))


def test_the_guard_fails_an_unmarked_function(hass: HomeAssistant, loop_callback_guard) -> None:
    """The guard is live: a plain function is recorded as a violation (and then cleared here)."""
    from custom_components.spotnav.pricing import market_observation

    def plain(_now):
        pass

    cancel = market_observation.async_call_later(hass, 60, plain)
    cancel()
    # Reach the fixture's violation list through the wrapper's closure.
    violations = market_observation.async_call_later.__closure__
    lists = [c.cell_contents for c in violations if isinstance(c.cell_contents, list)]
    assert lists and lists[0], "an unmarked function must be recorded"
    lists[0].clear()
