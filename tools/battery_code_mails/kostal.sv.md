Ämne: SpotNav: test av styrningen av ditt Kostal Plenticore-batteri

Hej,

Tack för att du vill testa SpotNavs styrning av ditt Kostal Plenticore-batteri. Så här går det till.

**Före testet.** SpotNav styr en Plenticore bara via integrationen Kostal Plenticore Battery Control
(kostal_modbus_control, från HACS), och bara när:

- växelriktarens Battery Management står på "External via protocol (Modbus TCP)" (en installatörsinställning i
  växelriktarens webbgränssnitt). Integrationens sensor "Battery Management Mode" ska visa "External battery
  management via Modbus protocol"; den är avstängd från början, och kortet erbjuder att slå på den;
- integrationens Modbus Timeout är samma som tidsgränsen bredvid den inställningen i webbgränssnittet;
- ingen av integrationens omkopplare (Charge Start, Discharge Start, Block Charge, Block Discharge, Inverter Control)
  är på, och Predbat, om det är installerat, inte står i ett styrläge;
- inget annat skriver till växelriktaren (evcc, Tibbers brygga, en egen Modbus-automation): växelriktaren tar en
  styrare i taget.

Kortet ber dig också bekräfta att SpotNavs kommandon går före växelriktarens egen batteristyrning och tidsstyrning,
som integrationen inte visar.

**Hårdvarutestet.** I SpotNav-kortet: Inställningar → Support → Hårdvarutest, tryck sedan på Start. Det är allt: du
behöver inte göra eller titta på något mer. Testet väntar själv på ett lämpligt tillfälle (huset drar mer än solen
ger, batteriet ligger mellan sina gränser, ingen bil laddar) och körs bara dagtid, 08–21. Det tar ungefär tio
minuter. Medan det pågår får dina telefoner med SpotNavs aviseringar ett meddelande med knappen "Stoppa testet".

Vad det gör, med integrationens egna omkopplare:

1. Hålla: Block Discharge på; batteriet slutar täcka huset (det lagrar fortfarande sol).
2. En liten nätladdning: Block Discharge av, Set Charge Rate till testets effekt (högst 2 kW, minst 5 % under
   laddeffektens maxvärde, mindre om säkringen kräver det), sedan Charge Start på.
3. Täcka huset: Charge Start av, växelriktarens egen styrning.
4. Lämna tillbaka: båda omkopplarna av som de var, sedan laddeffekten som den var.

Testet avbryts direkt och lämnar tillbaka om en skrivning inte läses tillbaka, om batteriet inte gör som det blir
tillsagt eller om en fas närmar sig huvudsäkringen. Resultatet skickas till mig av sig självt när testet är klart, så
länge Betatestare är på.

**Tre saker jag gärna vill veta** (ett kort svar räcker): tidsgränsen i webbgränssnittet bredvid "External via
protocol (Modbus TCP)", din växelriktare (Plenticore plus G1, G2, G3, BI eller MP G3) med batteri, och om något annat
någon gång har skrivit till den.

**Avbryta.** Tryck på "Stoppa testet" i aviseringen, eller "Avbryt testet" i kortets hårdvarutest, när som helst.
Vill du sluta testa helt (då skickas inget mer): avmarkera Betatestare under Support.

**Om något blir kvar** (till exempel om Home Assistant startade om mitt i testet): integrationens omkopplare är av
efter en omstart, och växelriktaren går tillbaka till sin egen styrning av sig själv när tidsgränsen har gått ut.
Annars slår du av Charge Start och Block Discharge, och sedan SpotNavs batteriomkopplare.

Jag läser testets logg och svarar dig via e-post.

SpotNav

---8<--- code
Ämne: SpotNav: din kontrollkod för Kostal Plenticore

Hej,

Tack för att du körde hårdvarutestet. Jag har läst loggen och det ser bra ut, så här är kontrollkoden för din
installation:

{code}

Klistra in den i SpotNav-kortet: Inställningar → Support → Hårdvarutest → Kontrollkod, tryck sedan på Skriv in.
Mellanslag och radbrytningar spelar ingen roll. Koden gäller bara din Home Assistant-installation och din Plenticore.
Sedan kan du slå på styrningen i avsnittet Hembatteri ("Batteristyrning").

Vad styrningen gör på din Plenticore: en hållning slår på Block Discharge; en nätladdning sätter Set Charge Rate och
slår på Charge Start; varje återlämning slår av dem. Integrationen upprepar varje kommando till växelriktaren inom dess
tidsgräns, och växelriktaren går tillbaka till sin egen styrning av sig själv när Home Assistant stannar. Det räknas
inte med: SpotNav begränsar varje nätladdning för fallet att Home Assistant stannar, och räknar hela laddningen på en
fas.

Efter en uppdatering av firmware eller integration kontrollerar SpotNav batteriet själv; ingen ny kod behövs. Går
den kontrollen inte igenom ber kortet om hårdvarutestet igen.

I 30 dagar från nu skickar betatestet en rapport per natt, så att jag kan se hur styrningen går. Avmarkera
Betatestare under Support för att stoppa dem när du vill; koden fortsätter att gälla.

**Återställ för hand** om du någon gång behöver: slå av Charge Start och Block Discharge, och sedan SpotNavs
batteriomkopplare.

SpotNav
