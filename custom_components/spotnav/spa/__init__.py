"""The spa (SpotNav 3.0, spec `plans/v3_pool_spa_spec.md`): one flexible load with bath times like departures.
`config.py` (the settings block, pure), `detect.py` (ranked detection from the registry), `plan.py` (the shadow planner,
pure). Its shadow runs in `pool/runtime.py` (`PoolSpaShadow`). Nothing here writes to a device.
"""
