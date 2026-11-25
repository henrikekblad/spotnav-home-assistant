Subject: SpotNav: testing Solis battery control

Hi,

Thank you for signing up to test SpotNav's control of your Solis battery. Here is what happens next.

**Before the test.** If another system runs the battery (an energy manager such as Enequi, or SolisCloud's own
strategy), pause it first: SpotNav never writes while something else drives the inverter's remote-control force,
and the test refuses to run. Once SpotNav has seen another system's force it waits 48 hours before it takes the
battery again, unless you press "It was me" in the card. Time-of-use periods must be off too.

**The hardware test.** In the SpotNav card: Settings → Support → Hardware test, then Start. That is all: there is
nothing else to do or to watch. The test waits by itself for a suitable moment (the house using more than the sun
gives, the battery between its limits, no car charging) and runs only in waking hours, 08–21. It takes about twenty
minutes. While it runs, your phones with SpotNav's notices get a message with a "Stop the test" button.

What it does, through the Solis Modbus integration, only in the Storage Mode Self-Use with no force running:

1. Hold: RC force charge at 0 W, with the RC Timeout at 5 minutes; the battery stops covering the house (it still
   stores sun).
2. A small grid charge: RC force charge at a low power (at most 2 kW, within the BMS's charge limit, less if the fuse
   needs it).
3. Cover the house: the force stopped, the inverter's own Self-Use.
4. Stops by itself: a hold again, renewed once, then left alone, timing how long the inverter keeps it before it
   covers the house by itself (about five minutes are expected).
5. Hand back: the force stopped, both force powers at 0 and the RC Timeout as it was.

It stops at once and hands back if a write does not read back, if the battery does not do as told or if a phase
comes near the main fuse. The result is sent to me by itself when the test ends, while Beta tester is on.

**Stopping.** Press "Stop the test" in the notice or in the card's hardware test, at any time. To stop testing
altogether (nothing more is sent), untick Beta tester under Support.

**If something is left behind** (Home Assistant restarted mid-test, say), restore by hand in Home Assistant: set RC
Force Charge/Discharge to None, RC Force Battery Charge Power and RC Force Battery Discharge Power to 0 and RC Timeout
to your value (5 minutes by default), then turn SpotNav's battery switch off. A force left standing also ends by
itself when the RC Timeout runs out.

I read the test's log and answer you by e-mail.

SpotNav

---8<--- code
Subject: SpotNav: your control code for Solis

Hi,

Thank you for running the hardware test. I have read its log and it looks right, so here is the control code for
your installation:

{code}

Paste it in the SpotNav card: Settings → Support → Hardware test → Control code, then Enter. Spaces and line breaks
do not matter. It works only on your Home Assistant installation and your Solis. After that you can switch control
on in the home battery section ("Battery control").

What control does on your Solis: a hold is an RC force charge at 0 W, a grid charge an RC force charge at the rate
(within the BMS's charge limit), a sale an RC force discharge, each sent with its power and a 5-minute RC Timeout in
one call; SpotNav renews the force every two minutes, so it ends by itself within five minutes if Home Assistant
stops. Every hand-back stops the force and sets both force powers to 0. While another system drives the force,
SpotNav only watches.

After a firmware or integration update SpotNav checks the battery again by itself; no new code is needed. If that
check fails, the card asks for the hardware test again.

For 30 days from now beta testing keeps sending one report a night, so I can see how control runs. Untick Beta tester
under Support to stop them at any time; the code keeps working.

**Restore by hand** if you ever need to: RC Force Charge/Discharge to None, both force powers to 0, RC Timeout to your
value, then turn SpotNav's battery switch off.

SpotNav
