Ämne: SpotNav: test av styrningen av ditt Fronius-batteri

Hej,

Tack för att du vill testa SpotNavs styrning av ditt Fronius-batteri. Så här går det till.

**Före testet.** SpotNav använder batterigränserna i Fronius-integrationen i Home Assistant core (Home Assistant
2026.11 eller senare), som pratar Modbus TCP med växelriktaren. Kontrollera en gång:

- I växelriktarens webbgränssnitt, Communication → Modbus: Modbus TCP på och "Inverter control via Modbus" på. Utan
  det visar integrationen inga batterigränser alls.
- I webbgränssnittet batteriets "Battery charging from other sources (including public grid)" på, och i Home
  Assistant omkopplaren **Battery grid charging** på, om steget med nätladdning ska köras. SpotNav läser båda och
  ändrar dem aldrig.
- Tidsstyrning (time-of-use) i webbgränssnittet och Energy Cost Assistant i Solar.web av. Home Assistant kan inte se
  dem, så kortet ber dig bekräfta att SpotNavs gränser tar över från dem.
- Rekommenderas: i Home Assistant, Inställningar → Enheter och tjänster → Fronius → Konfigurera om, slå på "Revert the
  power and battery limits if Home Assistant stops". Då släpper växelriktaren en gräns själv ungefär en timme efter att
  Home Assistant har stannat.
- Inget annat (evcc, ett skript, en automation) sätter batterigränserna: SpotNav startar inte medan någon av de två
  gränsomkopplarna är på.

**Hårdvarutestet.** I SpotNav-kortet: Inställningar → Support → Hårdvarutest, tryck sedan på Start. Det är allt: du
behöver inte göra eller titta på något mer. Testet väntar själv på ett lämpligt tillfälle (huset drar mer än solen
ger, batteriet ligger mellan sina gränser, ingen bil laddar) och körs bara dagtid, 08–21. Det tar ungefär tio
minuter. Medan det pågår får dina telefoner med SpotNavs aviseringar ett meddelande med knappen "Stoppa testet".

Vad det gör, och bara om båda gränsomkopplarna är av när det börjar:

1. Hålla: Battery discharge power limit till 0 % och Battery discharge power limiting på; batteriet slutar täcka
   huset (det lagrar fortfarande sol).
2. En liten nätladdning: Battery discharge power limit under 0 (en tvångsladdning med en andel av batteriets
   märkeffekt, högst 2 kW, mindre om säkringen kräver det).
3. Täcka huset: Battery charge power limit till 0 % och Battery charge power limiting på, urladdningsgränsen av;
   batteriet täcker huset och lagrar inget.
4. Lämna tillbaka: båda gränsomkopplarna av, båda gränserna tillbaka till sina värden.

Testet avbryts direkt och lämnar tillbaka om en skrivning inte läses tillbaka, om batteriet inte gör som det blir
tillsagt eller om en fas närmar sig huvudsäkringen. Resultatet skickas till mig av sig självt när testet är klart, så
länge Betatestare är på.

**Avbryta.** Tryck på "Stoppa testet" i aviseringen, eller "Avbryt testet" i kortets hårdvarutest, när som helst.
Vill du sluta testa helt (då skickas inget mer): avmarkera Betatestare under Support.

**Om något blir kvar** (till exempel om Home Assistant startade om mitt i testet), återställ för hand i Home
Assistant: slå av Battery charge power limiting och Battery discharge power limiting, sätt båda gränserna till dina
värden (100 % om du aldrig ändrat dem), och slå sedan av SpotNavs batteriomkopplare. Med återgångsinställningen på
upphör en kvarlämnad gräns också av sig själv ungefär en timme efter att Home Assistant har stannat.

Jag läser testets logg och svarar dig via e-post.

SpotNav

---8<--- code
Ämne: SpotNav: din kontrollkod för Fronius

Hej,

Tack för att du körde hårdvarutestet. Jag har läst loggen och det ser bra ut, så här är kontrollkoden för din
installation:

{code}

Klistra in den i SpotNav-kortet: Inställningar → Support → Hårdvarutest → Kontrollkod, tryck sedan på Skriv in.
Mellanslag och radbrytningar spelar ingen roll. Koden gäller bara din Home Assistant-installation och din Fronius.
Sedan kan du slå på styrningen i avsnittet Hembatteri ("Batteristyrning").

Vad styrningen gör på din Fronius: en hållning sätter batteriets urladdningsgräns till 0 %, en nätladdning sätter den
under 0 (en tvångsladdning med laddeffekten), att täcka huset utan att lagra sätter laddgränsen till 0 %; bara en av
de två gränsomkopplarna är på åt gången. Varje återlämning slår av båda omkopplarna och sätter tillbaka gränserna.
SpotNav ändrar aldrig nätladdningen eller den lägsta reserven, och säljer ännu inte från batteriet. Växelriktaren
behåller en gräns tills den ändras, eller, med integrationens återgångsinställning på, tills ungefär en timme efter
att Home Assistant har stannat, så SpotNav begränsar varje nätladdning för fallet att Home Assistant stannar.

Efter en uppdatering av firmware eller integration kontrollerar SpotNav batteriet själv; ingen ny kod behövs. Går
den kontrollen inte igenom ber kortet om hårdvarutestet igen.

I 30 dagar från nu skickar betatestet en rapport per natt, så att jag kan se hur styrningen går. Avmarkera
Betatestare under Support för att stoppa dem när du vill; koden fortsätter att gälla.

**Återställ för hand** om du någon gång behöver: båda gränsomkopplarna av, båda gränserna till dina värden, och slå
sedan av SpotNavs batteriomkopplare.

SpotNav
