Ämne: SpotNav: test av styrningen av ditt SolaX-batteri

Hej,

Tack för att du vill testa SpotNavs styrning av ditt SolaX-batteri. Så här går det till.

**Före testet.** Remote Control Power Control ska stå på Disabled (ingen annan använder fjärrstyrningen). Styr ett
annat system batteriet (Predbat, en energistyrning, SolaX Cloud), pausa det först: testet vägrar annars att köras.

**Hårdvarutestet.** I SpotNav-kortet: Inställningar → Support → Hårdvarutest, tryck sedan på Start. Det är allt: du
behöver inte göra eller titta på något mer. Testet väntar själv på ett lämpligt tillfälle (huset drar mer än solen
ger, batteriet ligger mellan sina gränser, ingen bil laddar) och körs bara dagtid, 08–21. Det tar ungefär tio
minuter. Medan det pågår får dina telefoner med SpotNavs aviseringar ett meddelande med knappen "Stoppa testet".

Vad det gör, via integrationen SolaX Modbus och dess fjärrstyrning (remote control). Värdena hålls av integrationen
och skickas till växelriktaren med knappen Remote Control Trigger; Autorepeat Duration sätts till 30 minuter, så att
fjärrstyrningen slutar av sig själv om ingen förnyar den:

1. Hålla: Power Control till Enabled No Discharge; batteriet slutar täcka huset.
2. En liten nätladdning: Power Control till Enabled Battery Control med Active Power på en låg laddeffekt (högst
   2 kW, mindre om säkringen kräver det).
3. Täcka huset: Power Control till Enabled Self Use.
4. Lämna tillbaka: Power Control till Disabled och skickat, Active Power och Autorepeat Duration till de värden de
   hade.

Eftersom knappen inte kan läsas tillbaka bedöms varje steg på batteriets effekt. Testet avbryts direkt och lämnar
tillbaka om en skrivning inte läses tillbaka, om batteriet inte gör som det blir tillsagt eller om en fas närmar sig
huvudsäkringen. Resultatet skickas till mig av sig självt när testet är klart, så länge Betatestare är på.

**Avbryta.** Tryck på "Stoppa testet" i aviseringen, eller "Avbryt testet" i kortets hårdvarutest, när som helst.
Vill du sluta testa helt (då skickas inget mer): avmarkera Betatestare under Support.

**Om något blir kvar** (till exempel om Home Assistant startade om mitt i testet), återställ för hand i Home
Assistant: Remote Control Power Control till Disabled, tryck på Remote Control Trigger, sätt Active Power och
Autorepeat Duration till dina värden, och slå sedan av SpotNavs batteriomkopplare. Fjärrstyrningen slutar också av
sig själv när Autorepeat Duration har gått ut (högst 30 minuter).

Jag läser testets logg och svarar dig via e-post.

SpotNav

---8<--- code
Ämne: SpotNav: din kontrollkod för SolaX

Hej,

Tack för att du körde hårdvarutestet. Jag har läst loggen och det ser bra ut, så här är kontrollkoden för din
installation:

{code}

Klistra in den i SpotNav-kortet: Inställningar → Support → Hårdvarutest → Kontrollkod, tryck sedan på Skriv in.
Mellanslag och radbrytningar spelar ingen roll. Koden gäller bara din Home Assistant-installation och din SolaX.
Sedan kan du slå på styrningen i avsnittet Hembatteri ("Batteristyrning").

Vad styrningen gör på din SolaX, genom integrationens fjärrstyrning: en hållning är Enabled No Discharge, en
nätladdning Enabled Battery Control med den planerade laddeffekten, att täcka huset Enabled Self Use och en
försäljning Enabled Battery Control med effekten som urladdning. Varje kommando skickas med Remote Control Trigger och
en Autorepeat Duration på 30 minuter som SpotNav förnyar, så fjärrstyrningen slutar av sig själv inom 30 minuter om
Home Assistant stannar. Varje återlämning sätter Power Control till Disabled och skickar det.

Efter en uppdatering av firmware eller integration kontrollerar SpotNav batteriet själv; ingen ny kod behövs. Går
den kontrollen inte igenom ber kortet om hårdvarutestet igen.

I 30 dagar från nu skickar betatestet en rapport per natt, så att jag kan se hur styrningen går. Avmarkera
Betatestare under Support för att stoppa dem när du vill; koden fortsätter att gälla.

**Återställ för hand** om du någon gång behöver: Remote Control Power Control till Disabled, tryck på Remote Control
Trigger, och slå sedan av SpotNavs batteriomkopplare.

SpotNav
