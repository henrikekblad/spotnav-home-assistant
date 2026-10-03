# Site and load balancing

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
- **Chargers on this site**, and for each charger whether it is three-phase or on one explicitly
  chosen phase. A single-phase charger whose phase is unknown never gets a recommendation.
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
    A one-phase charger is read from T3 only (TN). The charger's dialog shows the source the
    site reads, read-only.
  - Without any measured current for a charger, the regulator holds (`charger_measurement_unusable`)
    and solar never starts that charger. A running charge is held at the minimum current while the
    grid shows export or about zero import (100 W per phase), and stopped once import beyond that
    has lasted the solar stop delay; the reason is `charger_measurement_missing`.

After the basics, SpotNav looks for your grid meter in the entity registry (see
[Meter detection](#meter-detection-signs-and-estimated-current)). When it finds the meter and
everything else it needs, it shows a **Site found** summary to confirm; otherwise it shows the
forms above. A new site is created with its calculation on and active control off.

A charger added later is offered to the site when exactly one site exists, if SpotNav can tell how
the charger is wired (see [Set up SpotNav](setup.md#add-the-charger-to-an-existing-site)).

Everything can be changed later under the site's **Configure** and, for entities, in the card's
settings popover. In **Configure** the first step holds the basics and a tick box, **Change
measurement and phase wiring**. Unticked, saving keeps the stored measurement and wiring; the step
opens anyway when the measurement mode changed, when chargers were added or removed, or when the
measurement is incomplete. A site with nothing configured yet reports "not configured" and does
nothing.

## What the site calculates

For every phase the site works out what is left for chargers:

    headroom = main fuse - safety margin - other load

where *other load* is the site's measured total minus the chargers' own measured current on
that phase. Each charger gets a proposed current from the headroom on its phases, in one pass,
between the charger's minimum (6 A by default) and its own limits.

Measurements older than the *maximum measurement age* (default 120 s) are not trusted, and if
the chargers together claim more current than the site's total (beyond a small tolerance) the
whole result is reported as invalid rather than corrected silently. Results are exposed as
sensors and in [diagnostics](troubleshooting.md).

## Active load balancing

By default the site only calculates. Turn on **Active load balancing** (site options, or in the
card by an administrator) to let SpotNav lower a charger's current to keep the site under its fuse.
It is best effort and is not a protective device. Turning it off gives back any current it had lowered.

- A sudden overload is reduced immediately. Other changes are damped: a change smaller than the
  *deadband* (2 A) is ignored, and a new level must hold for the *dwell* time (60 s) before it
  is written, so the charger does not dither.
- It requires a site with usable measurement and a charger SpotNav can command: one whose
  current SpotNav is set to change (an OCPP charger set through ChangeConfiguration, or a charger
  with a current number or service, see [supported hardware](supported.md)). A charger whose
  current is stored in the charger cannot be adjusted during a charge, but can be stopped when the
  fuse needs less than its setting. It is refused while a charger belongs to more than one site.
- **Yield-verified stepping** (opt-in) is for sites with a load that gives way when the car
  draws more, for example a house battery holding the grid at its setpoint. SpotNav then judges a
  step by whether it moves the site current, within a hard ceiling that must be above the main
  fuse and below 1.25 times it.
  - **A home battery that charges from the grid and gives way.** With a *home battery power*
    sensor and solar priority *car first* (the default), SpotNav also checks that the battery's
    own charge falls when the car takes more. Once that is verified the car may climb in larger
    steps (up to 3 A, never more than the battery's charge per phase, and 1 A near the plan's
    current), the damper's dwell does not hold such a step, and the card says why the car is below
    its plan: "The home battery charges from the grid and shares the main fuse: the car gets 11 A."
    With *battery first*, or a battery that does not give way, nothing is credited and the steps
    stay at 2 A.
  - **A short overload is not a reset.** While yield stepping is verified, one sample above the
    ceiling (and at most 4 A above it) steps the car back once instead of to the minimum. A second
    sample in a row, a larger excess, or two such samples within two minutes still stops at the
    safe level, and the measured per-phase current stays the fuse protection throughout.
- **Decision log.** Every change the regulator makes or holds (time, from and to amps, the reason,
  the limiting phase, the battery power and the measured currents) is kept in a bounded list per
  site, the last 200, shown as `regulator_decision_log` in the site's diagnostics (and so in the
  debug bundle).
- Reading a measurement that has not changed is told apart from a dead link before the reading
  is treated as stale.

## Charger priority

When several chargers share a site, each phase's headroom is handed out to them one after the
other. Every charger has a **Charger priority** in the card's charger entities editor (only a
charger that belongs to a site has one): **First**, **Normal** (the default) or **Last**. A charger
set to First is served before the others and keeps what it asks for; one set to Last gets what is
left. Chargers with the same priority are served in the order they joined the site.

## Solar and forecast settings

Set on the site, used by the [solar and hybrid strategies](strategies.md): **solar priority**
(car first or house battery first), an optional **home battery power** sensor (positive means
charging), and the **solar forecast sources** for hybrid.

## Total grid power (for solar)

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
`solar_capable`, `solar_reason` and `grid_power` attributes and the diagnostics' `grid_power`
block show the state; `solar_reason` is `needs_total_grid_power` when a direct site lacks it.

## Meter detection, signs and estimated current

SpotNav scans the entity registry (including entities an integration ships disabled) for grid meters and home batteries and offers them in the site editor and the create flow (the recognised integrations are listed under [supported hardware](supported.md)); nothing is applied until confirmed, and applying enables only entities the integration disabled, never ones a person disabled. Each integration carries its sign conventions:

* `site_current_signed`: the meter reports export as a negative current; the fuse carries |I|, so the magnitude is read. Without the flag a negative current is invalid.
* `grid_power_inverted` / `battery_power_inverted`: export-positive grid power (the per-phase power of a derived site and the total grid power) and discharge-positive battery power are negated.
* Import and export reported as two entities (`power` and `power_export`, or a battery's charge and discharge) are combined as import minus export; a missing half makes the value missing, never zero.

Derived mode needs only signed active power and voltage per phase. The current is, in order: the meter's own current (as |I|), S / U from apparent power, sqrt(P^2 + Q^2) / U from reactive power, else `|P| / (U x 0.9)`, which is marked **estimated** in the site state and the card. The estimate is never below the real current for a power factor of 0.9 or better and understates it below that. A configured source that is unavailable never falls back to a cruder one.

The card warns when a source's integration updates more slowly than the maximum measurement age (naming the integration option that lowers it where known), and when a device with its own load balancing (Easee Equalizer, Zaptec Sense, Ferroamp ACE, ONEp1) is present.
