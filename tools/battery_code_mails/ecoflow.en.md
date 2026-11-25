Subject: SpotNav: testing EcoFlow PowerOcean battery control

Hi,

Thank you for signing up to test SpotNav's control of your EcoFlow PowerOcean. Here is what happens next.

**The hardware test.** In the SpotNav card: Settings → Support → Hardware test, then Start. That is all: there is
nothing else to do or to watch. The test waits by itself for a suitable moment (the house using more than the sun
gives, the battery between its limits, no car charging) and runs only in waking hours, 08–21. It takes about fifteen
minutes. While it runs, your phones with SpotNav's notices get a message with a "Stop the test" button. The EcoFlow app is
locked while SpotNav holds Modbus Control, and for about a minute after.

What it does, through the EF-PowerOcean-TcpModbus integration, only while the app runs self-consumption:

1. Hold: Modbus Control on, Battery Mode Hold Battery; the battery stands idle.
2. A small grid charge: Charge Battery at a low power (at most 2 kW, less if the fuse needs it).
3. Cover the house: Battery Mode Automatic.
4. The heartbeat: a hold again, then Modbus Control off and nothing else, timing how long the inverter keeps the hold
   before it covers the house by itself (about a minute is expected).
5. Hand back: Battery Mode Automatic, then Modbus Control off.

It stops at once and hands back if a write does not read back, if the battery does not do as told or if a phase
comes near the main fuse. The result is sent to me by itself when the test ends.

**Stopping.** Press "Stop the test" in the notice or in the card's hardware test, at any time. To stop testing altogether,
untick Beta tester under Support.

**If something is left behind** (Home Assistant restarted mid-test, say), restore by hand in Home Assistant: set
Battery Mode to Automatic, then turn Modbus Control off; after a minute the EcoFlow app works again. Then turn
SpotNav's battery switch off.

I read the test's log and answer you by e-mail.

SpotNav

---8<--- code
Subject: SpotNav: your control code for EcoFlow

Hi,

Thank you for running the hardware test. I have read its log and it looks right, so here is the control code for
your installation:

{code}

Paste it in the SpotNav card: Settings → Support → Hardware test → Control code, then Enter. Spaces and line breaks
do not matter. It works only on your Home Assistant installation and your EcoFlow. After that you can switch control
on in the home battery section ("Battery control").

What control does on your PowerOcean: it turns Modbus Control on and sets Battery Mode (Hold Battery, Charge Battery,
Automatic) through the integration, and hands back with Automatic and Modbus Control off. While SpotNav holds it, the
EcoFlow app is locked. If Home Assistant stops, the inverter's heartbeat runs out after about a minute and it goes
back to the app's own settings by itself.

After a firmware or integration update SpotNav checks the battery again by itself; no new code is needed. If that
check fails, the card asks for the hardware test again.

For 30 days from now beta testing keeps sending one report a night, so I can see how control runs. Untick Beta tester
under Support to stop them at any time; the code keeps working.

**Restore by hand** if you ever need to: Battery Mode to Automatic, then Modbus Control off, then turn SpotNav's
battery switch off.

SpotNav
