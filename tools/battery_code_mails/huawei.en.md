Subject: SpotNav: testing Huawei battery control

Hi,

Thank you for signing up to test SpotNav's control of your Huawei battery. Here is what happens next.

**Before the test.** The Huawei Solar integration must be logged in with the installer password: without it there
are neither the forcible charge services nor the battery's power limits. No forcible charge may be running. If
another system runs the battery (FusionSolar's own strategy, an energy manager), pause it first: the test refuses to
run otherwise.

**The hardware test.** In the SpotNav card: Settings → Support → Hardware test, then Start. That is all: there is
nothing else to do or to watch. The test waits by itself for a suitable moment (the house using more than the sun
gives, the battery between its limits, no car charging) and runs only in waking hours, 08–21. It takes about ten
minutes. While it runs, your phones with SpotNav's notices get a message with a "Stop the test" button.

What it does, through the Huawei Solar integration:

1. Hold: no forcible charge, and the battery's Maximum Discharging Power to 0; the battery stops covering the house
   (it still stores sun).
2. A small grid charge: a forcible charge at a low power (at most 2 kW, less if the fuse needs it) with a duration of
   30 minutes, so it ends by itself.
3. Cover the house: the forcible charge stopped, and Maximum Charging Power to 0; the battery covers the house and
   stores nothing.
4. Hand back: the forcible charge stopped, Maximum Charging Power and Maximum Discharging Power back to the values
   they had.

It stops at once and hands back if a write does not read back, if the battery does not do as told or if a phase
comes near the main fuse. The result is sent to me by itself when the test ends, while Beta tester is on.

**Stopping.** Press "Stop the test" in the notice or in the card's hardware test, at any time. To stop testing
altogether (nothing more is sent), untick Beta tester under Support.

**If something is left behind** (Home Assistant restarted mid-test, say), restore by hand in Home Assistant: run the
huawei_solar.stop_forcible_charge service for the battery, set Maximum Charging Power and Maximum Discharging Power to
your values (often the battery's maximum), then turn SpotNav's battery switch off. A forcible charge left standing
also ends by itself when its duration runs out (30 minutes at most).

I read the test's log and answer you by e-mail.

SpotNav

---8<--- code
Subject: SpotNav: your control code for Huawei

Hi,

Thank you for running the hardware test. I have read its log and it looks right, so here is the control code for
your installation:

{code}

Paste it in the SpotNav card: Settings → Support → Hardware test → Control code, then Enter. Spaces and line breaks
do not matter. It works only on your Home Assistant installation and your Huawei. After that you can switch control
on in the home battery section ("Battery control").

What control does on your Huawei: a hold sets Maximum Discharging Power to 0, covering the house sets Maximum
Charging Power to 0, a grid charge is a forcible charge at the planned power and a sale a forcible discharge, both
with a 30-minute duration that SpotNav renews; if Home Assistant stops they end by themselves within 30 minutes. Every
hand-back stops the force and sets both power limits back as they were.

After a firmware or integration update SpotNav checks the battery again by itself; no new code is needed. If that
check fails, the card asks for the hardware test again.

For 30 days from now beta testing keeps sending one report a night, so I can see how control runs. Untick Beta tester
under Support to stop them at any time; the code keeps working.

**Restore by hand** if you ever need to: run huawei_solar.stop_forcible_charge, set both power limits to your values,
then turn SpotNav's battery switch off.

SpotNav
