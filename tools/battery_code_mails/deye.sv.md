Ämne: SpotNav: test av styrningen av ditt Deye/Sunsynk-batteri

Hej,

Tack för att du vill testa SpotNavs styrning av ditt Deye/Sunsynk-batteri. Så här går det till.

**Före testet.** SpotNav tar bara över batteriet medan växelriktarens *Time of Use* står på Disabled (av): med Time
of Use på kör växelriktaren sina egna program och SpotNav bara tittar på. Använder du Sunsynk-tillägget (kellerza),
lägg till sensorgruppen "battery" (eller "advanced") i dess SENSORS-inställning, så att *Battery Max Charge current*
och *Battery Max Discharge current* når Home Assistant. Med Solarman-integrationen (davidrapan) finns de redan.

**Hårdvarutestet.** I SpotNav-kortet: Inställningar → Support → Hårdvarutest, tryck sedan på Start. Det är allt: du
behöver inte göra eller titta på något mer. Testet väntar själv på ett lämpligt tillfälle (huset drar mer än solen
ger, batteriet ligger mellan sina gränser, ingen bil laddar) och körs bara dagtid, 08–21. Det tar ungefär tio
minuter. Medan det pågår får dina telefoner med SpotNavs aviseringar ett meddelande med knappen "Stoppa testet".

Vad det gör på en Deye/Sunsynk, och bara medan Time of Use är av:

1. Hålla: Battery Max Discharging Current till 0 A, så att batteriet slutar täcka huset.
2. Nätladdningen hoppas över: SpotNav laddar aldrig en Deye från nätet och ändrar aldrig dina tidsprogram.
3. Täcka huset: urladdningsgränsen tillbaka och Battery Max Charging Current till 0 A, så att batteriet täcker
   huset och inte lagrar något.
4. Lämna tillbaka: Battery Max Charging Current och Battery Max Discharging Current till de värden de hade.

Testet avbryts direkt och lämnar tillbaka om en skrivning inte läses tillbaka, om batteriet inte gör som det blir
tillsagt eller om en fas närmar sig huvudsäkringen. Resultatet skickas till mig av sig självt när testet är klart,
så länge Betatestare är på.

**Avbryta.** Tryck på "Stoppa testet" i aviseringen, eller "Avbryt testet" i kortets hårdvarutest, när som helst.
Vill du sluta testa helt (då skickas inget mer): avmarkera Betatestare under Support.

**Om något blir kvar** (till exempel om Home Assistant startade om mitt i testet), återställ för hand i Home
Assistant: sätt Battery Max Charging Current och Battery Max Discharging Current till dina värden (ofta batteriets
max), och slå sedan av SpotNavs batteriomkopplare.

Tre saker som hjälper mig att läsa loggen, om du vet dem: växelriktarens modell (till exempel SUN-12K-SG04LP3-EU)
och firmware, vilken integration och version du använder, och om något annat skriver till växelriktaren (Deye- eller
Sunsynk-appens schema, Predbat, evcc, Solar Assistant).

Jag läser testets logg och svarar dig via e-post.

SpotNav

---8<--- code
Ämne: SpotNav: din kontrollkod för Deye/Sunsynk

Hej,

Tack för att du körde hårdvarutestet. Jag har läst loggen och det ser bra ut, så här är kontrollkoden för din
installation:

{code}

Klistra in den i SpotNav-kortet: Inställningar → Support → Hårdvarutest → Kontrollkod, tryck sedan på Skriv in.
Mellanslag och radbrytningar spelar ingen roll. Koden gäller bara din Home Assistant-installation och din
Deye/Sunsynk. Sedan kan du slå på styrningen i avsnittet Hembatteri ("Batteristyrning").

Vad styrningen gör på din Deye/Sunsynk: den sätter bara de två strömgränserna (Battery Max Charging Current och
Battery Max Discharging Current), och bara medan Time of Use är av. Den laddar aldrig från nätet, säljer aldrig och
ändrar aldrig dina tidsprogram. Den skriver bara när planen ändrar vad batteriet ska göra, högst 48 gånger per dygn,
eftersom ingen anger hur ofta växelriktaren tål att skrivas till. Om Home Assistant stannar ligger en gräns som
SpotNav satt kvar tills Home Assistant är tillbaka; batteriet täcker då mindre av huset, men tar aldrig mer från
nätet på grund av det.

Efter en uppdatering av firmware eller integration kontrollerar SpotNav batteriet själv; ingen ny kod behövs. Går
den kontrollen inte igenom ber kortet om hårdvarutestet igen.

I 30 dagar från nu skickar betatestet en rapport per natt, så att jag kan se hur styrningen går. Avmarkera
Betatestare under Support för att stoppa dem när du vill; koden fortsätter att gälla.

**Återställ för hand** om du någon gång behöver: Battery Max Charging Current och Battery Max Discharging Current
till dina värden, och slå sedan av SpotNavs batteriomkopplare.

SpotNav
