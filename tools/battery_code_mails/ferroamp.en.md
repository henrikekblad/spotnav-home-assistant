Subject: SpotNav: testing Ferroamp EnergyHub control

Hi,

Thank you for signing up to test SpotNav's control of your Ferroamp EnergyHub. Here is what happens next.

**Before the test.** Control goes through the ferroamp integration and Ferroamp's MQTT API, which Ferroamp's support
enables. The hub's strategy in EnergyCloud (peak shaving, FerroAI, a grid service) is not shown in Home Assistant:
the checklist asks you to accept that SpotNav's requests override it while SpotNav controls the battery, and nothing
is sent before you do. No other request may be standing (Ferroamp's app, an automation). If the hub takes part in a
grid service, leave control off.

**The hardware test.** In the SpotNav card: Settings → Support → Hardware test, then Start. That is all: there is
nothing else to do or to watch. The test waits by itself for a suitable moment (the house using more than the sun
gives, the battery between its limits, no car charging) and runs only in waking hours, 08–21. It takes about ten
minutes. While it runs, your phones with SpotNav's notices get a message with a "Stop the test" button.

What it does, through the integration's services. A request counts as taken only once the hub acknowledged it:

1. Hold: a charge request of 0 W; the battery must stop covering the house. Ferroamp does not document it, so the hub
   may refuse it; a later test also tries a discharge request of 0 W.
2. A small grid charge: a charge request at a low power (in steps of 500 W, at most 2 kW, less if the fuse needs it).
3. Cover the house: autocharge, the hub's own mode.
4. Hand back: autocharge.

A request has no timeout and stays if Home Assistant stops, so the grid charge stays under SpotNav's cap for the fuse,
counted as if all of it were on one phase. It stops at once and hands back if a request is not acknowledged, if the
battery does not do as told or if a phase comes near the main fuse. The result is sent to me by itself when the test
ends, while Beta tester is on.

**Stopping.** Press "Stop the test" in the notice or in the card's hardware test, at any time. To stop testing
altogether (nothing more is sent), untick Beta tester under Support.

**If something is left behind** (Home Assistant restarted mid-test, say), restore by hand in Home Assistant: run the
ferroamp.autocharge service for the hub, then turn SpotNav's battery switch off.

I read the test's log and answer you by e-mail.

SpotNav

---8<--- code
Subject: SpotNav: your control code for Ferroamp

Hi,

Thank you for running the hardware test. I have read its log and it looks right, so here is the control code for
your installation:

{code}

Paste it in the SpotNav card: Settings → Support → Hardware test → Control code, then Enter. Spaces and line breaks
do not matter. It works only on your Home Assistant installation and your Ferroamp. After that you can switch control
on in the home battery section ("Battery control").

What control does on your EnergyHub: a hold is a charge request of 0 W (or a discharge request of 0 W, whichever the
hardware test passed), a grid charge a charge request at the planned power and a sale a discharge request, both for
the whole system in steps of 500 W. Covering the house and every hand-back are autocharge, which gives the hub back to
its own strategy. A request stays if Home Assistant stops, so a grid charge always stays under the cap for the fuse.
If someone else's request is standing, SpotNav does not start.

After a firmware or integration update SpotNav checks the battery again by itself; no new code is needed. If that
check fails, the card asks for the hardware test again.

For 30 days from now beta testing keeps sending one report a night, so I can see how control runs. Untick Beta tester
under Support to stop them at any time; the code keeps working.

**Restore by hand** if you ever need to: run ferroamp.autocharge for the hub, then turn SpotNav's battery switch off.

SpotNav
