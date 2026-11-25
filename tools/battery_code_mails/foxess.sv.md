Ämne: SpotNav: test av styrningen av ditt FoxESS-batteri

Hej,

Tack för att du vill testa SpotNavs styrning av ditt FoxESS-batteri. Så här går det till.

**Hårdvarutestet.** I SpotNav-kortet: Inställningar → Support → Hårdvarutest, tryck sedan på Start. Det är allt: du
behöver inte göra eller titta på något mer. Testet väntar själv på ett lämpligt tillfälle (huset drar mer än solen
ger, batteriet ligger mellan sina gränser, ingen bil laddar) och körs bara dagtid, 08–21. Det tar ungefär tio
minuter. Medan det pågår får dina telefoner med SpotNavs aviseringar ett meddelande med knappen "Stoppa testet".

Vad det gör på en FoxESS, via integrationen foxess_modbus och bara medan arbetsläget är Self Use:

1. Hålla: Max Discharge Current till 0, så att batteriet slutar täcka huset.
2. Nätladdningen hoppas över: SpotNav laddar aldrig en FoxESS från nätet.
3. Täcka huset: gränserna tillbaka, batteriet täcker huset igen.
4. Lämna tillbaka: Max Charge Current och Max Discharge Current till de värden de hade.

Testet avbryts direkt och lämnar tillbaka om en skrivning inte läses tillbaka, om batteriet inte gör som det blir
tillsagt eller om en fas närmar sig huvudsäkringen. Resultatet skickas till mig av sig självt när testet är klart, så länge Betatestare är på.

**Avbryta.** Tryck på "Stoppa testet" i aviseringen, eller "Avbryt testet" i kortets hårdvarutest, när som helst. Vill du sluta testa (då skickas inget mer)
helt: avmarkera Betatestare under Support.

**Om något blir kvar** (till exempel om Home Assistant startade om mitt i testet), återställ för hand i Home
Assistant: sätt Max Charge Current och Max Discharge Current till dina värden (ofta batteriets max), och slå sedan av
SpotNavs batteriomkopplare.

Jag läser testets logg och svarar dig via e-post.

SpotNav

---8<--- code
Ämne: SpotNav: din kontrollkod för FoxESS

Hej,

Tack för att du körde hårdvarutestet. Jag har läst loggen och det ser bra ut, så här är kontrollkoden för din
installation:

{code}

Klistra in den i SpotNav-kortet: Inställningar → Support → Hårdvarutest → Kontrollkod, tryck sedan på Skriv in.
Mellanslag och radbrytningar spelar ingen roll. Koden gäller bara din Home Assistant-installation och ditt FoxESS.
Sedan kan du slå på styrningen i avsnittet Hembatteri ("Batteristyrning").

Vad styrningen gör på ditt FoxESS: den sätter bara de två strömgränserna (Max Charge Current och Max Discharge
Current), och bara medan arbetsläget är Self Use. Den laddar aldrig från nätet och säljer aldrig. Om Home Assistant
stannar ligger en gräns som SpotNav satt kvar tills Home Assistant är tillbaka; batteriet täcker då mindre av huset,
men tar aldrig mer från nätet på grund av det.

Efter en uppdatering av firmware eller integration kontrollerar SpotNav batteriet själv; ingen ny kod behövs. Går
den kontrollen inte igenom ber kortet om hårdvarutestet igen.

I 30 dagar från nu skickar betatestet en rapport per natt, så att jag kan se hur styrningen går. Avmarkera
Betatestare under Support för att stoppa dem när du vill; koden fortsätter att gälla.

**Återställ för hand** om du någon gång behöver: Max Charge Current och Max Discharge Current till dina värden, och
slå sedan av SpotNavs batteriomkopplare.

SpotNav
