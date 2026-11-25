Subject: SpotNav: testing SolarEdge battery control

Hi,

Thank you for signing up to test SpotNav's control of your SolarEdge battery. Here is what happens next.

**Before the test.** The Storage Control Mode must already read Remote Control (SpotNav never changes it), and for
the grid charge the AC Charge Policy must not be Disabled. If another system runs the battery (Predbat, an energy
manager), pause it first: the test refuses to run otherwise.

**The hardware test.** In the SpotNav card: Settings → Support → Hardware test, then Start. That is all: there is
nothing else to do or to watch. The test waits by itself for a suitable moment (the house using more than the sun
gives, the battery between its limits, no car charging) and runs only in waking hours, 08–21. It takes about ten
minutes. While it runs, your phones with SpotNav's notices get a message with a "Stop the test" button.

What it does, through the SolarEdge Modbus Multi integration and only in Remote Control. Each command is written with
a Storage Command Timeout of 30 minutes, so the inverter goes back to its own mode if nothing renews it:

1. Hold: Storage Command Mode to Charge from Solar Power; the battery stops covering the house (it still stores sun).
2. A small grid charge: Storage Charge Limit to a low power (at most 2 kW, less if the fuse needs it) and Storage
   Command Mode to Charge from Solar Power and Grid.
3. Cover the house: Storage Command Mode to Discharge to Minimize Import.
4. Hand back: Storage Command Mode, Storage Charge Limit, Storage Discharge Limit and Storage Command Timeout back to
   the values they had.

It stops at once and hands back if a write does not read back, if the battery does not do as told or if a phase
comes near the main fuse. The result is sent to me by itself when the test ends, while Beta tester is on.

**Stopping.** Press "Stop the test" in the notice or in the card's hardware test, at any time. To stop testing
altogether (nothing more is sent), untick Beta tester under Support.

**If something is left behind** (Home Assistant restarted mid-test, say), restore by hand in Home Assistant: Storage
Command Mode, Storage Charge Limit, Storage Discharge Limit and Storage Command Timeout to your values, then turn
SpotNav's battery switch off. A command left standing also ends by itself when the Storage Command Timeout runs out
(30 minutes at most).

I read the test's log and answer you by e-mail.

SpotNav

---8<--- code
Subject: SpotNav: your control code for SolarEdge

Hi,

Thank you for running the hardware test. I have read its log and it looks right, so here is the control code for
your installation:

{code}

Paste it in the SpotNav card: Settings → Support → Hardware test → Control code, then Enter. Spaces and line breaks
do not matter. It works only on your Home Assistant installation and your SolarEdge. After that you can switch
control on in the home battery section ("Battery control").

What control does on your SolarEdge, only in Remote Control: a hold is Charge from Solar Power, a grid charge Charge
from Solar Power and Grid with the charge limit at the planned power, covering the house Discharge to Minimize Import
and a sale Discharge to Maximize Export with the discharge limit at the power. Each command is written with a
30-minute Storage Command Timeout and renewed every fifteen minutes, so the inverter goes back to its own mode within
30 minutes if Home Assistant stops. Every hand-back sets the mode, the limits and the timeout back as they were.

After a firmware or integration update SpotNav checks the battery again by itself; no new code is needed. If that
check fails, the card asks for the hardware test again.

For 30 days from now beta testing keeps sending one report a night, so I can see how control runs. Untick Beta tester
under Support to stop them at any time; the code keeps working.

**Restore by hand** if you ever need to: Storage Command Mode, the charge limit, the discharge limit and the Storage
Command Timeout to your values, then turn SpotNav's battery switch off.

SpotNav
