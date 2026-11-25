Subject: SpotNav: testing Tesla Powerwall control

Hi,

Thank you for signing up to test SpotNav's control of your Tesla Powerwall. Here is what happens next.

**Before the test.** Control goes through Tesla's cloud (the Tesla Fleet or Teslemetry integration). The Powerwall
must be in Self-Powered; Time-Based Control and Backup are left alone. Turn on the battery's size sensor (pack
energy) if it is disabled: without it no charge starts. It helps to note the Backup Reserve and the grid charging
switch before. If another system runs the battery, pause it first: the test refuses to run otherwise.

**The hardware test.** In the SpotNav card: Settings → Support → Hardware test, then Start. That is all: there is
nothing else to do or to watch. The test waits by itself for a suitable moment (the house using more than the sun
gives, the battery between its limits, no car charging) and runs only in waking hours, 08–21. Tesla's cloud is slow:
a change can take several minutes to show, and SpotNav waits a quarter of an hour between two steps. So the test
takes between half an hour and an hour, never more than 75 minutes. Meanwhile no car charge starts on the site, and
your phones with SpotNav's notices get a message with a "Stop the test" button.

What it does, through Tesla's cloud:

1. Hold: grid charging off, and the Backup Reserve two points under the level now (rounded down), so the Powerwall
   neither covers the house nor charges from the grid.
2. A grid charge: grid charging on, and the Backup Reserve as high as the Powerwall reaches in 30 minutes. The charge
   power is the Powerwall's own (SpotNav counts 5 kW per Powerwall) and cannot be lowered. If it does not fit under
   the fuse, the test skips the step and can still pass; control then charges from the grid only once SpotNav's own
   check has later seen a charge that fits.
3. Cover the house: the Backup Reserve and grid charging back as they were; the Powerwall covers the house itself.
4. Hand back: the Backup Reserve first, then grid charging, back to the values they had. For 15 minutes after,
   SpotNav watches that the cloud keeps them.

Tesla has no dead-man: if Home Assistant or the cloud stops during the charge, at most those 30 minutes are charged,
never to 100 %. It stops at once and hands back if a write does not read back, if the battery does not do as told or
if a phase comes near the main fuse. The result is sent to me by itself when the test ends, while Beta tester is on.

**Stopping.** Press "Stop the test" in the notice or in the card's hardware test, at any time. To stop testing
altogether (nothing more is sent), untick Beta tester under Support.

**If something is left behind** (Home Assistant restarted mid-test, say), restore by hand in the Tesla app or Home
Assistant: the Backup Reserve to your value and grid charging (the "allow charging from grid" switch, on a site with
solar) as it was, then turn SpotNav's battery switch off.

I read the test's log and answer you by e-mail.

SpotNav

---8<--- code
Subject: SpotNav: your control code for Tesla Powerwall

Hi,

Thank you for running the hardware test. I have read its log and it looks right, so here is the control code for
your installation:

{code}

Paste it in the SpotNav card: Settings → Support → Hardware test → Control code, then Enter. Spaces and line breaks
do not matter. It works only on your Home Assistant installation and your Tesla. After that you can switch control
on in the home battery section ("Battery control").

What control does on your Powerwall, only in Self-Powered and only the plan's own steps (no hold for the car, at most
24 writes a day, no new step within 15 minutes): a hold turns grid charging off and sets the Backup Reserve two
points under the level, a grid charge turns grid charging on and sets the reserve as high as the Powerwall reaches in
30 minutes, renewed every 30 minutes. Covering the house is the Powerwall's own mode. It never sells from the battery.
If Home Assistant or the cloud stops, at most one such block is charged from the grid, and the reserve stays there
until it is set back. Every hand-back sets the reserve and grid charging back as they were.

After a firmware or integration update SpotNav checks the battery again by itself; no new code is needed. If that
check fails, the card asks for the hardware test again.

For 30 days from now beta testing keeps sending one report a night, so I can see how control runs. Untick Beta tester
under Support to stop them at any time; the code keeps working.

**Restore by hand** if you ever need to: the Backup Reserve to your value and grid charging as it was, then turn
SpotNav's battery switch off.

SpotNav
