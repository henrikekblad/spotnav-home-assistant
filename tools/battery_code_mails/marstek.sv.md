Ämne: SpotNav: test av styrningen av ditt Marstek-batteri

Hej,

Tack för att du vill testa SpotNavs styrning av ditt Marstek-batteri. Så här går det till.

**Hårdvarutestet.** I SpotNav-kortet: Inställningar → Support → Hårdvarutest, tryck sedan på Start. Det är allt: du
behöver inte göra eller titta på något mer. Testet väntar själv på ett lämpligt tillfälle (huset drar mer än solen
ger, batteriet ligger mellan sina gränser, ingen bil laddar) och körs bara dagtid, 08–21. Det tar ungefär tio
minuter. Medan det pågår får dina telefoner med SpotNavs aviseringar ett meddelande med knappen "Stoppa testet".

Vad det gör, via Marsteks Modbus-integration och bara medan arbetsläget är Self Consumption:

1. Hålla: Max Discharge Power till 0; batteriet slutar ladda ur (det lagrar fortfarande sol).
2. En liten nätladdning: RS485 Control Mode på, Set Charge Power med testets effekt (högst 2 kW, aldrig mer än
   entiteten tar, mindre om säkringen kräver det), Force Mode charge.
3. Täcka huset: Force Mode standby, RS485 Control Mode av, Max Discharge Power tillbaka.
4. Lämna tillbaka: allt som det var före testet.

Testet avbryts direkt och lämnar tillbaka om en skrivning inte läses tillbaka, om batteriet inte gör som det blir
tillsagt eller om en fas närmar sig huvudsäkringen. Resultatet skickas till mig av sig självt när testet är klart, så länge Betatestare är på.

**Avbryta.** Tryck på "Stoppa testet" i aviseringen, eller "Avbryt testet" i kortets hårdvarutest, när som helst. Vill du sluta testa (då skickas inget mer)
helt: avmarkera Betatestare under Support.

**Om något blir kvar** (till exempel om Home Assistant startade om mitt i testet), återställ för hand i Home
Assistant: Force Mode till standby, RS485 Control Mode av, Max Discharge Power till ditt värde (ofta 2500 W, eller
800 W i Tyskland), och slå sedan av SpotNavs batteriomkopplare. Visar batteriets maxeffekter 800 W efter testet, slå
av och på dem i Marstek-appen.

Jag läser testets logg och svarar dig via e-post.

SpotNav

---8<--- code
Ämne: SpotNav: din kontrollkod för Marstek

Hej,

Tack för att du körde hårdvarutestet. Jag har läst loggen och det ser bra ut, så här är kontrollkoden för din
installation:

{code}

Klistra in den i SpotNav-kortet: Inställningar → Support → Hårdvarutest → Kontrollkod, tryck sedan på Skriv in.
Mellanslag och radbrytningar spelar ingen roll. Koden gäller bara din Home Assistant-installation och ditt Marstek.
Sedan kan du slå på styrningen i avsnittet Hembatteri ("Batteristyrning").

Vad styrningen gör på ditt Marstek: en hållning sätter Max Discharge Power till 0; en nätladdning använder RS485
Control Mode, Set Charge Power och Force Mode; varje återlämning sätter Force Mode till standby, RS485 Control Mode av
och Max Discharge Power tillbaka. Batteriet har ingen egen dödmansgrepp-funktion: en tvångsladdning fortsätter tills
den blir tillsagd något annat, så SpotNav begränsar varje nätladdning för fallet att Home Assistant stannar, och
räknar hela laddningen på batteriets enda fas.

Efter en uppdatering av firmware eller integration kontrollerar SpotNav batteriet själv; ingen ny kod behövs. Går
den kontrollen inte igenom ber kortet om hårdvarutestet igen.

I 30 dagar från nu skickar betatestet en rapport per natt, så att jag kan se hur styrningen går. Avmarkera
Betatestare under Support för att stoppa dem när du vill; koden fortsätter att gälla.

**Återställ för hand** om du någon gång behöver: Force Mode till standby, RS485 Control Mode av, Max Discharge Power
till ditt värde, och slå sedan av SpotNavs batteriomkopplare.

SpotNav
