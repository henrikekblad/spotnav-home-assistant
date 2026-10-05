"""The charge-ownership core: who owns a charger's charge and which person intent holds, as pure data and one
pure decision (`ownership.decide`). No Home Assistant imports anywhere in this package.

Step 1 of the state-machine refactor runs it in shadow mode beside today's code
(`execution/ownership_shadow.py`); nothing here acts on a charger. See docs/architecture-state.md.
"""
