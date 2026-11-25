Subject: SpotNav: testing Solinteg battery control

Hi,

Thank you for signing up to test SpotNav's control of your Solinteg battery (M-TEC Energy Butler and Wattsonic
too). Here is what happens next.

**Before the test.** Working Mode must read General and the EMS BattCtrl Charge Discharge Power Target 0. The dongle
answers one Modbus client only: run no other Modbus tool beside the integration, and set the integration's timeout to
10 to 12 seconds. If another system runs the battery (the Solinteg app, the cloud, Samvolt, CheckWatt, Enequi), pause
it first: the test refuses to run otherwise.

**The hardware test.** In the SpotNav card: Settings → Support → Hardware test, then Start. That is all: there is
nothing else to do or to watch. The test waits by itself for a suitable moment (the house using more than the sun
gives, the battery between its limits, no car charging) and runs only in waking hours, 08–21. It takes about ten
minutes. While it runs, your phones with SpotNav's notices get a message with a "Stop the test" button.

What it does, through the Solinteg plugin of SolaX Modbus. The mode is always written before the power:

1. Hold: Working Mode to EMS BattCtrl with the power target at 0; the battery stands idle (a sun surplus is
   exported).
2. A small grid charge: EMS BattCtrl with the power target at minus a low power (negative charges; at most 2 kW,
   less if the fuse needs it).
3. Cover the house: Working Mode to General, the inverter's own mode.
4. Hand back: Working Mode to General and the power target to 0.

The inverter keeps the last command if Home Assistant stops (no timeout is known), so the grid charge stays under
SpotNav's cap for the fuse. It stops at once and hands back if a write does not read back, if the battery does not
do as told or if a phase comes near the main fuse. The result is sent to me by itself when the test ends, while Beta
tester is on.

**Stopping.** Press "Stop the test" in the notice or in the card's hardware test, at any time. To stop testing
altogether (nothing more is sent), untick Beta tester under Support.

**If something is left behind** (Home Assistant restarted mid-test, say), restore by hand in Home Assistant: Working
Mode to General and EMS BattCtrl Charge Discharge Power Target (and the three EMS ACCtrl phase numbers) to 0, then
turn SpotNav's battery switch off.

I read the test's log and answer you by e-mail.

SpotNav

---8<--- code
Subject: SpotNav: your control code for Solinteg

Hi,

Thank you for running the hardware test. I have read its log and it looks right, so here is the control code for
your installation:

{code}

Paste it in the SpotNav card: Settings → Support → Hardware test → Control code, then Enter. Spaces and line breaks
do not matter. It works only on your Home Assistant installation and your Solinteg. After that you can switch control
on in the home battery section ("Battery control").

What control does on your Solinteg, only when it finds the inverter in General: a hold is EMS BattCtrl with the power
target at 0, a grid charge EMS BattCtrl with the target at minus the planned power, a sale EMS BattCtrl with the
target at the power, and covering the house is General. The inverter keeps the last command if Home Assistant stops,
so a grid charge always stays under the cap for the fuse, counted as if all of it were on one phase. Every hand-back
sets Working Mode to General and the target to 0. If the app, the cloud or another system changes the mode while
SpotNav runs it, SpotNav hands back and stops.

After a firmware or integration update SpotNav checks the battery again by itself; no new code is needed. If that
check fails, the card asks for the hardware test again.

For 30 days from now beta testing keeps sending one report a night, so I can see how control runs. Untick Beta tester
under Support to stop them at any time; the code keeps working.

**Restore by hand** if you ever need to: Working Mode to General and the power target (and the phase numbers) to 0,
then turn SpotNav's battery switch off.

SpotNav
