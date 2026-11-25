Ämne: SpotNav: test av styrningen av ditt Solinteg-batteri

Hej,

Tack för att du vill testa SpotNavs styrning av ditt Solinteg-batteri (gäller även M-TEC Energy Butler och
Wattsonic). Så här går det till.

**Före testet.** Working Mode ska stå på General och EMS BattCtrl Charge Discharge Power Target på 0. Dongeln
svarar bara en Modbus-klient: kör inget annat Modbus-verktyg bredvid integrationen, och sätt integrationens timeout
till 10–12 sekunder. Styr ett annat system batteriet (Solinteg-appen, molnet, Samvolt, CheckWatt, Enequi), pausa det
först: testet vägrar annars att köras.

**Hårdvarutestet.** I SpotNav-kortet: Inställningar → Support → Hårdvarutest, tryck sedan på Start. Det är allt: du
behöver inte göra eller titta på något mer. Testet väntar själv på ett lämpligt tillfälle (huset drar mer än solen
ger, batteriet ligger mellan sina gränser, ingen bil laddar) och körs bara dagtid, 08–21. Det tar ungefär tio
minuter. Medan det pågår får dina telefoner med SpotNavs aviseringar ett meddelande med knappen "Stoppa testet".

Vad det gör, via Solinteg-pluginen i SolaX Modbus. Läget skrivs alltid före effekten:

1. Hålla: Working Mode till EMS BattCtrl med effektmålet på 0; batteriet står stilla (ett solöverskott exporteras).
2. En liten nätladdning: EMS BattCtrl med effektmålet på minus en låg effekt (negativt laddar; högst 2 kW, mindre om
   säkringen kräver det).
3. Täcka huset: Working Mode till General, växelriktarens eget läge.
4. Lämna tillbaka: Working Mode till General och effektmålet till 0.

Växelriktaren behåller det senaste kommandot om Home Assistant stannar (ingen timeout är känd), så nätladdningen
hålls under SpotNavs tak för säkringen. Testet avbryts direkt och lämnar tillbaka om en skrivning inte läses
tillbaka, om batteriet inte gör som det blir tillsagt eller om en fas närmar sig huvudsäkringen. Resultatet skickas
till mig av sig självt när testet är klart, så länge Betatestare är på.

**Avbryta.** Tryck på "Stoppa testet" i aviseringen, eller "Avbryt testet" i kortets hårdvarutest, när som helst.
Vill du sluta testa helt (då skickas inget mer): avmarkera Betatestare under Support.

**Om något blir kvar** (till exempel om Home Assistant startade om mitt i testet), återställ för hand i Home
Assistant: Working Mode till General och EMS BattCtrl Charge Discharge Power Target (och de tre fasvärdena under EMS
ACCtrl) till 0, och slå sedan av SpotNavs batteriomkopplare.

Jag läser testets logg och svarar dig via e-post.

SpotNav

---8<--- code
Ämne: SpotNav: din kontrollkod för Solinteg

Hej,

Tack för att du körde hårdvarutestet. Jag har läst loggen och det ser bra ut, så här är kontrollkoden för din
installation:

{code}

Klistra in den i SpotNav-kortet: Inställningar → Support → Hårdvarutest → Kontrollkod, tryck sedan på Skriv in.
Mellanslag och radbrytningar spelar ingen roll. Koden gäller bara din Home Assistant-installation och din Solinteg.
Sedan kan du slå på styrningen i avsnittet Hembatteri ("Batteristyrning").

Vad styrningen gör på din Solinteg, bara när den hittar växelriktaren i General: en hållning är EMS BattCtrl med
effektmålet på 0, en nätladdning EMS BattCtrl med målet på minus den planerade effekten, en försäljning EMS BattCtrl
med målet på effekten, och att täcka huset är General. Växelriktaren behåller det senaste kommandot om Home Assistant
stannar, så en nätladdning hålls alltid under taket för säkringen, räknad som om allt gick på en fas. Varje
återlämning sätter Working Mode till General och målet till 0. Ändrar appen, molnet eller ett annat system läget
medan SpotNav styr lämnar SpotNav tillbaka och slutar.

Efter en uppdatering av firmware eller integration kontrollerar SpotNav batteriet själv; ingen ny kod behövs. Går
den kontrollen inte igenom ber kortet om hårdvarutestet igen.

I 30 dagar från nu skickar betatestet en rapport per natt, så att jag kan se hur styrningen går. Avmarkera
Betatestare under Support för att stoppa dem när du vill; koden fortsätter att gälla.

**Återställ för hand** om du någon gång behöver: Working Mode till General och effektmålet (och fasvärdena) till 0,
och slå sedan av SpotNavs batteriomkopplare.

SpotNav
