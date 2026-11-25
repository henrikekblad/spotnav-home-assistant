Subject: SpotNav: testing Victron battery control

Hi,

Thank you for signing up to test SpotNav's control of your Victron ESS system. Here is what happens next.

**Before the test.** The ESS mode must be Optimized (with or without BatteryLife) and Dynamic ESS off. Nothing else on
the GX may write the setpoints (a Node-RED flow, a script). If another system runs the battery, pause it first: the
test refuses to run otherwise. It helps to note ESS max charge power, ESS max inverter power and AC power setpoint
before.

**The hardware test.** In the SpotNav card: Settings → Support → Hardware test, then Start. That is all: there is
nothing else to do or to watch. The test waits by itself for a suitable moment (the house using more than the sun
gives, the battery between its limits, no car charging) and runs only in waking hours, 08–21. It takes about ten
minutes. While it runs, your phones with SpotNav's notices get a message with a "Stop the test" button.

What it does, through the Victron GX integration (or Victron MQTT), in ESS mode 2 where the GX runs its own loop:

1. Hold: ESS max inverter power limit to 0; the battery does not discharge (the sun is still stored).
2. A small grid charge (at most 2 kW, less if the fuse needs it): ESS max charge power to that power and AC power
   setpoint (the grid's power, not the battery's) to the house's load now plus that power. On a three-phase system
   where the fuse leaves no room for that, a charge of at most 1 kW on the phase with the most room is tried instead,
   in external control (ESS mode Hub4) with a setpoint per phase, renewed every 20 seconds.
3. Cover the house: ESS max charge power to 0; the GX covers the house as always and the battery stores nothing from
   the inverter.
4. Hand back: AC power setpoint, ESS max charge power and ESS max inverter power back to the values they had (and the
   ESS mode back as it was, if the charge per phase ran).

It stops at once and hands back if a write does not read back, if the battery does not do as told or if a phase
comes near the main fuse. The result is sent to me by itself when the test ends, while Beta tester is on.

**Stopping.** Press "Stop the test" in the notice or in the card's hardware test, at any time. To stop testing
altogether (nothing more is sent), untick Beta tester under Support.

**If something is left behind** (Home Assistant restarted mid-test, say), restore by hand in Home Assistant or on the
GX under Settings → ESS: the ESS mode back to your Optimized mode, AC power setpoint to your usual value, ESS max
charge power and ESS max inverter power to your values (-1 is no limit). Check Hub4 disable charge and Hub4 disable
feed-in too. Then turn SpotNav's battery switch off.

I read the test's log and answer you by e-mail.

SpotNav

---8<--- code
Subject: SpotNav: your control code for Victron

Hi,

Thank you for running the hardware test. I have read its log and it looks right, so here is the control code for
your installation:

{code}

Paste it in the SpotNav card: Settings → Support → Hardware test → Control code, then Enter. Spaces and line breaks
do not matter. It works only on your Home Assistant installation and your Victron. After that you can switch control
on in the home battery section ("Battery control").

What control does on your Victron ESS, only in an Optimized mode with Dynamic ESS off: a hold sets ESS max inverter
power limit to 0, covering the house ESS max charge power to 0, a grid charge ESS max charge power to the planned
power and AC power setpoint to the house's load plus that power, and a sale AC power setpoint below the house's load.
These are settings on the GX that stay if Home Assistant stops, so a grid charge always stays under the cap for the
fuse. A charge per phase runs in external control with a setpoint per phase, renewed every 20 seconds; that the Multi
goes to passthru by itself when the writes stop is not counted on until a plant has shown it. Every hand-back sets the
values (and the ESS mode) back as they were.

After a firmware or integration update SpotNav checks the battery again by itself; no new code is needed. If that
check fails, the card asks for the hardware test again.

For 30 days from now beta testing keeps sending one report a night, so I can see how control runs. Untick Beta tester
under Support to stop them at any time; the code keeps working.

**Restore by hand** if you ever need to: the ESS mode to your Optimized mode, AC power setpoint, ESS max charge power
and ESS max inverter power to your values, then turn SpotNav's battery switch off.

SpotNav
