"""The home battery (SpotNav 3.0): what it is and what SpotNav would do with it.

Every policy here is pure (no Home Assistant import); `runtime.py` is the thin shell that reads the registry and
the states and keeps the log. `plan.py` plans the battery over the price horizon (charge, hold or discharge per
slot), in shadow.

The write path: `commands.py` (what each brand is sent per planned action), `executor.py` (the edge-triggered,
read-back writes and the hand-back on every exit path), `verify.py` (the per-mode verification routine), `gate.py`
(the site's options, the report per firmware, what control may do) and `control.py` (the shell, one per site). It
writes only behind the site's gate: the person's opt-in, "shadow only" off (on by default) and a passed verification
for the plant's firmware; until then it logs what it would do.
"""
