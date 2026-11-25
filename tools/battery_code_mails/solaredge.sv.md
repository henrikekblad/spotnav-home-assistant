Ämne: SpotNav: test av styrningen av ditt SolarEdge-batteri

Hej,

Tack för att du vill testa SpotNavs styrning av ditt SolarEdge-batteri. Så här går det till.

**Före testet.** Storage Control Mode ska redan stå på Remote Control (SpotNav ändrar den aldrig), och för
nätladdningen får AC Charge Policy inte vara Disabled. Styr ett annat system batteriet (Predbat, en energistyrning),
pausa det först: testet vägrar annars att köras.

**Hårdvarutestet.** I SpotNav-kortet: Inställningar → Support → Hårdvarutest, tryck sedan på Start. Det är allt: du
behöver inte göra eller titta på något mer. Testet väntar själv på ett lämpligt tillfälle (huset drar mer än solen
ger, batteriet ligger mellan sina gränser, ingen bil laddar) och körs bara dagtid, 08–21. Det tar ungefär tio
minuter. Medan det pågår får dina telefoner med SpotNavs aviseringar ett meddelande med knappen "Stoppa testet".

Vad det gör, via integrationen SolarEdge Modbus Multi och bara i Remote Control. Varje kommando skrivs med Storage
Command Timeout på 30 minuter, så att växelriktaren går tillbaka till sitt eget läge om ingen förnyar det:

1. Hålla: Storage Command Mode till Charge from Solar Power; batteriet slutar täcka huset (det lagrar fortfarande
   sol).
2. En liten nätladdning: Storage Charge Limit till en låg effekt (högst 2 kW, mindre om säkringen kräver det) och
   Storage Command Mode till Charge from Solar Power and Grid.
3. Täcka huset: Storage Command Mode till Discharge to Minimize Import.
4. Lämna tillbaka: Storage Command Mode, Storage Charge Limit, Storage Discharge Limit och Storage Command Timeout
   till de värden de hade.

Testet avbryts direkt och lämnar tillbaka om en skrivning inte läses tillbaka, om batteriet inte gör som det blir
tillsagt eller om en fas närmar sig huvudsäkringen. Resultatet skickas till mig av sig självt när testet är klart,
så länge Betatestare är på.

**Avbryta.** Tryck på "Stoppa testet" i aviseringen, eller "Avbryt testet" i kortets hårdvarutest, när som helst.
Vill du sluta testa helt (då skickas inget mer): avmarkera Betatestare under Support.

**Om något blir kvar** (till exempel om Home Assistant startade om mitt i testet), återställ för hand i Home
Assistant: Storage Command Mode, Storage Charge Limit, Storage Discharge Limit och Storage Command Timeout till dina
värden, och slå sedan av SpotNavs batteriomkopplare. Ett kommando som blir kvar slutar också av sig självt när Storage
Command Timeout har gått ut (högst 30 minuter).

Jag läser testets logg och svarar dig via e-post.

SpotNav

---8<--- code
Ämne: SpotNav: din kontrollkod för SolarEdge

Hej,

Tack för att du körde hårdvarutestet. Jag har läst loggen och det ser bra ut, så här är kontrollkoden för din
installation:

{code}

Klistra in den i SpotNav-kortet: Inställningar → Support → Hårdvarutest → Kontrollkod, tryck sedan på Skriv in.
Mellanslag och radbrytningar spelar ingen roll. Koden gäller bara din Home Assistant-installation och din SolarEdge.
Sedan kan du slå på styrningen i avsnittet Hembatteri ("Batteristyrning").

Vad styrningen gör på din SolarEdge, bara i Remote Control: en hållning är Charge from Solar Power, en nätladdning
Charge from Solar Power and Grid med laddgränsen på den planerade effekten, att täcka huset Discharge to Minimize
Import och en försäljning Discharge to Maximize Export med urladdningsgränsen på effekten. Varje kommando skrivs med en
Storage Command Timeout på 30 minuter och förnyas var femtonde minut, så växelriktaren går tillbaka till sitt eget
läge inom 30 minuter om Home Assistant stannar. Varje återlämning sätter läget, gränserna och timeouten tillbaka som
de var.

Efter en uppdatering av firmware eller integration kontrollerar SpotNav batteriet själv; ingen ny kod behövs. Går
den kontrollen inte igenom ber kortet om hårdvarutestet igen.

I 30 dagar från nu skickar betatestet en rapport per natt, så att jag kan se hur styrningen går. Avmarkera
Betatestare under Support för att stoppa dem när du vill; koden fortsätter att gälla.

**Återställ för hand** om du någon gång behöver: Storage Command Mode, laddgränsen, urladdningsgränsen och Storage
Command Timeout till dina värden, och slå sedan av SpotNavs batteriomkopplare.

SpotNav
