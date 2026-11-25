Ämne: SpotNav: test av styrningen av ditt Solis-batteri

Hej,

Tack för att du vill testa SpotNavs styrning av ditt Solis-batteri. Så här går det till.

**Före testet.** Styr ett annat system batteriet (en energistyrning som Enequi, eller SolisClouds egen strategi),
pausa det först: SpotNav skriver aldrig medan något annat styr växelriktarens fjärrstyrda tvångsläge, och testet
vägrar då att köras. När SpotNav har sett ett annat systems tvångsläge väntar det 48 timmar innan det tar batteriet
igen, om du inte trycker på "Det var jag" i kortet. Tidsstyrda perioder måste också vara avslagna.

**Hårdvarutestet.** I SpotNav-kortet: Inställningar → Support → Hårdvarutest, tryck sedan på Start. Det är allt: du
behöver inte göra eller titta på något mer. Testet väntar själv på ett lämpligt tillfälle (huset drar mer än solen
ger, batteriet ligger mellan sina gränser, ingen bil laddar) och körs bara dagtid, 08–21. Det tar ungefär tjugo
minuter. Medan det pågår får dina telefoner med SpotNavs aviseringar ett meddelande med knappen "Stoppa testet".

Vad det gör, via integrationen Solis Modbus och bara i lagringsläget Self-Use utan något tvångsläge igång:

1. Hålla: RC-tvångsladdning med 0 W och RC Timeout på 5 minuter; batteriet slutar täcka huset (det lagrar
   fortfarande sol).
2. En liten nätladdning: RC-tvångsladdning med låg effekt (högst 2 kW, inom BMS:ens laddgräns, mindre om säkringen
   kräver det).
3. Täcka huset: tvångsläget stoppat, växelriktarens eget Self-Use.
4. Slutar av sig själv: en hållning igen, förnyad en gång och sedan lämnad i fred, och tiden mäts tills växelriktaren
   själv börjar täcka huset (ungefär fem minuter väntas).
5. Lämna tillbaka: tvångsläget stoppat, båda tvångseffekterna på 0 och RC Timeout som den var.

Testet avbryts direkt och lämnar tillbaka om en skrivning inte läses tillbaka, om batteriet inte gör som det blir
tillsagt eller om en fas närmar sig huvudsäkringen. Resultatet skickas till mig av sig självt när testet är klart,
så länge Betatestare är på.

**Avbryta.** Tryck på "Stoppa testet" i aviseringen, eller "Avbryt testet" i kortets hårdvarutest, när som helst.
Vill du sluta testa helt (då skickas inget mer): avmarkera Betatestare under Support.

**Om något blir kvar** (till exempel om Home Assistant startade om mitt i testet), återställ för hand i Home
Assistant: RC Force Charge/Discharge till None, RC Force Battery Charge Power och RC Force Battery Discharge Power
till 0 och RC Timeout till ditt värde (5 minuter som standard), och slå sedan av SpotNavs batteriomkopplare. Ett
tvångsläge som blir kvar slutar också av sig självt när RC Timeout har gått ut.

Jag läser testets logg och svarar dig via e-post.

SpotNav

---8<--- code
Ämne: SpotNav: din kontrollkod för Solis

Hej,

Tack för att du körde hårdvarutestet. Jag har läst loggen och det ser bra ut, så här är kontrollkoden för din
installation:

{code}

Klistra in den i SpotNav-kortet: Inställningar → Support → Hårdvarutest → Kontrollkod, tryck sedan på Skriv in.
Mellanslag och radbrytningar spelar ingen roll. Koden gäller bara din Home Assistant-installation och din Solis.
Sedan kan du slå på styrningen i avsnittet Hembatteri ("Batteristyrning").

Vad styrningen gör på din Solis: en hållning är en RC-tvångsladdning med 0 W, en nätladdning en RC-tvångsladdning med
den planerade effekten (inom BMS:ens laddgräns), en försäljning en RC-tvångsurladdning, var och en skickad med sin
effekt och en RC Timeout på 5 minuter i ett och samma anrop; SpotNav förnyar tvångsläget varannan minut, så det slutar
av sig självt inom fem minuter om Home Assistant stannar. Varje återlämning stoppar tvångsläget och sätter båda
tvångseffekterna till 0. Medan ett annat system styr tvångsläget bara tittar SpotNav.

Efter en uppdatering av firmware eller integration kontrollerar SpotNav batteriet själv; ingen ny kod behövs. Går
den kontrollen inte igenom ber kortet om hårdvarutestet igen.

I 30 dagar från nu skickar betatestet en rapport per natt, så att jag kan se hur styrningen går. Avmarkera
Betatestare under Support för att stoppa dem när du vill; koden fortsätter att gälla.

**Återställ för hand** om du någon gång behöver: RC Force Charge/Discharge till None, båda tvångseffekterna till 0,
RC Timeout till ditt värde, och slå sedan av SpotNavs batteriomkopplare.

SpotNav
