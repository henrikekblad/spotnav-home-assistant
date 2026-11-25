Ämne: SpotNav: test av styrningen av ditt Victron-batteri

Hej,

Tack för att du vill testa SpotNavs styrning av ditt Victron ESS-system. Så här går det till.

**Före testet.** ESS-läget ska vara Optimized (med eller utan BatteryLife) och Dynamic ESS avslaget. Inget annat på
GX:en får skriva börvärdena (ett Node-RED-flöde, ett skript). Styr ett annat system batteriet, pausa det först:
testet vägrar annars att köras. Anteckna gärna ESS max charge power, ESS max inverter power och AC power setpoint
innan.

**Hårdvarutestet.** I SpotNav-kortet: Inställningar → Support → Hårdvarutest, tryck sedan på Start. Det är allt: du
behöver inte göra eller titta på något mer. Testet väntar själv på ett lämpligt tillfälle (huset drar mer än solen
ger, batteriet ligger mellan sina gränser, ingen bil laddar) och körs bara dagtid, 08–21. Det tar ungefär tio
minuter. Medan det pågår får dina telefoner med SpotNavs aviseringar ett meddelande med knappen "Stoppa testet".

Vad det gör, via integrationen Victron GX (eller Victron MQTT), i ESS mode 2 där GX:en kör sin egen reglering:

1. Hålla: ESS max inverter power limit till 0; batteriet laddar inte ur (solen lagras fortfarande).
2. En liten nätladdning (högst 2 kW, mindre om säkringen kräver det): ESS max charge power till den effekten och AC
   power setpoint (nätets effekt, inte batteriets) till husets last nu plus effekten. På ett trefassystem där
   säkringen inte ger plats för det provas i stället en laddning på högst 1 kW på den fas som har mest plats, i
   external control (ESS mode Hub4) med ett börvärde per fas, förnyat var 20:e sekund.
3. Täcka huset: ESS max charge power till 0; GX:en täcker huset som vanligt och batteriet lagrar inget från växlaren.
4. Lämna tillbaka: AC power setpoint, ESS max charge power och ESS max inverter power till de värden de hade (och ESS
   mode tillbaka som det var, om laddningen per fas kördes).

Testet avbryts direkt och lämnar tillbaka om en skrivning inte läses tillbaka, om batteriet inte gör som det blir
tillsagt eller om en fas närmar sig huvudsäkringen. Resultatet skickas till mig av sig självt när testet är klart,
så länge Betatestare är på.

**Avbryta.** Tryck på "Stoppa testet" i aviseringen, eller "Avbryt testet" i kortets hårdvarutest, när som helst.
Vill du sluta testa helt (då skickas inget mer): avmarkera Betatestare under Support.

**Om något blir kvar** (till exempel om Home Assistant startade om mitt i testet), återställ för hand i Home
Assistant eller på GX:en under Settings → ESS: ESS mode tillbaka till ditt Optimized-läge, AC power setpoint till
ditt vanliga värde, ESS max charge power och ESS max inverter power till dina värden (-1 är ingen gräns). Kontrollera
också Hub4 disable charge och Hub4 disable feed-in. Slå sedan av SpotNavs batteriomkopplare.

Jag läser testets logg och svarar dig via e-post.

SpotNav

---8<--- code
Ämne: SpotNav: din kontrollkod för Victron

Hej,

Tack för att du körde hårdvarutestet. Jag har läst loggen och det ser bra ut, så här är kontrollkoden för din
installation:

{code}

Klistra in den i SpotNav-kortet: Inställningar → Support → Hårdvarutest → Kontrollkod, tryck sedan på Skriv in.
Mellanslag och radbrytningar spelar ingen roll. Koden gäller bara din Home Assistant-installation och din Victron.
Sedan kan du slå på styrningen i avsnittet Hembatteri ("Batteristyrning").

Vad styrningen gör på ditt Victron ESS, bara i ett Optimized-läge med Dynamic ESS av: en hållning sätter ESS max
inverter power limit till 0, att täcka huset ESS max charge power till 0, en nätladdning ESS max charge power till
den planerade effekten och AC power setpoint till husets last plus effekten, och en försäljning AC power setpoint
under husets last. Det är inställningar på GX:en som står kvar om Home Assistant stannar, så en nätladdning hålls
alltid under taket för säkringen. En laddning per fas går i external control med ett börvärde per fas, förnyat var
20:e sekund; att Multin går till passthru av sig själv när skrivningarna slutar räknar SpotNav inte med förrän en
anläggning har visat det. Varje återlämning sätter värdena (och ESS-läget) tillbaka som de var.

Efter en uppdatering av firmware eller integration kontrollerar SpotNav batteriet själv; ingen ny kod behövs. Går
den kontrollen inte igenom ber kortet om hårdvarutestet igen.

I 30 dagar från nu skickar betatestet en rapport per natt, så att jag kan se hur styrningen går. Avmarkera
Betatestare under Support för att stoppa dem när du vill; koden fortsätter att gälla.

**Återställ för hand** om du någon gång behöver: ESS mode till ditt Optimized-läge, AC power setpoint, ESS max charge
power och ESS max inverter power till dina värden, och slå sedan av SpotNavs batteriomkopplare.

SpotNav
