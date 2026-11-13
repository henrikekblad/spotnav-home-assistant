"""The home battery (SpotNav 3.0): what it is and what SpotNav would do with it.

Every policy here is pure (no Home Assistant import); `runtime.py` is the thin shell that reads the registry and
the states and keeps the log. `plan.py` plans the battery over the price horizon (charge, hold or discharge per
slot). In this release nothing writes to a battery or an inverter: each part runs in shadow
and only logs what it would have written.

The write path a later, explicit opt-in will use is built but unreachable: `commands.py` (what each brand would be
sent per planned action), `executor.py` (the edge-triggered, read-back writes and the hand-back on every exit path),
`verify.py` (the per-mode verification routine) and `control.py` (the shell). Nothing constructs or imports them, and
the shell refuses to act while `shadow.ON_REACHABLE` is off.
"""
