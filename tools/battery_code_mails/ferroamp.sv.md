Ämne: SpotNav: test av styrningen av din Ferroamp EnergyHub

Hej,

Tack för att du vill testa SpotNavs styrning av din Ferroamp EnergyHub. Så här går det till.

**Före testet.** Styrningen går via integrationen ferroamp och Ferroamps MQTT-gränssnitt, som Ferroamps support slår
på. Hubbens strategi i EnergyCloud (toppkapning, FerroAI, en nättjänst) syns inte i Home Assistant: checklistan ber
dig godkänna att SpotNavs förfrågningar går före den medan SpotNav styr, och inget skickas innan du har gjort det.
Ingen annan förfrågan får stå kvar (Ferroamps app, en automation). Är hubben med i en nättjänst, låt styrningen vara
av.

**Hårdvarutestet.** I SpotNav-kortet: Inställningar → Support → Hårdvarutest, tryck sedan på Start. Det är allt: du
behöver inte göra eller titta på något mer. Testet väntar själv på ett lämpligt tillfälle (huset drar mer än solen
ger, batteriet ligger mellan sina gränser, ingen bil laddar) och körs bara dagtid, 08–21. Det tar ungefär tio
minuter. Medan det pågår får dina telefoner med SpotNavs aviseringar ett meddelande med knappen "Stoppa testet".

Vad det gör, via integrationens tjänster. En förfrågan räknas som tagen först när hubben har kvitterat den:

1. Hålla: en laddförfrågan på 0 W; batteriet ska sluta täcka huset. Ferroamp dokumenterar inte det, så hubben kan
   vägra; vid ett senare test provas också en urladdningsförfrågan på 0 W.
2. En liten nätladdning: en laddförfrågan med låg effekt (i steg om 500 W, högst 2 kW, mindre om säkringen kräver
   det).
3. Täcka huset: autocharge, hubbens eget läge.
4. Lämna tillbaka: autocharge.

En förfrågan har ingen timeout och står kvar om Home Assistant stannar, så nätladdningen hålls under SpotNavs tak för
säkringen, räknad som om allt gick på en fas. Testet avbryts direkt och lämnar tillbaka om en förfrågan inte
kvitteras, om batteriet inte gör som det blir tillsagt eller om en fas närmar sig huvudsäkringen. Resultatet skickas
till mig av sig självt när testet är klart, så länge Betatestare är på.

**Avbryta.** Tryck på "Stoppa testet" i aviseringen, eller "Avbryt testet" i kortets hårdvarutest, när som helst.
Vill du sluta testa helt (då skickas inget mer): avmarkera Betatestare under Support.

**Om något blir kvar** (till exempel om Home Assistant startade om mitt i testet), återställ för hand i Home
Assistant: kör tjänsten ferroamp.autocharge för hubben, och slå sedan av SpotNavs batteriomkopplare.

Jag läser testets logg och svarar dig via e-post.

SpotNav

---8<--- code
Ämne: SpotNav: din kontrollkod för Ferroamp

Hej,

Tack för att du körde hårdvarutestet. Jag har läst loggen och det ser bra ut, så här är kontrollkoden för din
installation:

{code}

Klistra in den i SpotNav-kortet: Inställningar → Support → Hårdvarutest → Kontrollkod, tryck sedan på Skriv in.
Mellanslag och radbrytningar spelar ingen roll. Koden gäller bara din Home Assistant-installation och din Ferroamp.
Sedan kan du slå på styrningen i avsnittet Hembatteri ("Batteristyrning").

Vad styrningen gör på din EnergyHub: en hållning är en laddförfrågan på 0 W (eller urladdning på 0 W, det som
hårdvarutestet godkände), en nätladdning en laddförfrågan med den planerade effekten och en försäljning en
urladdningsförfrågan, båda för hela systemet i steg om 500 W. Att täcka huset och varje återlämning är autocharge,
som ger hubben tillbaka till sin egen strategi. En förfrågan står kvar om Home Assistant stannar, så en nätladdning
hålls alltid under taket för säkringen. Står någon annans förfrågan kvar startar SpotNav inte.

Efter en uppdatering av firmware eller integration kontrollerar SpotNav batteriet själv; ingen ny kod behövs. Går
den kontrollen inte igenom ber kortet om hårdvarutestet igen.

I 30 dagar från nu skickar betatestet en rapport per natt, så att jag kan se hur styrningen går. Avmarkera
Betatestare under Support för att stoppa dem när du vill; koden fortsätter att gälla.

**Återställ för hand** om du någon gång behöver: kör ferroamp.autocharge för hubben, och slå sedan av SpotNavs
batteriomkopplare.

SpotNav
