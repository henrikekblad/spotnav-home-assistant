Ämne: SpotNav: test av styrningen av din Tesla Powerwall

Hej,

Tack för att du vill testa SpotNavs styrning av din Tesla Powerwall. Så här går det till.

**Före testet.** Styrningen går via Teslas moln (integrationen Tesla Fleet eller Teslemetry). Powerwallen ska stå i
läget Self-Powered; Time-Based Control och Backup lämnas i fred. Slå på batteriets storlekssensor (pack energy) om
den är avslagen: utan den startar ingen laddning. Anteckna gärna Backup Reserve och omkopplaren för laddning från
nätet innan. Styr ett annat system batteriet, pausa det först: testet vägrar annars att köras.

**Hårdvarutestet.** I SpotNav-kortet: Inställningar → Support → Hårdvarutest, tryck sedan på Start. Det är allt: du
behöver inte göra eller titta på något mer. Testet väntar själv på ett lämpligt tillfälle (huset drar mer än solen
ger, batteriet ligger mellan sina gränser, ingen bil laddar) och körs bara dagtid, 08–21. Teslas moln är långsamt:
en ändring kan ta flera minuter att synas, och SpotNav väntar en kvart mellan två steg. Därför tar testet mellan en
halvtimme och en timme, aldrig mer än 75 minuter. Under tiden startar ingen billaddning på anläggningen, och dina
telefoner med SpotNavs aviseringar får ett meddelande med knappen "Stoppa testet".

Vad det gör, via Teslas moln:

1. Hålla: laddning från nätet av, och Backup Reserve två procentenheter under nivån nu (avrundat nedåt), så att
   Powerwallen inte täcker huset och inte laddar från nätet.
2. En nätladdning: laddning från nätet på, och Backup Reserve så högt som Powerwallen når på 30 minuter. Laddeffekten
   är Powerwallens egen (SpotNav räknar 5 kW per Powerwall) och kan inte sänkas. Ryms den inte under säkringen hoppar
   testet över steget och kan ändå gå igenom; styrningen laddar då inte från nätet förrän SpotNavs egen kontroll
   senare har sett en laddning som ryms.
3. Täcka huset: Backup Reserve och laddning från nätet tillbaka som de var; Powerwallen täcker huset själv.
4. Lämna tillbaka: Backup Reserve först, sedan laddningen från nätet, till de värden de hade. I 15 minuter efteråt
   ser SpotNav efter att molnet behåller dem.

Inget på en Powerwall stoppar laddningen av sig självt: stannar Home Assistant eller molnet under laddningen laddas
som mest de 30 minuterna, aldrig till 100 %. Testet avbryts direkt och lämnar tillbaka om en skrivning inte läses tillbaka, om batteriet inte gör som
det blir tillsagt eller om en fas närmar sig huvudsäkringen. Resultatet skickas till mig av sig självt när testet är
klart, så länge Betatestare är på.

**Avbryta.** Tryck på "Stoppa testet" i aviseringen, eller "Avbryt testet" i kortets hårdvarutest, när som helst.
Vill du sluta testa helt (då skickas inget mer): avmarkera Betatestare under Support.

**Om något blir kvar** (till exempel om Home Assistant startade om mitt i testet), återställ för hand i Tesla-appen
eller Home Assistant: Backup Reserve till ditt värde och laddning från nätet (omkopplaren "allow charging from grid",
på en anläggning med sol) som den var, och slå sedan av SpotNavs batteriomkopplare.

Jag läser testets logg och svarar dig via e-post.

SpotNav

---8<--- code
Ämne: SpotNav: din kontrollkod för Tesla Powerwall

Hej,

Tack för att du körde hårdvarutestet. Jag har läst loggen och det ser bra ut, så här är kontrollkoden för din
installation:

{code}

Klistra in den i SpotNav-kortet: Inställningar → Support → Hårdvarutest → Kontrollkod, tryck sedan på Skriv in.
Mellanslag och radbrytningar spelar ingen roll. Koden gäller bara din Home Assistant-installation och din Tesla.
Sedan kan du slå på styrningen i avsnittet Hembatteri ("Batteristyrning").

Vad styrningen gör på din Powerwall, bara i Self-Powered och bara planens egna steg (ingen hållning för bilen, högst
24 skrivningar per dygn, inget nytt steg inom 15 minuter): en hållning stänger av laddning från nätet och sätter
Backup Reserve två procentenheter under nivån, en nätladdning slår på laddning från nätet och sätter reserven så högt
som Powerwallen når på 30 minuter, förnyat var 30:e minut. Att täcka huset är Powerwallens eget läge. Den säljer
aldrig från batteriet. Stannar Home Assistant eller molnet laddas som mest ett sådant block från nätet, och reserven
står kvar där tills den sätts tillbaka. Varje återlämning sätter reserven och laddningen från nätet tillbaka som de
var.

Efter en uppdatering av firmware eller integration kontrollerar SpotNav batteriet själv; ingen ny kod behövs. Går
den kontrollen inte igenom ber kortet om hårdvarutestet igen.

I 30 dagar från nu skickar betatestet en rapport per natt, så att jag kan se hur styrningen går. Avmarkera
Betatestare under Support för att stoppa dem när du vill; koden fortsätter att gälla.

**Återställ för hand** om du någon gång behöver: Backup Reserve till ditt värde och laddning från nätet som den var,
och slå sedan av SpotNavs batteriomkopplare.

SpotNav
