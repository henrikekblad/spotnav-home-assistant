"""The charge-ownership core: who owns a charger's charge and which person intent holds, as pure data and one
pure decision (`ownership.decide`). No Home Assistant imports anywhere in this package.

The controller and the execution boundary feed it every happening and act on what it decides, through the runner
(`execution/session_runner.py`); nothing here acts on a charger itself. See docs/architecture-state.md.
"""
