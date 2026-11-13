"""The home battery (SpotNav 3.0): what it is and what SpotNav would do with it.

Every policy here is pure (no Home Assistant import); `runtime.py` is the thin shell that reads the registry and
the states and keeps the log. `plan.py` plans the battery over the price horizon (charge, hold or discharge per
slot). In this release nothing writes to a battery or an inverter: each part runs in shadow
and only logs what it would have written.
"""
