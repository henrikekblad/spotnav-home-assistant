Subject: SpotNav: testing Marstek battery control

Hi,

Thank you for signing up to test SpotNav's control of your Marstek battery. Here is what happens next.

**The hardware test.** In the SpotNav card: Settings → Support → Hardware test, then Start. That is all: there is
nothing else to do or to watch. The test waits by itself for a suitable moment (the house using more than the sun
gives, the battery between its limits, no car charging) and runs only in waking hours, 08–21. It takes about ten
minutes. While it runs, your phones with SpotNav's notices get a message with a "Stop the test" button.

What it does, through the Marstek Modbus integration, only while the work mode reads Self Consumption:

1. Hold: Max Discharge Power to 0; the battery stops discharging (it still stores sun).
2. A small grid charge: RS485 Control Mode on, Set Charge Power at the test's power (at most 2 kW, never more than
   the entity takes, less if the fuse needs it), Force Mode charge.
3. Cover the house: Force Mode standby, RS485 Control Mode off, Max Discharge Power back.
4. Hand back: everything as it was before the test.

It stops at once and hands back if a write does not read back, if the battery does not do as told or if a phase
comes near the main fuse. The result is sent to me by itself when the test ends.

**Stopping.** Press "Stop the test" in the notice or in the card's hardware test, at any time. To stop testing altogether,
untick Beta tester under Support.

**If something is left behind** (Home Assistant restarted mid-test, say), restore by hand in Home Assistant: Force
Mode to standby, RS485 Control Mode off, Max Discharge Power to your value (often 2500 W, or 800 W in Germany), then
turn SpotNav's battery switch off. If the battery's maximum powers read 800 W after the test, toggle them in the
Marstek app.

I read the test's log and answer you by e-mail.

SpotNav

---8<--- code
Subject: SpotNav: your control code for Marstek

Hi,

Thank you for running the hardware test. I have read its log and it looks right, so here is the control code for
your installation:

{code}

Paste it in the SpotNav card: Settings → Support → Hardware test → Control code, then Enter. Spaces and line breaks
do not matter. It works only on your Home Assistant installation and your Marstek. After that you can switch control
on in the home battery section ("Battery control").

What control does on your Marstek: a hold sets Max Discharge Power to 0; a grid charge uses RS485 Control Mode, Set
Charge Power and Force Mode; every hand-back sets Force Mode to standby, RS485 Control Mode off and Max Discharge
Power back. The battery has no dead-man of its own: a force charge goes on until it is told otherwise, so SpotNav
caps every grid charge for the case that Home Assistant stops, counting all of it on the battery's one phase.

After a firmware or integration update SpotNav checks the battery again by itself; no new code is needed. If that
check fails, the card asks for the hardware test again.

For 30 days from now beta testing keeps sending one report a night, so I can see how control runs. Untick Beta tester
under Support to stop them at any time; the code keeps working.

**Restore by hand** if you ever need to: Force Mode to standby, RS485 Control Mode off, Max Discharge Power to your
value, then turn SpotNav's battery switch off.

SpotNav
