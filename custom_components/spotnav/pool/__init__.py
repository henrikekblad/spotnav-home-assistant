"""The pool (SpotNav 3.0, spec `plans/v3_pool_spa_spec.md`): two nested flexible loads, the circulation pump (the day's
filtering need) and the pool heat pump (runs only inside circulation hours). `config.py` (the settings block, pure),
`detect.py` (ranked detection from the registry), `flexible.py` (the loads in the common shape), `plan.py` (the shadow
planner, pure) and `runtime.py` (`PoolSpaShadow`, one per site, the pool's and the spa's shadow). Nothing here writes to a
device: the controls come later, each behind its own opt-in.
"""
