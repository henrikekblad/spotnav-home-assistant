Subject: SpotNav: testing Deye / Sunsynk battery control

Hi,

Thank you for signing up to test SpotNav's control of your Deye / Sunsynk battery. Here is what happens next.

**Before the test.** SpotNav takes the battery only while the inverter's *Time of Use* reads Disabled (off): with
Time of Use on, the inverter runs its own programmes and SpotNav only watches. If you use the Sunsynk add-on
(kellerza), add the sensor group "battery" (or "advanced") to its SENSORS option, so that *Battery Max Charge
current* and *Battery Max Discharge current* reach Home Assistant. With the Solarman integration (davidrapan) they
are there already.

**The hardware test.** In the SpotNav card: Settings → Support → Hardware test, then Start. That is all: there is
nothing else to do or to watch. The test waits by itself for a suitable moment (the house using more than the sun
gives, the battery between its limits, no car charging) and runs only in waking hours, 08–21. It takes about ten
minutes. While it runs, your phones with SpotNav's notices get a message with a "Stop the test" button.

What it does on a Deye / Sunsynk, and only while Time of Use is off:

1. Hold: Battery Max Discharging Current to 0 A, so the battery stops covering the house.
2. The grid charge is skipped: SpotNav never charges a Deye from the grid and never changes your time-of-use
   programmes.
3. Cover the house: the discharge limit back and Battery Max Charging Current to 0 A, so the battery covers the
   house and stores nothing.
4. Hand back: Battery Max Charging Current and Battery Max Discharging Current to the values they had.

It stops at once and hands back if a write does not read back, if the battery does not do as told or if a phase
comes near the main fuse. The result is sent to me by itself when the test ends, while Beta tester is on.

**Stopping.** Press "Stop the test" in the notice or in the card's hardware test, at any time. To stop testing
altogether (nothing more is sent), untick Beta tester under Support.

**If something is left behind** (Home Assistant restarted mid-test, say), restore by hand in Home Assistant: set
Battery Max Charging Current and Battery Max Discharging Current back to your values (often the battery's maximum),
then turn SpotNav's battery switch off.

Three things that help me read the log, if you know them: the inverter's model (for example SUN-12K-SG04LP3-EU) and
firmware, which integration and version you use, and whether anything else writes to the inverter (the Deye or
Sunsynk app's schedule, Predbat, evcc, Solar Assistant).

I read the test's log and answer you by e-mail.

SpotNav

---8<--- code
Subject: SpotNav: your control code for Deye / Sunsynk

Hi,

Thank you for running the hardware test. I have read its log and it looks right, so here is the control code for
your installation:

{code}

Paste it in the SpotNav card: Settings → Support → Hardware test → Control code, then Enter. Spaces and line breaks
do not matter. It works only on your Home Assistant installation and your Deye / Sunsynk. After that you can switch
control on in the home battery section ("Battery control").

What control does on your Deye / Sunsynk: it only sets the two current limits (Battery Max Charging Current and
Battery Max Discharging Current), and only while Time of Use is off. It never charges from the grid, never sells and
never changes your time-of-use programmes. It writes only when the plan changes what the battery should do, at most
48 times a day, since nobody states how often the inverter may be written. If Home Assistant stops, a limit SpotNav
set stays until Home Assistant is back; the battery then covers less of the house, it never takes more from the grid
because of it.

After a firmware or integration update SpotNav checks the battery again by itself; no new code is needed. If that
check fails, the card asks for the hardware test again.

For 30 days from now beta testing keeps sending one report a night, so I can see how control runs. Untick Beta tester
under Support to stop them at any time; the code keeps working.

**Restore by hand** if you ever need to: Battery Max Charging Current and Battery Max Discharging Current back to
your values, then turn SpotNav's battery switch off.

SpotNav
