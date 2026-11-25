Subject: SpotNav: testing Fronius battery control

Hi,

Thank you for signing up to test SpotNav's control of your Fronius battery. Here is what happens next.

**Before the test.** SpotNav uses the battery limits of the Fronius integration in Home Assistant core (Home
Assistant 2026.11 or later), which talk Modbus TCP to the inverter. Please check, once:

- In the inverter's web UI, Communication → Modbus: Modbus TCP on and "Inverter control via Modbus" on. Without it
  the integration shows no battery limits at all.
- In the web UI, the battery's "Battery charging from other sources (including public grid)" on, and in Home
  Assistant the switch **Battery grid charging** on, if the grid charge step is to run. SpotNav reads both and never
  sets them.
- Time-of-use schedules in the web UI and the Energy Cost Assistant in Solar.web off. Home Assistant cannot see them,
  so the card asks you to confirm that SpotNav's limits take over from them.
- Recommended: in Home Assistant, Settings → Devices & services → Fronius → Reconfigure, turn on "Revert the power
  and battery limits if Home Assistant stops". The inverter then drops a limit by itself about an hour after Home
  Assistant stops.
- Nothing else (evcc, a script, an automation) sets the battery limits: SpotNav does not start while one of the two
  limit switches is on.

**The hardware test.** In the SpotNav card: Settings → Support → Hardware test, then Start. That is all: there is
nothing else to do or to watch. The test waits by itself for a suitable moment (the house using more than the sun
gives, the battery between its limits, no car charging) and runs only in waking hours, 08–21. It takes about ten
minutes. While it runs, your phones with SpotNav's notices get a message with a "Stop the test" button.

What it does, only with both battery limit switches off when it starts:

1. Hold: Battery discharge power limit at 0 % and Battery discharge power limiting on; the battery stops covering the
   house (it still stores sun).
2. A small grid charge: Battery discharge power limit below 0 (a forced charge at a share of the battery's rated
   power, at most 2 kW, less if the fuse needs it) and Battery charge power limit at the same share as its cap, both
   switches on.
3. Cover the house: Battery charge power limit at 0 % and Battery charge power limiting on, the discharge limit off;
   the battery covers the house and stores nothing.
4. Hand back: both limit switches off, both limits back to the values they had.

It stops at once and hands back if a write does not read back, if the battery does not do as told or if a phase
comes near the main fuse. The result is sent to me by itself when the test ends, while Beta tester is on.

**Stopping.** Press "Stop the test" in the notice or in the card's hardware test, at any time. To stop testing
altogether (nothing more is sent), untick Beta tester under Support.

**If something is left behind** (Home Assistant restarted mid-test, say), restore by hand in Home Assistant: turn
Battery charge power limiting and Battery discharge power limiting off, set both limits to your values (100 % if you
never changed them), then turn SpotNav's battery switch off. With the revert option on, a limit left standing also
ends by itself about an hour after Home Assistant stops.

I read the test's log and answer you by e-mail.

SpotNav

---8<--- code
Subject: SpotNav: your control code for Fronius

Hi,

Thank you for running the hardware test. I have read its log and it looks right, so here is the control code for
your installation:

{code}

Paste it in the SpotNav card: Settings → Support → Hardware test → Control code, then Enter. Spaces and line breaks
do not matter. It works only on your Home Assistant installation and your Fronius. After that you can switch control
on in the home battery section ("Battery control").

What control does on your Fronius: a hold sets the battery discharge power limit to 0 %, a grid charge sets it below
0 (a forced charge at the rate) with the charge limit at the same share as its cap, covering the house without storing
sets the charge limit to 0 %. Every hand-back turns both switches off and puts the limits back. SpotNav never sets
grid charging or the minimum reserve, and does not sell from the battery yet. The inverter keeps a limit until it is
changed, or, with the integration's revert option on, until about an hour after Home Assistant stops, so SpotNav
caps each grid charge for the case that Home Assistant stops.

After a firmware or integration update SpotNav checks the battery again by itself; no new code is needed. If that
check fails, the card asks for the hardware test again.

For 30 days from now beta testing keeps sending one report a night, so I can see how control runs. Untick Beta tester
under Support to stop them at any time; the code keeps working.

**Restore by hand** if you ever need to: both limit switches off, both limits to your values, then turn SpotNav's
battery switch off.

SpotNav
