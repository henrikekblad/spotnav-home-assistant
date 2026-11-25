Subject: SpotNav: testing SolaX battery control

Hi,

Thank you for signing up to test SpotNav's control of your SolaX battery. Here is what happens next.

**Before the test.** Remote Control Power Control must read Disabled (nobody else is using the remote control). If
another system runs the battery (Predbat, an energy manager, SolaX Cloud), pause it first: the test refuses to run
otherwise.

**The hardware test.** In the SpotNav card: Settings → Support → Hardware test, then Start. That is all: there is
nothing else to do or to watch. The test waits by itself for a suitable moment (the house using more than the sun
gives, the battery between its limits, no car charging) and runs only in waking hours, 08–21. It takes about ten
minutes. While it runs, your phones with SpotNav's notices get a message with a "Stop the test" button.

What it does, through the SolaX Modbus integration and its remote control. The integration holds the values and sends
them to the inverter with the Remote Control Trigger button; the Autorepeat Duration is set to 30 minutes, so the
remote control ends by itself if nothing renews it:

1. Hold: Power Control to Enabled No Discharge; the battery stops covering the house.
2. A small grid charge: Power Control to Enabled Battery Control with Active Power at a low charge power (at most
   2 kW, less if the fuse needs it).
3. Cover the house: Power Control to Enabled Self Use.
4. Hand back: Power Control to Disabled and sent, Active Power and Autorepeat Duration back to the values they had.

Since the button cannot be read back, each step is judged by the battery's power. It stops at once and hands back if
a write does not read back, if the battery does not do as told or if a phase comes near the main fuse. The result is
sent to me by itself when the test ends, while Beta tester is on.

**Stopping.** Press "Stop the test" in the notice or in the card's hardware test, at any time. To stop testing
altogether (nothing more is sent), untick Beta tester under Support.

**If something is left behind** (Home Assistant restarted mid-test, say), restore by hand in Home Assistant: Remote
Control Power Control to Disabled, press Remote Control Trigger, set Active Power and Autorepeat Duration to your
values, then turn SpotNav's battery switch off. The remote control also ends by itself when the Autorepeat Duration
runs out (30 minutes at most).

I read the test's log and answer you by e-mail.

SpotNav

---8<--- code
Subject: SpotNav: your control code for SolaX

Hi,

Thank you for running the hardware test. I have read its log and it looks right, so here is the control code for
your installation:

{code}

Paste it in the SpotNav card: Settings → Support → Hardware test → Control code, then Enter. Spaces and line breaks
do not matter. It works only on your Home Assistant installation and your SolaX. After that you can switch control
on in the home battery section ("Battery control").

What control does on your SolaX, through the integration's remote control: a hold is Enabled No Discharge, a grid
charge Enabled Battery Control at the planned charge power, covering the house Enabled Self Use and a sale Enabled
Battery Control with the power as a discharge. Each command is sent with the Remote Control Trigger and a 30-minute
Autorepeat Duration that SpotNav renews, so the remote control ends by itself within 30 minutes if Home Assistant
stops. Every hand-back sets Power Control to Disabled and sends it.

After a firmware or integration update SpotNav checks the battery again by itself; no new code is needed. If that
check fails, the card asks for the hardware test again.

For 30 days from now beta testing keeps sending one report a night, so I can see how control runs. Untick Beta tester
under Support to stop them at any time; the code keeps working.

**Restore by hand** if you ever need to: Remote Control Power Control to Disabled, press Remote Control Trigger, then
turn SpotNav's battery switch off.

SpotNav
