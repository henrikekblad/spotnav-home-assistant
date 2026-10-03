"""Charge sessions with cost: what each charge delivered and what it cost.

* `model` -- one session, its stored shape and its public shape;
* `costing` -- the pure price maths (energy per interval x the interval's effective price);
* `summary` -- per day and per month, the savings comparison and the CSV export;
* `store` -- the installation's bounded, restart-surviving record;
* `recorder` -- one per charger: watches the charge, opens and closes sessions, accrues energy and cost;
* `inputs` -- the facts and prices a recorder reads from a live charger.
"""
