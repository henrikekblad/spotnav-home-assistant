"""The year's review ("Året med SpotNav", plan `plans/year_review_plan.md`): a short story of a site's year, worked
out once a night in December (and once on 1 January, final for the year), kept, and read by the cards and the
`spotnav.show_year_review` service.

* `compute.py`: pure, the figures and every page's omission rule.
* `store.py`: what is kept per site (the review, the per-day parts that outlive their sources, the fuse's overload
  episodes, each person's seen state).
* `runtime.py`: the shell: gathers the inputs, runs the computation in the executor, the schedule.
"""
