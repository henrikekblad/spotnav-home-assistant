"""The pool's and the spa's hard numbers in one place (spec sections 5 and 6), read by the descriptions
(`pool/flexible.py`), the planners and, later, the controls. Pure constants. **Placeholders** until a tester's pump says
otherwise: the circulation's run-on (owner: 2 to 5 minutes), the minimum times, the starts and the write budgets.
"""

from __future__ import annotations

from typing import Final

#: The stable load ids the site's priority list refers to (one pool and one spa per site; house cooling is the zones'
#: setpoint raise in cool mode).
LOAD_POOL_HEAT: Final = "pool_heat"
LOAD_POOL_CIRCULATION: Final = "pool_circulation"
LOAD_SPA: Final = "spa"
LOAD_COOLING: Final = "cooling"

#: The heat pump goes on only after the circulation has read on this long (and flow, where a flow sensor reads).
CIRC_LEAD_S: Final = 60
#: The circulation runs on this long after the heat pump is off.
RUN_ON_MIN: Final = 5
HEAT_PUMP_MIN_ON_MIN: Final = 30
HEAT_PUMP_MIN_OFF_MIN: Final = 10
HEAT_PUMP_STARTS_DAY: Final = 6
CIRCULATION_MIN_ON_MIN: Final = 30
CIRCULATION_MIN_OFF_MIN: Final = 15
CIRCULATION_STARTS_HOUR: Final = 2
CIRCULATION_STARTS_DAY: Final = 8
#: Writes per entity per local day of a stored setting (a setpoint, Balboa's preset, a cloud write), and of on/off.
SETPOINT_WRITES_DAY: Final = 6
ON_OFF_WRITES_DAY: Final = 12
#: A change SpotNav did not make, seen this long, is the person's: SpotNav hands the load back.
FOREIGN_CONFIRM_S: Final = 60

#: House cooling: a zone in cool mode is raised at most this far over the person's setpoint, never above the cap.
COOL_RAISE_C: Final = 1.0
COOL_CEILING_MAX_C: Final = 28.0
