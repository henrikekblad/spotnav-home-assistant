Subject: SpotNav: testing FoxESS battery control

Hi,

Thank you for signing up to test SpotNav's control of your FoxESS battery. Here is what happens next.

**The hardware test.** In the SpotNav card: Settings → Support → Hardware test, then Start. That is all: there is
nothing else to do or to watch. The test waits by itself for a suitable moment (the house using more than the sun
gives, the battery between its limits, no car charging) and runs only in waking hours, 08–21. It takes about ten
minutes. While it runs, your phones with SpotNav's notices get a message with a "Stop the test" button.

What it does on a FoxESS, through the foxess_modbus integration and only while the work mode reads Self Use:

1. Hold: Max Discharge Current to 0, so the battery stops covering the house.
2. The grid charge is skipped: SpotNav never charges a FoxESS from the grid.
3. Cover the house: the limits back, the battery covers the house again.
4. Hand back: Max Charge Current and Max Discharge Current to the values they had.

It stops at once and hands back if a write does not read back, if the battery does not do as told or if a phase
comes near the main fuse. The result is sent to me by itself when the test ends, while Beta tester is on.

**Stopping.** Press "Stop the test" in the notice or in the card's hardware test, at any time. To stop testing altogether (nothing more is sent),
untick Beta tester under Support.

**If something is left behind** (Home Assistant restarted mid-test, say), restore by hand in Home Assistant: set Max
Charge Current and Max Discharge Current back to your values (often the battery's maximum), then turn SpotNav's
battery switch off.

I read the test's log and answer you by e-mail.

SpotNav

---8<--- code
Subject: SpotNav: your control code for FoxESS

Hi,

Thank you for running the hardware test. I have read its log and it looks right, so here is the control code for
your installation:

{code}

Paste it in the SpotNav card: Settings → Support → Hardware test → Control code, then Enter. Spaces and line breaks
do not matter. It works only on your Home Assistant installation and your FoxESS. After that you can switch control
on in the home battery section ("Battery control").

What control does on your FoxESS: it only sets the two current limits (Max Charge Current and Max Discharge Current),
and only while the work mode reads Self Use. It never charges from the grid and never sells. If Home Assistant stops,
a limit SpotNav set stays until Home Assistant is back; the battery then covers less of the house, it never takes more
from the grid because of it.

After a firmware or integration update SpotNav checks the battery again by itself; no new code is needed. If that
check fails, the card asks for the hardware test again.

For 30 days from now beta testing keeps sending one report a night, so I can see how control runs. Untick Beta tester
under Support to stop them at any time; the code keeps working.

**Restore by hand** if you ever need to: Max Charge Current and Max Discharge Current back to your values, then turn
SpotNav's battery switch off.

SpotNav
