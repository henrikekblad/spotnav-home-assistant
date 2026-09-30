# OCPP chargers

SpotNav works with chargers through the Home Assistant OCPP integration (0.12 and later), and
with any charger that exposes a switch. There is no separate OCPP choice when adding a charger:
choose **Automatic (recommended)** and pick the charge point's connector. SpotNav recognises it as an
OCPP device, offers only its compatible entities, and asks the charge point how its current can be
set (see [Setting the current](#setting-the-current)). The walk-through is in
[Set up SpotNav](setup.md#1-add-a-charger), and [supported hardware](supported.md) says which
OCPP hardware has been tested.

## Entities

| Role | Entity | Scope |
| ---- | ------ | ----- |
| Charge control | `switch.<charge_point>_connector_<n>_charge_control` | per connector; starts and stops charging |
| Session current limit | `number.<charge_point>_connector_<n>_session_current_limit` | per connector, only during a transaction |
| Station maximum | `number.<charge_point>_maximum_current` | per charge point, a persistent ceiling |
| Connector status | `sensor.<charge_point>_connector_<n>_status_connector` | per connector |
| Transaction | `sensor.<charge_point>_connector_<n>_transaction_id` | per connector |

A charge point with one connector uses the flat names (`switch.<charge_point>_charge_control`),
which mean connector 1. The station maximum is never written by SpotNav. The session current
limit is `unavailable` while no transaction runs, which is normal.

## Which connector

A charger with several connectors has one Charge Control switch per connector, and the device list
shows the connectors. Select the one for the outlet you use, and add each connector you want to
control as its own charger. The charge point and connector are derived once from that entity and stored;
nothing else parses an entity id to decide a connector, and connector 1 is never assumed. If no unique
connector can be established the current is recorded but not applied; choose the Charge Control
entity of the connector again under **Configure**.

## Setting the current

Chargers that should follow a plan's amps, solar surplus or load balancing need current
control. SpotNav decides how when the charger is added, by asking the charge point and never by
its make or model. While the charge point is reachable, SpotNav reads its `AssignedCurrent`
configuration key, a read-only question with a ten second limit:

- **The charge point lists this connector.** The current is set through **ChangeConfiguration
  (AssignedCurrent)**: the OCPP integration's `configure` service sets the `AssignedCurrent`
  configuration key. It modulates a running charge with no charging profile and no reboot.
- **The charge point answers without it.** If the connector has exactly one session current limit
  number, the current is set through that number, at most one write every 10 s. Otherwise
  SpotNav does not set the current.
- **The charge point does not answer**, for example because it is not connected yet. Nothing is
  decided and nothing is guessed: the flow shows the entities form with a message asking you to wait
  until the charger is connected and submit again, or to choose yourself.

When a connector is found this way, the **Charger found** dialog names the result, and **Change
these choices** opens the form where **How SpotNav sets the current** can be changed:
**Do not set a current**, **ChangeConfiguration (AssignedCurrent)**, or, when the connector has one,
the charger's current number. With **Do not set a current** the requested current is recorded for
planning and reporting but never applied, and the charger runs at its own configured maximum.

Later, **Configure** on the charger's entry shows **Set the charger current** with **Do not set a
current** and **ChangeConfiguration (AssignedCurrent)** for an OCPP connector. The session limit
number is chosen when the charger is added.

Without any of these, SpotNav still starts and stops the charge.

## The 6 A floor

IEC 61851 chargers do not deliver under 6 A. SpotNav never writes a current below the charger's
minimum (6 A by default); when load balancing or solar leaves less, it does not lower the
charger further.

## Vehicle not requesting current

If a charge was started and the connector stays `SuspendedEV` with about zero import current
for two minutes, the card shows that the vehicle is not requesting current. It is only an
observation: SpotNav does not retry, reset or clear anything, because the usual cause is at the
car (re-plugging the cable is what normally helps). SpotNav reads the current-import main state
for this, not the per-phase attributes, which can keep stale non-zero samples.

## One controller per switch

A charge-control switch, and a current-limit number if configured, can belong to only one
SpotNav charger. Reusing one from another SpotNav entry is refused, because two controllers
could send contradictory start and stop commands.
