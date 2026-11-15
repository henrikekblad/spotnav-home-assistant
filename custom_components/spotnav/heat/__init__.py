"""Heat pumps (SpotNav 3.0 heat, plan `plans/v3_heating_plan.md`): what they read and which controls they offer.

Read only: `adapters.py` (where each brand's integration keeps its readings and controls), `facts.py` (the pure
reading of one pump), `detect.py` (the registry shell, found by the integration's presence). Nothing here writes to
a heat pump or anything else.
"""
