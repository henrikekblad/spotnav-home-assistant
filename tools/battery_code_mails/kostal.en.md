Subject: SpotNav: testing Kostal Plenticore battery control

Hi,

Thank you for signing up to test SpotNav's control of your Kostal Plenticore battery. Here is what happens next.

**Before the test.** SpotNav controls a Plenticore only through the Kostal Plenticore Battery Control integration
(kostal_modbus_control, from HACS), and only when:

- the inverter's Battery Management is set to "External via protocol (Modbus TCP)" (an installer setting in the
  inverter's web UI). The integration's "Battery Management Mode" sensor must read "External battery management via
  Modbus protocol"; it ships disabled, and the card offers to enable it;
- the integration's Modbus Timeout is the same as the timeout beside that setting in the web UI;
- none of the integration's switches (Charge Start, Discharge Start, Block Charge, Block Discharge, Inverter
  Control) is on, and Predbat, if installed, is not in a control mode;
- nothing else writes to the inverter (evcc, Tibber's bridge, a Modbus automation of your own): the inverter takes
  one controller.

The card also asks you to confirm that SpotNav's commands override the inverter's own battery control and time
control, which the integration does not show.

**The hardware test.** In the SpotNav card: Settings → Support → Hardware test, then Start. That is all: there is
nothing else to do or to watch. The test waits by itself for a suitable moment (the house using more than the sun
gives, the battery between its limits, no car charging) and runs only in waking hours, 08–21. It takes about ten
minutes. While it runs, your phones with SpotNav's notices get a message with a "Stop the test" button.

What it does, through the integration's own switches:

1. Hold: Block Discharge on; the battery stops covering the house (it still stores sun).
2. A small grid charge: Block Discharge off, Set Charge Rate to the test's power (at most 2 kW, at least 5 % below
   the rate's maximum, less if the fuse needs it), then Charge Start on.
3. Cover the house: Charge Start off, the inverter's own control.
4. Hand back: both switches off as found, then the charge rate as found.

It stops at once and hands back if a write does not read back, if the battery does not do as told or if a phase
comes near the main fuse. The result is sent to me by itself when the test ends, while Beta tester is on.

**Three things I would like to know** (a short reply is enough): the timeout set in the web UI beside "External via
protocol (Modbus TCP)", your inverter (Plenticore plus G1, G2, G3, BI or MP G3) with its battery, and whether
anything else has ever written to it.

**Stopping.** Press "Stop the test" in the notice or in the card's hardware test, at any time. To stop testing
altogether (nothing more is sent), untick Beta tester under Support.

**If something is left behind** (Home Assistant restarted mid-test, say): the integration's switches start off after
a restart and the inverter returns to its own control by itself once its timeout has run out. Otherwise turn Charge
Start and Block Discharge off, then turn SpotNav's battery switch off.

I read the test's log and answer you by e-mail.

SpotNav

---8<--- code
Subject: SpotNav: your control code for Kostal Plenticore

Hi,

Thank you for running the hardware test. I have read its log and it looks right, so here is the control code for
your installation:

{code}

Paste it in the SpotNav card: Settings → Support → Hardware test → Control code, then Enter. Spaces and line breaks
do not matter. It works only on your Home Assistant installation and your Plenticore. After that you can switch
control on in the home battery section ("Battery control").

What control does on your Plenticore: a hold turns Block Discharge on; a grid charge sets Set Charge Rate and turns
Charge Start on; every hand-back turns them off. The integration repeats each command to the inverter within its
timeout, and the inverter returns to its own control by itself when Home Assistant stops. That is not counted on:
SpotNav caps every grid charge for the case that Home Assistant stops, counting all of it on one phase.

After a firmware or integration update SpotNav checks the battery again by itself; no new code is needed. If that
check fails, the card asks for the hardware test again.

For 30 days from now beta testing keeps sending one report a night, so I can see how control runs. Untick Beta tester
under Support to stop them at any time; the code keeps working.

**Restore by hand** if you ever need to: Charge Start and Block Discharge off, then turn SpotNav's battery switch off.

SpotNav
