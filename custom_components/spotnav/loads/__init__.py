"""Loads on the site other than the chargers (SpotNav 3.0): a switched load such as a water heater, run in the
cheapest hours. The policy is pure (`switch_load.py`); `runtime.py` is the thin shell. In this release the load runs
in shadow only: SpotNav logs when it would switch it and never writes to the switch (`battery/shadow.ON_REACHABLE`).
"""
