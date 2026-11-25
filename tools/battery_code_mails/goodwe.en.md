Subject: SpotNav: testing GoodWe battery control

Hi,

Thank you for signing up to test SpotNav's control of your GoodWe battery. Here is what happens next.

**Before the test.** Control needs the HACS integration (mletenay/home-assistant-goodwe-inverter): the integration in
Home Assistant core has no eco charge mode or eco values. Operation Mode must read General. It helps to note
Operation Mode, Eco Mode Power, Eco Mode SoC and Depth of Discharge (on grid) before. If another system runs the
battery, pause it first: the test refuses to run otherwise.

**The hardware test.** In the SpotNav card: Settings → Support → Hardware test, then Start. That is all: there is
nothing else to do or to watch. The test waits by itself for a suitable moment (the house using more than the sun
gives, the battery between its limits, no car charging) and runs only in waking hours, 08–21. It takes about ten
minutes. While it runs, your phones with SpotNav's notices get a message with a "Stop the test" button.

What it does, through the HACS integration for GoodWe:

1. Hold: Depth of Discharge (on grid) so that the cut-off is the level now; the battery must stop covering the house.
   This is not confirmed on an inverter: if the battery charges from the grid instead, it is handed back.
2. A small grid charge: Eco Mode SoC to 100, Eco Mode Power to a share of the battery's rated power (at most 2 kW,
   less if the fuse needs it) and Operation Mode to Eco Charge. The charge is judged by the battery's power, since
   some firmwares ignore the SoC ceiling.
3. Cover the house: Operation Mode to General.
4. Hand back: Operation Mode, Eco Mode Power, Eco Mode SoC and Depth of Discharge back to the values they had.

None of this ends by itself if Home Assistant stops, so the grid charge stays under SpotNav's cap for the fuse. The
integration does not poll these values, so a change shows only in Home Assistant's own echo. It stops at once and
hands back if a write does not read back, if the battery does not do as told or if a phase comes near the main fuse.
The result is sent to me by itself when the test ends, while Beta tester is on.

**Stopping.** Press "Stop the test" in the notice or in the card's hardware test, at any time. To stop testing
altogether (nothing more is sent), untick Beta tester under Support.

**If something is left behind** (Home Assistant restarted mid-test, say), restore by hand in Home Assistant:
Operation Mode to General (or your own mode), Depth of Discharge (on grid) to your value and Eco Mode Power and Eco
Mode SoC to what they were, then turn SpotNav's battery switch off.

I read the test's log and answer you by e-mail.

SpotNav

---8<--- code
Subject: SpotNav: your control code for GoodWe

Hi,

Thank you for running the hardware test. I have read its log and it looks right, so here is the control code for
your installation:

{code}

Paste it in the SpotNav card: Settings → Support → Hardware test → Control code, then Enter. Spaces and line breaks
do not matter. It works only on your Home Assistant installation and your GoodWe. After that you can switch control
on in the home battery section ("Battery control").

What control does on your GoodWe: a hold sets Depth of Discharge so the cut-off is the level now, a grid charge is
Eco Charge with Eco Mode SoC at 100 and Eco Mode Power at a share of the rated power, and covering the house is
General. It never sells from the battery. Nothing ends by itself if Home Assistant stops, so a grid charge always
stays under the cap for the fuse and ends with the plan's quarter hour. A hold or a charge that takes more from the
grid than it was given is handed back and not tried again for six hours. Every hand-back sets the mode, the eco values
and Depth of Discharge back as they were.

After a firmware or integration update SpotNav checks the battery again by itself; no new code is needed. If that
check fails, the card asks for the hardware test again.

For 30 days from now beta testing keeps sending one report a night, so I can see how control runs. Untick Beta tester
under Support to stop them at any time; the code keeps working.

**Restore by hand** if you ever need to: Operation Mode to General, Depth of Discharge to your value and the eco
values to what they were, then turn SpotNav's battery switch off.

SpotNav
