# Site and load balancing

For anyone with more than one charger, or a charger that must stay within the main fuse: how to add a site, what it measures and how load balancing acts.

A **site** is an optional second kind of SpotNav entry. It represents one electrical connection,
typically your main fuse, shared by one or more SpotNav chargers. It is what makes load
balancing, [solar charging and hybrid](strategies.md) possible. A charger can belong to only
one site.

## Add a site

**Settings, Devices & services, Add integration, SpotNav, A site (load balancing).** The dialogs are
shown in [Set up SpotNav](setup.md#2-add-a-site-optional). You provide:

- **Main fuse rating (A).** Always entered by you; it is never guessed from measured load.
- **Safety margin (A)** kept below the fuse rating. It starts at 1 A. It can also be changed in the
  card, in the site dialog next to the main fuse; it must stay below the fuse.
- **Measurement source** for the site's per-phase current:
  - *Direct*: a sensor per phase reporting current, or one entity carrying every phase as
    attributes (you name the attribute for each phase and its unit, A or mA). The flow suggests
    candidates from entities already in Home Assistant; nothing is applied until you confirm.
  - *Derived*: current computed from each phase's active power and voltage, with the meter's own
    current, apparent power or reactive power when it has them. Without any of those the current
    is estimated, which is marked as such.
- **Chargers on this site**, and for each charger whether it is three-phase or single-phase, and for
  a single-phase charger its phase: L1, L2, L3 or **Automatic (follows the charger)**, the default.
  Automatic is for a charger whose phase is not fixed, such as one behind an Easee Equalizer, which
  moves a one-phase charge to another phase from one session to the next:
  - While the charger draws, its own measured current shows the phase: every phase reads, fresh, and
    exactly one carries 1 A or more, which the site's reading of that phase bears out (at least the
    charger's draw, less 2 A). Load balancing reckons it on that phase and follows it when it moves. A
    new phase counts once it has held for 10 s, and a reading in transition (two phases drawing) keeps
    the phase it had for at most that long.
  - Otherwise (idle, before a start, no measured current set, a stale or missing reading, a source that
    reads one phase only, or a reading the site does not bear out) it is reckoned on the phase with the
    least headroom, drawing the largest current any phase of its own reads: never on a guess.
  - The site's diagnostics show, per charger, the phase it is reckoned on and how that was decided
    (`charger_phase`: `chosen`, `measured` or `worst_case`). Only when some phase's headroom is unknown
    can it not be reckoned at all (`single_phase_charger_wiring_unknown`); the site then proposes
    nothing for any charger anyway.
- Optionally, each charger's own **measured current** (its per-phase sensors). Never use a
  commanded current here: a car can draw less than its setpoint, and crediting a setpoint would
  overstate how much of the site's load is the charger's.
  - **Easee** reports the charger's input terminals, not phases, as the attributes
    `state_inCurrentT2` to `state_inCurrentT5` of its **Current** sensor (disabled by default in
    the Easee integration: enable it). On a TN network (400 V between phases) T3, T4 and T5 are
    L1, L2 and L3 and T2 is the neutral; on an IT network (230 V between phases) there is no
    neutral and T2, T3 and T4 are L1, L2 and L3. SpotNav fills this in when a charger joins a
    site, and for a charger already in a site the next time the site starts (once, logged),
    when the wiring has no source; a source you chose, or removed on purpose, is never changed.
    A one-phase charger on a chosen phase is read from T3 only (TN); an Automatic one from T3, T4
    and T5, which show the phase it charges on. The charger's dialog shows the source the site
    reads, read-only.
  - **A charger whose integration has its own current sensors** (Peblar's *Current phase 1–3*, and
    likewise Zaptec, go-e, NRGkick, Charge Amps, Lektrico, Tesla Wall Connector, NexBlue and
    Easee's *Current*) is offered them in the card's site editor (**Site, Change site entities**,
    under *Found in Home Assistant*) while its wiring reads no measured current, also when the
    integration ships them disabled, as Peblar does. **Use** enables the ones the integration
    disabled and sets them in the charger's wiring; nothing changes before. A sensor you disabled
    yourself is never enabled, and the charger is then offered nothing. While the wiring reads no
    current, the status line and the charger's dialog name these sensors.
  - Without any measured current for a charger, the regulator holds (`charger_measurement_unusable`)
    and solar runs that charger blind, at the minimum current only. It starts (reason
    `unmeasured_start`, after `unmeasured_arming`) when the spare power without the car (export, plus
    a charging battery under car first) has covered the start minimum for the solar start delay, no
    other charger on the site is about to start or started less than a minute ago, and the fuse caps
    allow it. A running charge is held at the minimum current while there is export or about zero
    import (100 W per phase; a battery discharging into the car counts as import), and stopped once
    import beyond that has lasted the solar stop delay; the reason is `charger_measurement_missing`.
    The status says the charger's own current is not set (`solar_charger_current_missing`).
- On a direct site whose phase measurement is incomplete (a phase with no value, such as an inverter's
  sensor in standby), solar still runs on the total grid power. A phase that reads keeps its own fuse
  headroom; a phase that cannot be read caps the charger at what it draws there now, or the minimum
  current if that is more, even while the total exports: the total is netted over the phases, and a
  three-phase inverter's export says nothing about one phase's import. Current never rises on a phase
  that cannot be read.
- An Automatic single-phase charger is reckoned by solar on the phase its measured current shows it
  on, as above. While that is not known it is reckoned on a stand-in phase: the total grid power does
  not depend on the phase, the fuse cap is the lowest of all three phases' caps, and its draw the
  largest phase its measured current reads.

After the basics, SpotNav looks for your grid meter in the entity registry (see
[Meter detection](#meter-detection-signs-and-estimated-current)). When it finds the meter and
everything else it needs, it shows a **Site found** summary to confirm; otherwise it shows the
forms above. A new site is created with load balancing off. A site always calculates once it has a
measurement: the calculation only reads, and solar's per-phase fuse caps and the view of what load
balancing would do depend on it. (Earlier versions had a switch for the calculation and settings for load
balancing's deadband and dwell and yield stepping's ceiling. They are gone: a site stored with the
calculation off calculates all the same, and the others are fixed at their former defaults, whatever a site
had stored.)

A charger added later is offered to the site when exactly one site exists, if SpotNav can tell how
the charger is wired (see [Set up SpotNav](setup.md#add-the-charger-to-an-existing-site)).

Everything can be changed later under the site's **Configure** and, for entities, in the card's
settings popover. In **Configure** the first page, **Site settings**, holds the basics, load balancing
and solar, each field with a line on what it does, and ends with the switch **Next: change meter and
phases**. Left off, saving keeps the stored measurement and wiring; the measurement step opens anyway
when the measurement mode changed, when chargers were added or removed, or when the measurement is
incomplete. A site with nothing configured yet reports "not configured" and does
nothing.

## What the site calculates

For every phase the site works out what is left for chargers:

    headroom = main fuse - safety margin - other load

where *other load* is the site's measured total minus the chargers' own measured current on
that phase. Each charger gets a proposed current from the headroom on its phases, in one pass,
between the charger's minimum (6 A by default) and its own limits.

Measurements older than the *maximum measurement age* (default 120 s) are not trusted. On a site that
derives its current from power, a phase's active power that has not changed for longer than that
still counts as fresh (a still value, not a lost one) while Home Assistant keeps hearing from it and
the same phase's voltage, or the reactive or apparent power the site reads for it, changed within the
maximum age: some meters (a Sigenergy with the grid held at about 0 W by its battery) leave a phase's
power unchanged for minutes while the meter is live. The last value is kept, and it is trusted for
holding and lowering a charger only, never for raising one: while a phase the charger draws on sits
still, load balancing keeps the current last written to the charger rather than raise it
(`held_still_no_raise` in the diagnostics; never the lower current a car that has just started still
draws, so a start is never paused for it), and no yield step or battery probe raises it either; nor
does a still value widen the room an increase may take. A charge load balancing paused is resumed as
ever, at its minimum when the measured current leaves room. Once the phase's power changes again, raises are judged as ever.
A phase whose power is unavailable or unknown, whose age Home Assistant cannot tell, or whose other
readings sit still too, is stale as before. The diagnostics
show such a phase under `phase_signed_active_power_held_still` (`held_still`, with the companion
reading and its age; the entity is the one the site's settings name for that phase). And if
the chargers together claim more current than the site's total (beyond a small tolerance) the
whole result is reported as invalid rather than corrected silently. Results are exposed as
sensors and in [diagnostics](troubleshooting.md):

- **Capacity state** on the site: **Measuring** when the site's measurement is healthy, otherwise why
  not (**Not set up**, **Measurement missing**, **Measurement stale**, **Measurement invalid**). Its **Activity** attribute (`site_activity`) says it in a word: **Measuring, load
  balancing off**, **Measuring, load balancing on** or **Not measuring**.
- **Proposed current** per charger, with the charger's share in its **State** attribute: **Fits**,
  **Limited** (lowered to the headroom), **Below the minimum current** (the headroom is too small for the
  charger's minimum), **Not charging** (`not_requesting`: the charger asks for no current, so there is
  nothing to fit), **Requested current unknown** or **Measurement invalid**.

## Active load balancing

By default the site only calculates. Turn on **Load balancing** (the site's **Configure**, or in the
card by an administrator) to let SpotNav lower a charger's current to keep the site under its fuse.
It is best effort and is not a protective device. Turning it off gives back any current it had lowered.

- A sudden overload is reduced immediately. Other changes are damped: a change smaller than the
  *deadband* (2 A) is ignored, and a new level must hold for the *dwell* time (60 s) before it
  is written, so the charger does not dither. Both are fixed; they are not settings.
- **An import overload is always lowered.** When a phase imports more than the limit, the lowering
  goes out in the same pass, as a lower current (a pause when the room left is below the charger's
  minimum) or, if the charger cannot take it in time, a safety stop. Nothing that is not about safety holds it back: not the damper, not yield stepping's settle
  time or holds, not the step guard below, not the band of a battery that holds the grid at the fuse,
  not a resume or a battery probe about to start, not a meter that reports too seldom and not a charger
  that more than one site claims (less current is safe whoever owns it). What still decides: the fresh
  readings must still show the overload and its direction (never a lowering on export; where the direction
  cannot be known, an overload by the measured current, see below), a battery probe already running judges and stops the car itself, and a charger that is
  already at or below the lower current is not written again. The one exception is a home battery whose
  yield is verified on this site (see yield stepping below): up to the yield ceiling, yield stepping
  judges that overload as before.
- **A lost measurement steps the car to its minimum.** While the site's measurement is stale, missing or
  unusable, load balancing decides nothing and writes nothing on it: the site's status says **Measurement
  stale** or **Measurement missing**. Once the measurement has been lost for 60 seconds on a phase a charger
  may draw on (on top of the maximum measurement age, so a stale reading counts after some three minutes
  and an unavailable one after one), every charger load balancing governs that draws above its minimum
  current is set to that minimum, at once and through the charger's usual write. It is not paused or
  stopped (a meter that is silent does not show an overload), nothing is started, and a charger load
  balancing does not govern is not touched. The card says "The site's meter has no fresh reading: load
  balancing holds the car at its minimum of 6 A until it is back." A reading that is merely unchanged
  while Home Assistant keeps hearing from the sensor is not a lost one. When the measurement is back the
  regulator works as before from its next pass, and raises the car by its usual damped steps, never
  straight back to the old current. Like any other decision, this one waits while a meter reports too
  seldom or a charger is claimed by more than one site. The site's diagnostics count the times it
  happened, with the last (`measurement_lost`).
- **A step up stops one amp short of the room there is**, so the car settles one step below the
  limit (the fuse less the site's safety margin, which may be 0) instead of landing on it and being
  cut. A charger's own current is read far less often than the site meter (OCPP: about once a
  minute), so right after a change the site already shows the new current while the charger still
  reports the old one. A lowering on such a reading is held while no phase is over the limit; a
  phase a little over it (up to 2 A) takes off only what it is over and one amp more, from what the
  charger was set to. One site reading gives at most one cut, and a level that went over right after
  a step is not tried again for ten minutes. A phase more than 2 A over the limit is cut at once on
  the charger's own reading, as before. An import overload is never held by these rules; it may only
  take off less than the regulator asked when the step it overshot is still in flight.
- A start never gives the car more than the site has room for now, and a start on its way holds its
  share of that room until the charger draws it (at most two minutes): two chargers starting in the
  same moment share the margin instead of both taking all of it. A charger whose own current cannot be
  read holds its whole share for those two minutes even if the car draws less, which may keep another
  start lower for that long: counting a start twice is the safe side. A charger that fails or is slow
  to answer (a cloud service) does not keep the others from being lowered: each charger's command runs
  on its own, one at a time per charger, and a lower current decided meanwhile goes out as soon as the
  one on its way returns. A stop waits for a current change already on its way, so the change cannot
  undo it.
- **Every other current SpotNav writes is held to the same room and told to load balancing**: the
  current an Easee is sent again after its status blips or a car is plugged in, an OCPP session limit
  written once the session exists, and the sun's own current. Such a write goes out at no more than
  the site has room for now (never below the charger's minimum, since writing nothing would leave the
  charger on the limit it cleared), and load balancing takes it as what the charger is at, so the
  next overload lowers it from there. As a last line, an overload is also measured against the
  charger's own current when a fresh reading shows it a step above what was written: a charger that
  dropped its limit by itself is lowered all the same.
- **Right after a start** a charger's own current often reads nothing for a while (OCPP meter values
  come every so often) while the site meter already shows the car. For up to 90 seconds after a start
  (SpotNav's own, or the charger seen charging), and only while the charger's own reading still shows
  it drawing nothing, the car is credited with what the site's current rose by since the start, never
  more than the charger was given. So the car is not taken for house load and paused by its own start.
  The site's current itself is never changed: a phase over the fuse with the credit applied is still
  lowered, or paused, at once. The credit only keeps a charger from being paused or lowered by its own
  start; it never raises a charger: a decision that would go more than one amp above what is credited
  is taken on the charger's own reading instead (a rise that may be the house's is not the car's).
  Chargers started together share one rise: each start, in the order they went out, is credited only
  with what the other chargers' readings and earlier credits do not already account for. What another
  start may take is still judged on the chargers' own readings. Once the charger's own reading shows
  the car, it counts alone. A start the charger has not answered yet asks for its current, not for
  nothing. The credit is in the decision log (`start_credit_a`) and the site's diagnostics.
- **A charge load balancing paused** (no room for the car's minimum current) is started again once
  every phase it uses has room for the minimum and one amp more for the dwell time (60 s), while the
  charge is still wanted: the plan's window is open, the sun's charge or a person's Start. It goes on as
  the charge it was (a plan's charge stays the plan's). The log says a pause once, as information
  ("pausing the charge; load balancing leaves less than the charger's minimum current"), however many
  passes of the regulator or a failed battery probe ask for it before the charger is seen off; a safety
  stop (a lower current the charger would not take) stays a warning. A stopped charger is not paused again, so the
  charge it holds back is not forgotten. A person's Stop, the window's end or a new plan with no window
  open ends it, also while the charger has not yet answered the pause: a pause repeated then holds back
  nothing. At most two resumes go out in ten minutes; one more due in them waits 15 minutes, so a load
  that comes and goes does not cycle the relays (the charger is paused at most three times in ten
  minutes, as Peblar allows). The site sensor and the diagnostics show it (`balancing_resume`). A
  stopped charger's own current is not needed for it: an OCPP charger sends no meter values while it
  is stopped, so its reading grows old, but a stopped charger draws nothing. Inside the plan's window
  the status says so: "Paused – the house is using the whole fuse; trying again at 04:30" (or "when
  there is room" when no time is set, and "the home battery charges from the grid and fills the main
  fuse" when that is why).
- **A start while the home battery charges from the grid** (yield stepping on, *car first*, a *home
  battery power* sensor that charges at 300 W or more, or rose by that much in the last minute) gives
  the car its minimum current, not the whole request, and so does the regulator's first word on a
  start whose car does not draw yet; yield stepping raises it on what the battery gives up.
- **One reading lowers the car once.** A pass on a site reading taken before the regulator's last write
  does not credit the car with that write's lower current, so the same excess does not lower the car
  again on every pass; the next reading that is still over lowers it again at once.
- A stop already sent is not sent again by another path for the same decision (a re-arm right after
  the last window's end, the regulator's next pass) for 15 seconds, unless the charger reports charging
  again or SpotNav starts it meanwhile: a charger that has ended the transaction rejects a second stop.
  A safety stop for the fuse and a person's Stop always go out. A stop under a person's Stop that sent
  nothing for this reason does not count toward giving up on a charger that ignores the Stop.
- A person's charge that load balancing stopped for safety (the current could not be lowered in time)
  is started again when there is room, no sooner than five minutes after that stop, and only within
  the same plug-in.
- It requires a site with usable measurement and a charger SpotNav can command: one whose
  current SpotNav is set to change (an OCPP charger set through ChangeConfiguration, or a charger
  with a current number or service, see [supported hardware](supported.md)), or one it can only
  switch on and off (below). A charger whose
  current is stored in the charger cannot be adjusted during a charge, but can be stopped when the
  fuse needs less than its setting. It is refused while a charger belongs to more than one site, and
  held while a meter it reads reports too seldom; an import overload is still lowered then.
- **It needs to know which way each phase flows** (import or export). Lowering a car lowers the import,
  which on a phase that nets to export raises the current instead, so a lowering is only allowed where
  the phase is known to import. A *derived* site knows this from its signed power on every phase. A
  *direct* site knows it only when its currents carry a sign (*The grid current is signed*) **and** it
  has the meter's **total grid power**: the sign is trusted on a phase only after that phase has agreed
  with the total on three readings in a row where the answer is clear (the total and the phase outside
  ±500 W, every phase outside it on the total's side, and the phases adding up to the total). Not every
  meter counts export as negative, so the sign is checked rather than assumed; a reading against the
  total withdraws it again, and after a restart it is checked again (on a solar site, where the phases
  often point different ways in the daytime, that can take until a reading where they agree, such as
  the evening). A charger on one chosen phase, or an *Automatic* one followed to its phase, needs that
  phase; a three-phase charger, or an Automatic one not yet followed, needs all three. Elsewhere the
  regulator never raises the charger (`phase_direction_unknown` in the diagnostics) and the sun,
  if it runs the charge, writes its current itself. **An overload is still lowered there**, whoever
  started the charge (a plan, a person, the sun): a phase the charger draws on that goes over the limit
  by its measured current lowers it at once to the room that leaves (a pause below its minimum; a
  charger that can only be switched is paused), and a charge paused so is started again as any
  balancing pause is, at no more than the room there is. Every start and every current SpotNav writes
  is held to that room as on any site. The one case this does worse is a phase over the limit by
  export, which the car's draw was reducing: such a site cannot tell it from import. A phase known to
  export (a signed site once confirmed) is never lowered on, one known to import still is, and a charger read seldom
  (a cloud) is reckoned at no more than was last written to it until its reading catches up, so a
  lowered car is not given its old current back. Slow meters and doubtful membership never hold this
  lowering, as for an import overload. A direct site
  without both *The grid current is signed* and the total grid power never knows the direction: the
  load-balancing row in the card's settings then says that it only protects the fuse and does not use
  all the room there is, and what to set (the reason `site_direction_unknown` in the site's state and diagnostics). If the site editor
  lists your meter under *Found in Home Assistant*, **Use** sets both; nothing changes until you press
  it. The line goes by itself once both are set. Who writes is settled when a charge starts and kept
  until it ends (the charger stops or the car is unplugged): a charge that started before its phases were
  confirmed stays the sun's (`phase_direction_unknown_at_charge_start`), and one that started with active
  load balancing keeps it if the confirmation is withdrawn meanwhile: the current is never raised then, and
  a phase it draws on that goes over the limit by its measured current still lowers it at once to the room
  that leaves (as the sun's own capped write did), until the confirmation is back or the charge ends. Room
  the sun's write gave its charger, or a raise active control gave one of its own, is not given to the
  other writer until the charger draws it (at most two minutes). On a direct site a raise is judged on the measured
  current as it is (export on a phase is not counted as room), and a lowering also stays within the
  room the phase has, since a current with a sign does not tell how much of it is active power.
- **Yield-verified stepping** (**Car may take the home battery's share** in the site's settings, off by
  default) is for sites with a load that gives way when the car
  draws more, for example a house battery holding the grid at its setpoint. SpotNav then judges a
  step by whether it moves the site current, within a hard ceiling. Until the battery's yield is
  verified on the site, the ceiling is the main fuse itself: no step or probe takes a phase above the
  fuse on a battery that has not shown it gives way, and an import overload is lowered at once. Verified
  means the battery probe below succeeded, or a step was confirmed and the battery's own charge fell by
  what the car took; a failed step (its backoff) or the end of the car's draw forgets it. With the battery
  verified, the *yield ceiling* holds (1.15 times the fuse, fixed: below the 1.25 times a gG fuse carries
  for an hour), and an import overload below it is yield stepping's to judge.
  - **A home battery that charges from the grid and gives way.** With a *home battery power*
    sensor and solar priority *car first* (the default), SpotNav also checks that the battery's
    own charge falls when the car takes more. Once that is verified the car may climb in larger
    steps (up to 3 A, never more than the battery's charge per phase, and 1 A near the plan's
    current), the damper's dwell does not hold such a step, and the card says why the car is below
    its plan: "The home battery charges from the grid and shares the main fuse: the car gets 11 A."
    With *battery first*, or a battery that does not give way, nothing is credited and the steps
    stay at 2 A.
  - **A battery that holds the grid at the fuse.** Only with *car first* and yield stepping on. While
    the battery charges at least as hard as the car's minimum current would draw and every phase
    reads no more than 0.5 A above the limit (the fuse less the safety margin), the grid sitting at
    the limit is the battery's own regulation, and a pause or a lowering that is not an import
    overload is not carried out for it (`held_battery_at_limit` in the decision log). An import
    overload in the band is held the same way while the battery charges at least what the cut would free (below);
    a reading above that
    band, a stale measurement or a sustained excess is handled exactly as before. When load balancing has paused
    the car for want of headroom and the battery is charging from the grid, SpotNav starts the car
    at its minimum current as a probe and watches the grid for up to 30 seconds (the damper's dwell
    if shorter): if every phase is back within the band the battery gave way and the car goes on
    from there; if not, the car is stopped again and no new probe is tried for 10 minutes, then 15
    minutes each time, for as long as the charge is wanted (the plan's window open, a person's Start).
    A grid held a few tenths over the fuse within the band is no reason to skip the probe: that is the
    battery's own regulation. A car that has visibly started but does not yet draw its minimum when the window
    ends (the charger's status has turned to charging since the start, or its own current has begun
    to rise) is waited for up to 90 seconds from the start, as long as a start is credited: cars ramp
    up over tens of seconds and OCPP meter values lag. A status that already said charging when the
    probe started (even after a moment of being unavailable), or a reading the charger simply has not
    sent again, is no such sign. That wait
    holds only while every phase stays within the band; a reading above it ends the probe at once. A
    car that shows nothing by the end of the window is stopped then, as before. A probe is never started against a
    real overload, and it ends at once on a stale or missing measurement or a phase far above what
    the car's minimum can explain. A charge a person stopped is never restarted this way. A car
    that refuses two probes in a row in the plan's charge (each ends as `car_not_drawing`, the charger's
    own current at most 1 A all along, never rising, its status never charging and its connector at
    `SuspendedEV` at the end; at most 30 minutes apart, in one plug-in) has ended the charge by itself,
    exactly as if the plan's own five-minute watch had seen it ([target level](target-soc.md)): the plan
    waits for the car's level, and no probe, no resume of the paused charge and no planned period starts
    the car again until that wait ends. One probe looks at the car for half a minute, which a car waking
    from sleep may need whole, hence two. A probe that fails because the battery did not yield, a missing
    reading, or a charger that holds the car back itself (`SuspendedEVSE`) says nothing about the car.
    (On 14 October a full car was started at the minimum every 10 to 15 minutes for over an hour while
    the home battery charged from the grid.) The outcome is in the site's diagnostics under
    `battery_probe` (with `car_refusals`, the refusals in a row) and in the decision log (`probe_started`,
    `probe_extended`, `probe_succeeded`, `probe_failed`, `probe_car_ended`).
    A running car that such a battery would have lowered or paused for an import overload within the band (or a
    grid read at exactly the fuse) keeps its current, as long as the battery charges, on a fresh reading, at least as
    hard as the cut would free (all of the car's minimum for a pause): `held_battery_at_limit` in the decision log.
    This verifies nothing: the fuse stays the ceiling of every step until a step or a probe of a paused charge has
    seen the battery give way, so the car is not stepped back up into the battery's share meanwhile. An overload
    above the band is lowered at once, as before, and the car is held from where that leaves it; a request lowered
    below the car's current is always followed. For the resume and the probe a stopped charger's own current read before
    its pause, or not reported for longer than the site's maximum age, counts as nothing drawn: an OCPP
    charger sends no meter values while stopped and keeps showing the car's last draw. (On 1 November a
    Sigenergy battery held a 20 A fuse at 20.0-20.1 A from 03:15 and grew to 12.5 kW as the car was lowered
    to its minimum and paused; the HALO kept showing 6 A, and the paused charge was not resumed when the
    battery stopped at 04:07. Now the car is lowered once, from 16 to 14 A for the 21.6 A at the battery's
    start, keeps 14 A until the battery is done, and is back at 16 A within three minutes of that.)
  - **A running car below its plan while the battery fills the fuse.** Only with *car first* and yield
    stepping on. While the car draws what it was given and the battery charges from the grid by at
    least a 2 A step's worth over the car's phases (2 A x 230 V x 3 = 1380 W on three phases, on a
    fresh reading), the car is not lowered for the grid sitting at the limit, and it is stepped up
    2 A into the battery's share even though the fuse has no headroom left. Such a step may take a
    phase over the limit by at most 2.5 A (the 0.5 A band plus the step, and never past the ceiling,
    which is the main fuse until the battery's yield is verified): a step that would go further is not
    tried. It is then measured like any other yield
    step: if the battery's charge falls by what the car took, the grid stays put and the car goes
    on, one step at a time (each sized to stay within the 2.5 A, and paced by the settle time) up to
    its plan. If the grid carries the step instead, or a phase goes past the 2.5 A, or the charger's
    own reading has not shown the step within 90 seconds, the car goes straight back to where it was
    and no step is tried for 10 minutes, then 20, 40 and at most an hour. Anything above the
    allowance is still cut at once. The card keeps saying that the battery shares the fuse, with the
    current the car has now. (On 17 October a car sat at 7 A all night against a plan of 16 A while
    the battery charged at 7.3 kW from a 20 A fuse held at 20.0-20.5 A. With the grid above the fuse
    and the battery not yet verified, the car now keeps its 7 A within the band; it climbs once a battery
    probe or a confirmed step has verified the battery.)
  - **A short overload is not a reset.** While yield stepping is verified, one sample above the
    ceiling (and at most 4 A above it) steps the car back once instead of to the minimum. A second
    sample in a row, a larger excess, or two such samples within two minutes still stops at the
    safe level, and the measured per-phase current stays the fuse protection throughout.
- **Decision log.** Every change the regulator makes or holds (time, from and to amps, the reason,
  the limiting phase, the battery power and the measured currents) is kept in a bounded list per
  site, the last 200, shown as `regulator_decision_log` in the site's diagnostics. What solar and
  hybrid decide is beside it, per charger, as `solar_decision_log`. The debug bundle keeps both per
  charger plug-in instead, also across restarts (`sessions`, see
  [Diagnostics and troubleshooting](troubleshooting.md)).
- Reading a measurement that has not changed is told apart from a dead link before the reading
  is treated as stale.

### Chargers that can only be switched on and off

A charger whose current SpotNav cannot set (Monta, myenergi zappi, Ohme, go-e legacy, ChargePoint, NexBlue,
a charger behind a smart plug, or any charger whose current control is not turned on) draws a
current of its own. Load balancing reckons it as one load of that size, its **switched current**, and
pauses or resumes it whole. There is no setting for it; SpotNav learns it, from the first of:

1. **Its own measurement**: the charger's current sensors (the highest phase), or a smart plug's power
   divided by its phases times 230 V, read while it charges and the car draws at least 2 A. A rise is
   taken quickly and a fall slowly, so a car tapering off at the end of a charge does not make the next
   charge look small.
2. **The site meter's step**, for a charger with neither: when SpotNav switches it on or off, the change
   in the site's phase currents on the phases it is wired to, less what the other chargers' own readings
   changed by, from the reading just before the command to the first reading after it that has held
   still (within 0.5 A for ten seconds, from ten seconds after the switch, within two minutes). A step
   is used only when it is the same on every phase the charger is wired to, the other phases stayed
   quiet, it is at least 2 A, and it is not above the charger's maximum (its current setting's upper
   bound, else the main fuse). It is trusted once a second step agrees with it (an on and an off, or the
   same edge again). An "Automatic" single-phase charger learns no step until it is wired to a phase.
3. **The charging current you set** (*Laddström* in Auto, else the plan's current, else the last one
   asked for), before anything is learned; 16 A when none is set. It is the current you told SpotNav the
   charger uses and what the plan already reckons with.

The current and where it came from are in the charger's diagnostics (`switched_current`). With it:

- **It never blocks the other chargers.** Its request is its switched current, never "unknown".
- **An import overload pauses it whole.** While a phase is over the limit on a net import and such a
  charger takes part, the overload is resolved in one pass, by charger priority, Last first, then Normal,
  then First; within each priority: the chargers whose current can be set are lowered, at most to their
  minimum; if the site is still over, the switch-only chargers are paused whole, through the same
  balancing pause as any other charger; only then are the chargers whose current can be set paused. An
  electric car at 10 A beside a 10 A switch-only charger, 10 A over, ends at its minimum with the other
  one paused, not paused itself while the other runs on. If the switch-only charger's pause cannot be
  sent (its command fails or is refused), the chargers held at their minimum for it are paused in that
  same pass. It is never paused on a merely tight margin, and on an export or unclear overload nothing is
  done, as for any charger. A site of only such chargers is protected the same way.
- **It is resumed whole.** A charge load balancing paused, or a start it held back for want of room, is
  started again once every phase it uses has room for its whole switched current plus 1 A, for the
  dwell (60 s), with the usual limit on resumes. A start goes out only where the site has room for all of
  it. A charger that cannot report whether a car is plugged in (a plain switch, an OCPP charger whose connector
  status cannot be found, or a status sensor SpotNav cannot map for its platform) is resumed while the charge is
  still wanted; a charger that can report it must say a car is there. This holds for any paused charger, not only
  a switch.
- **A lost measurement does nothing to it.** It cannot be stepped to a minimum, and pausing it on a meter
  that merely went quiet would stop charges with no overload shown; it is treated as a charger already at
  its minimum.
- A charger with no reading of its own is taken to draw its whole switched current while it is on, the
  safe side for the fuse.
- The card never says it is limited to a current, and its capabilities say load balancing can only pause
  the charge.

## Charger priority

When several chargers share a site, each phase's headroom is handed out to them one after the
other. Every charger has a **Charger priority** in the card's charger entities editor (only a
charger that belongs to a site has one): **First**, **Normal** (the default) or **Last**. A charger
set to First is served before the others and keeps what it asks for; one set to Last gets what is
left. Chargers with the same priority are served in the order they joined the site.

When the site is over its limit and load balancing has to cut, the cut is taken in the opposite
order: from Last first, then Normal, then First, in the same pass. Within one priority the chargers
are cut in the order they joined the site. A charger set to First keeps its current as long as the
others can give way.

Solar surplus follows the same order. Of the chargers on solar (or on hybrid outside a plan window
and short of their target, or past it and below the car's own known limit), the first in the order is offered the whole surplus; the next is
offered only what the first cannot use: surplus too small for the first one to start on, or more
than it takes (its maximum current, or less when the car draws less). A charger that is paused, on
another strategy, or known to be unplugged takes no part, and its draw counts as house load.

## Solar and forecast settings

Set on the site, used by the [solar and hybrid strategies](strategies.md): **solar priority**
(car first or house battery first), an optional **home battery power** sensor (positive means
charging), and the **solar forecast sources** for hybrid.

The site's **Solar surplus** sensor shows, in watts, the power a car could take from the sun at the
site now, from these settings and the site's grid readings, also on a site without chargers; see
[Solar](strategies.md#solar).

## Total grid power

Solar and hybrid need the grid's signed power. A derived site has it per phase. A direct site
(a current per phase, no direction, such as a Tibber Pulse) has it only when you give it the
meter's **total grid power**, as one signed sensor (positive = import, or tick *Grid power is
export-positive*) or as an import and an export sensor (combined as import minus export; a missing
half makes the total missing, never zero). Units W and kW are read; anything else is refused at
read time. The total is detected where the integration has it (see
[supported hardware](supported.md)) and is edited in the site editor, field *Total grid power (for
solar)*; a derived site may also have one, which it ignores for the surplus.

The total only decides the surplus; the fuse protection and load balancing stay per phase on the
measured currents. Swedish and most Nordic meters settle the phases summed, so the surplus is the
total export. The solar controller spreads the total evenly over the phases the charger uses (a
third each for three phases, the whole total on the charger's phase for one phase), assumes 230 V
per phase and caps the result by that phase's fuse headroom on top of what the car already draws.
A stale (older than the maximum measurement age, unless Home Assistant keeps hearing from the
sensor) or missing total is an unknown surplus, never zero export. The site sensor's
`solar_capable` and `solar_reason` attributes and the diagnostics' `grid_power` block show the
state; `solar_reason` is `needs_total_grid_power` when a direct site lacks it.

## Demand charge and import target

A site measures each clock hour's mean import, for a [demand charge](prices.md#a-demand-charge) on its
electricity price and for its own **import target**. A demand charge is measured and shown only. The import target
holds the site's chargers, through load balancing, so the hour stays under it (see
[Holding the chargers under the import target](#holding-the-chargers-under-the-import-target)).

- **What is measured.** The net import from the grid, from the same signed grid power solar reads: a derived
  site's per-phase power, or a direct site's [total grid power](#total-grid-power). Export within an hour lowers
  that hour's import, as on the meter; a negative reading counts as no import. A direct site without the total
  cannot measure: the price section says so under the demand charge, and "Get started" asks for the site's total
  grid power while a demand charge or an import target is set.
- **Hours.** Each reading is held until the next one, for at most two minutes. An hour is its import energy over
  the time it was measured, on the clock of the electricity price's area (Home Assistant's own where the site has
  no electricity price); the repeated hour in October is two hours, as on the meter. An hour with more than ten
  minutes without a usable reading is kept but never sets a peak. The running hour is kept at most once a minute
  and the month's hours when an hour ends, so a restart keeps them; the minutes the restart took count as missing.
  At the first hour of a new month the month starts again; the month before is kept beside it. What the house
  imports without the chargers' measured power is kept beside each hour too, and whether the import target held a
  charger's current down or paused a charger in it.
- **Two years of days.** Beside the hours the site keeps, per day, its highest hour (with the house's part of it and
  the hour it started), whether the import target held or paused a charger that day, and per month each part's
  counted peaks and figure, for 24 months. They are written as each hour ends. The months before SpotNav measured are filled once from Home Assistant's statistics (below).
- **Where to see it.** The charge history's [Power view](card.md#power) shows a month day by day, with the import
  target as a line and the days it held or paused a charger marked, and a tapped day's hours; it is offered where the
  site measures its import.
- **Import target.** An optional hourly mean of import, in kW, set in the card's Settings beside the main fuse
  (*Import target*, off until set). It is written without restarting load balancing. While it is set, a line under
  it shows the hour running now against it, *This hour: 4.2 of 6.0 kW* (the mean over the minutes measured so far);
  where the month's peak lifts the target this hour, against that level: *This hour: 2.8 of 6.6 kW – the month's
  peak (import target 6.0 kW)*. Its editor suggests a target (see
  [A suggested import target](#a-suggested-import-target)).
- **The Demand-charge peak sensor.** While the site's electricity price has a demand-charge part, the site has a
  *Demand-charge peak* sensor (kW): the first part's figure this month. Its attributes are every part with its
  figure, its peaks and how many days counted, and the running hour. It goes away with the last part.
- **Diagnostics.** The site's diagnostics (and the debug bundle) carry a `demand` block: every hour of the month
  (`[start, kW, house kW, incomplete, held, paused, floor kW]`: kW the mean over the time measured, floor kW the import over the whole hour with any gap counted as none; an hour from Home Assistant's statistics has an eighth column, `1`), each part's figure and peaks, the running hour with its gaps, the
  import target, and `backfill` (when the [earlier months](#earlier-months-from-home-assistants-statistics) were
  read, what came of it and how many hours were filed).

### A suggested import target

When the import target's editor opens (administrators), SpotNav reads Home Assistant's hourly statistics of the
last three months (fewer where less exists) and suggests a target, for example *The house alone: usually at most
2.4 kW in an hour each day, at most 3.7 kW (Sat 17 Oct). Suggestion: 6.5 kW – the car then gets about 4.0 kW in
most hours.* **Use** fills the field; nothing is saved until *Save*.

- **What is read.** The hourly mean of the site's grid power (a direct site's total grid power, or an
  import/export pair, negated where the meter is export-positive; a derived site's per-phase power less each phase's
  export entity), and what each charger drew: its energy register's hourly change, else its power sensor's mean,
  else its measured current's mean per phase times 230 V. Only sensors with long-term statistics count (a
  `measurement` sensor, or a `total`/`total_increasing` register); W, kW and MW, A and mA, Wh, kWh and MWh are
  converted. The house alone is the grid's mean less the chargers', never below zero. A charger without statistics
  is named, and the grid alone stands for the house then (the charging is counted as the house's).
- **The rule.** The house's **typical** daily peak: the median over the days of each day's highest hourly mean of
  the house alone (only within the hours a demand-charge part counts when every part has hours of its own, else all
  hours), plus the car's minimum power on this site (the smallest charger's minimum current times its phases times
  230 V), rounded up to the next 0.5 kW. Not the highest peak: on the few evenings the house alone goes above the
  target (a sauna), the car is paused, the bill's peak is the one the house set anyway, and the month's peak then
  lifts the target for the rest of the month. A target at the highest peak would pay for a level only that evening
  needed. The house's highest hour is shown beside it, and what the car gets at the suggestion in a typical charging
  hour: the suggestion less the house's median hourly mean between 18:00 and 06:00, never below its minimum.
- **Never invented.** With no recorder, no long-term statistics of the grid power, fewer than seven days of them or
  no charger on the site, there is no suggestion and the editor says why.
- **Solar.** The statistics are hourly means: an hour that exported in one half and imported in the other nets out,
  so with solar the house's import is read lower than the meter billed it in such hours. Evening and night hours,
  which set the peaks, are not affected.
- The answer is kept for ten minutes per site, so opening the editor again does not read the statistics again.

### Earlier months from Home Assistant's statistics

Once, when a site first measures, the hours before SpotNav measured are filled from the same statistics, three
months back: the running month's and the month before's as hours, older months as day summaries. An hour's import is
the grid's mean (never below zero), the house's part as above where the chargers are known. They are marked as from
Home Assistant's statistics (hatched in the [Power view](card.md#power), and `imported` in the API), with no holds or
pauses and never as incomplete; an hour the statistics lack is simply missing. An hour SpotNav measured itself is
never overwritten: nothing at or after its first measured hour is filled. The demand-charge parts' counted peaks and
figures count them, only approximately what the meter billed. **They never lift the import target**: the month's
free level is worked out from SpotNav's own measured hours only, and the house's typical load (for the departure's
check and the plans) too, so an approximation never changes what a charger is told. Where nothing could be read (no
statistics of the grid power yet) the Power view asks again at most once a day while no earlier month is kept.

### Holding the chargers under the import target

While a site has an import target and load balancing (active control) is on, each recompute works out what the
site's chargers may still draw this clock hour, and load balancing gives each charger the smaller of that and what
the main fuse allows. The fuse's rules come first and are never loosened: the target only ever lowers a charger, an
overload's lowering is never changed by it, and a lowering for the target never goes so far that a phase the
charger draws on would turn from import to export (so the current the fuse carries can only fall).

- **The month's peak lifts the target, for the running hour.** Where the electricity price has a
  [demand charge](prices.md#a-demand-charge), the hour running now may reach a level the month has already paid
  for: its hold is lifted to the month's free level, the lowest of what each part that counts the running hour
  leaves free (a highest-hour part's peak this month; a top-3 or top-5 part's third or fifth highest day, or today's
  own peak while today is among them; with fewer days than that, today's own peak or nothing). Each hour is counted
  at its floor for this, its import over the whole hour with any minutes without a reading counted as none, so never
  above what the operator billed. Never below the target as set. An
  hour no part counts keeps the target, a new month starts from it, and an incomplete hour never sets a peak.
  Example: a 6.0 kW target, and a sauna evening made the day part's peak 7.0 kW: for the rest of the month day hours
  are held under 7.0 kW (less the margin below, 6.86 kW). Only the running hour's hold is lifted: a plan, and the later hours of the departure's check,
  count on the target as set (a later hour may be counted by no part, a new day or a new month). The fuse is
  untouched. The status names both (*Held to 8 A to keep this hour under 7.0 kW, the month's peak (import target
  6.0 kW)*), as do the house-alone lines and the notice, the line under the target in Settings, and the Power
  view's line of the month's peak. An hour read from Home Assistant's statistics never lifts it. No setting. **A power-level subscriber** (Göteborg's
  tidsindelad level, which must never be exceeded) should leave the demand charge's parts out, or set them so they
  never free more than the level, or the month's peak lifts the hold above it.
- **A small margin.** SpotNav keeps a small margin below the target, the larger of 0.1 kW and 2 %, for a meter that
  reads differently from the operator's and the hour's last minutes: a 6.0 kW target holds the hour under 5.88 kW.
  It is taken once, from the level the hour is held to (the target, or the month's peak that lifts it), never below
  nothing. Plans and the departure's check count on the target less it too, so they agree with what the hold allows.
  What you are shown stays your own numbers (the 6.0 kW target, the month's peak); the diagnostics show both.
- **The hour's budget.** The target (less its margin) times one hour, less what was imported so far this hour, is what the rest of
  the hour may import. Spread over the time left, less what the house draws now (the site's net import less what
  its chargers draw; a solar surplus counts as less, so it leaves the chargers more), it is what the chargers may
  draw. In the hour's last minute the time left counts as a minute, and the fuse's own ceiling bounds the result.
  A few minutes without a reading count at the hour's own mean.
- **Several chargers** share it in the site's order (priority First, Normal, Last, then the order they joined). Each
  is held to the whole amps that still fit. A switch-only charger runs only while its whole switched power fits.
- **At the minimum.** A charger is never held below its minimum current. Once the hour can no longer end at or under
  the target with it at its minimum for the rest of the hour, it is paused until the next clock hour and given back
  then, as any load-balancing pause is (with the resume's margin and dwell). It is paused at most once per hour: it
  stays paused to the hour's end even when the house uses less again, from the moment the pause goes out (whenever
  the charger reports itself off). A pause decided for a moment (a kettle) and taken back before the dwell let it
  out pauses nothing.
- **Raises** come at the next hour, or when the budget allows them, through the same deadband and dwell as every
  write. A lowering to a current is written at once (no deadband: an amp kept back would be drawn to the hour's
  end), and a raise that only gives back the target's own lowering within the hour passes the deadband too (it still
  waits for the dwell), so a wobbling house does not ratchet the car down. In the hour's last ten minutes a charger the target held is raised no further than it held it (the budget
  left is spread over too little time to be worth a raise the next hour takes back); a charger the target did not
  hold is the fuse's and the budget's as at any other time.
- **The house alone over the target.** When the house by itself ends the hour above the target, SpotNav pauses its
  chargers and holds back nothing else; the status says that the house alone is above the target, never that the
  hour stays under it.
- **The car's minimum level comes first.** Below the car's [minimum level](target-soc.md) SpotNav charges at once,
  and that charge is never held by the target. A minimum level at or above the plan's target makes the whole charge
  a minimum-level charge: none of it is held.
- **Above it the target holds, even past a departure.** When the plan cannot be ready by its departure under the
  target (what it still needs, against what the target leaves the car by then beside the house's typical load: its
  mean over the last 24 measured hours and the hour so far, so a kettle's half minute changes nothing; decided again
  on every recompute, with half a kWh of margin either way), what happens is the plan's own choice, *If the car
  cannot be ready in time* in the card's Plan dialog (shown while the site has a target):
  - **Ask** (the default): the target holds, and the phones chosen for *the charge will not be ready in time* are
    told once, in its place: *The car will not be ready by 07:00 under the 6.0 kW import target – 4 kWh short*, with
    one button, *Release the import target for this charge*. Told again only when the shortfall grows clearly (by a kWh and by half), and taken
    off the phones once it can be met again. No answer: the target holds. The card's status says the same, with the
    same press.
  - **Release the import target**: the departure wins by itself; once holding would miss it the target is set aside for that
    charge until its departure, as *Release the import target for this charge* does, and the plan counts on the car's own power.
  - **Keep the import target**: the target holds, nothing is sent, and the status says how much will be missing.
- **Release the import target for this charge** (from the notification, the card or the app) sets the target aside for that charge until its
  departure, or until the car is unplugged or the plan is made for another departure; a restart keeps it. A charge
  without a departure ahead (a person's Start, or a plan without one) is set aside until the car is unplugged, the
  car ends the charge by itself (full, or at its limit), or the first noon at least six hours after the press
  (pressed at 11:59, the next day's), whichever comes first: a release always ends by then, also on a charger that cannot
  tell whether a car is plugged in. A Start pressed after a release keeps it: the plan the Start drops no longer
  ends it, and it lasts until the car is unplugged as above. The
  card offers it beside every line where the target holds or pauses a charge, and asks first, since a press can
  cost the month: *Release the import target? The car then charges at full power until it is unplugged, or until the
  departure at 07:00. The month's peak may reach up to 11 kW, against 6.0 kW today.* (up to: the house's typical
  load and the charge's full power, rounded; today: the month's level the target holds to). The phone's button is
  one press, as it answers a question already asked. A phone's
  button carries the departure it was told for: pressed once the plan is for another departure (after a restart or a
  new plan), it sets nothing aside. While the target's notice stands, the plan's own *will not be ready* notice is
  held back; once the target's notice comes down and the plan still cannot be met for reasons of its own, that
  notice is told. The status says *The import target is set aside by
  you until 07:00*, or *until the car is unplugged or the charge is done, by noon at the latest*.
- **Plans count on the target.** A plan for a charger on the site counts on the power the target leaves it (the
  target less the house's typical load as above, never below the charger's minimum), so it buys the quarter-hours
  the held charge needs and says honestly what it can deliver by the departure (see
  [Planning strategies](strategies.md)). Not for a plan set to *Release the import target*, nor for a charger the hold
  cannot act on (no current of its own to judge a lowering on, or a phase that cannot be judged).
- **A person's Start obeys the target** as a plan's charge does: held, or paused to the hour's end, and shown so,
  with *Release the import target for this charge* the only way past it. No phone is told just because a Start is held. A
  person's Stop is untouched. Only the car's minimum level is never held (above), and the sun's charge is left
  alone: it counts toward the hour but is never held. A held charge the fuse paused counts at what it resumes at.
- **Solar and Hybrid.** The target counts import from the grid only, so a charge the sun covers is free. Under
  *battery first* a home battery's discharge counts as import too (the car is not to be paid for by the battery);
  under *car first* only the grid counts. A lowering never turns a phase the car draws on from import to export,
  so with the grid held at about nothing it could not lower the car at all; under *battery first*, with the battery
  read live and discharging, lowering the car by up to the battery's discharge only lowers that discharge, so the
  car may come down that much further (spread evenly over its phases, and shared among the chargers the target
  lowers at the same time). That extra drop is never more than the smallest fuse margin over the car's phases, so
  a battery that does not follow the car down (a fixed setpoint, a single-phase battery beside a three-phase car)
  cannot export past the fuse. Any drop beyond that still keeps every phase an import, and with the battery not
  read live nothing changes. A charger lowered only part of the way is marked `floor_limited` in the diagnostics,
  and a plan for it does not count on the hold's power.
- **Yield stepping and the battery probe** never step a charger above what the target leaves it, and no probe or
  resume starts a charge the target holds back.
- **Not applied** (nothing held for the target, and the fuse rules as always) while load balancing is off, the
  site cannot measure the hour (a direct site without its total grid power), the meter has no live reading, the
  hour has more than ten minutes without one, or under *battery first* the battery cannot be read. After a restart
  the hour kept by the tracker decides again; the minutes the restart took count as missing. Should the target's own
  code fail, the fuse's decisions go out as they are and nothing is held for it.
- **Shown.** The charger's status says *Held to 7 A to keep this hour under 6.0 kW (now 5.1 kW)*, *Paused until
  15:00 to keep this hour under 6.0 kW* (or, where the house alone is above the target, *Paused until 15:00 – the
  house alone is above the 6.0 kW import target this hour*), *The car will not be ready by 07:00 under the 6.0 kW import
  target – 4 kWh short* with *Release the import target for this charge*, or that the target is set aside until 07:00. The site's
  load-balancing limit names the cause `import_target` only where the target's budget holds the charger. The site's
  diagnostics carry an `import_target` block: the hour's budget or why it is not applied, the level held to (`effective`: the lifted level, the month's free level and whether every part counts the hour), what it does to each
  charger (held, paused, kept at the hour's end, or why it could not hold it), the hour each was paused in, the
  departures at risk and those set aside, and its recent holds, pauses and releases.

## Meter detection, signs and estimated current

SpotNav scans the entity registry (including entities an integration ships disabled) for grid meters and home batteries and offers them in the site editor and the create flow (the recognised integrations are listed under [supported hardware](supported.md)); nothing is applied until confirmed, and applying enables only entities the integration disabled, never ones a person disabled. Each integration carries its sign conventions:

* `site_current_signed`: the meter reports export as a negative current; the fuse carries |I|, so the magnitude is read. Without the flag a negative current is invalid. On a direct site with the total grid power, the sign also gives each phase its direction for active load balancing once checked against the total (see [Active load balancing](#active-load-balancing)).
* `grid_power_inverted` / `battery_power_inverted`: export-positive grid power (the per-phase power of a derived site and the total grid power) and discharge-positive battery power are negated.
* Import and export reported as two entities (`power` and `power_export`, or a battery's charge and discharge) are combined as import minus export; a missing half makes the value missing, never zero.

Derived mode needs only signed active power and voltage per phase. The current is, in order: the meter's own current (as |I|), S / U from apparent power, sqrt(P^2 + Q^2) / U from reactive power, else `|P| / (U x 0.9)`, which is marked **estimated** in the site state and the card. The estimate is never below the real current for a power factor of 0.9 or better and understates it below that. A configured source that is unavailable never falls back to a cruder one.

The card warns when a source's integration updates more slowly than the maximum measurement age (naming the integration option that lowers it where known), and when a device with its own load balancing (Easee Equalizer, Zaptec Sense, Ferroamp ACE, ONEp1) is present.
