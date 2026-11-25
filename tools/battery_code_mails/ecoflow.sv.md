Ämne: SpotNav: test av styrningen av din EcoFlow PowerOcean

Hej,

Tack för att du vill testa SpotNavs styrning av din EcoFlow PowerOcean. Så här går det till.

**Hårdvarutestet.** I SpotNav-kortet: Inställningar → Support → Hårdvarutest, tryck sedan på Start. Det är allt: du
behöver inte göra eller titta på något mer. Testet väntar själv på ett lämpligt tillfälle (huset drar mer än solen
ger, batteriet ligger mellan sina gränser, ingen bil laddar) och körs bara dagtid, 08–21. Det tar ungefär en kvart.
Medan det pågår får dina telefoner med SpotNavs aviseringar ett meddelande med knappen "Stoppa testet". EcoFlow-appen är låst
medan SpotNav håller Modbus Control, och ungefär en minut efter.

Vad det gör, via integrationen EF-PowerOcean-TcpModbus och bara medan appen kör egenförbrukning:

1. Hålla: Modbus Control på, Battery Mode Hold Battery; batteriet står stilla.
2. En liten nätladdning: Charge Battery med låg effekt (högst 2 kW, mindre om säkringen kräver det).
3. Täcka huset: Battery Mode Automatic.
4. Hjärtslaget: hålla igen, sedan Modbus Control av och inget annat, och mäta hur länge växelriktaren behåller
   hållningen innan den täcker huset själv (ungefär en minut väntas).
5. Lämna tillbaka: Battery Mode Automatic, sedan Modbus Control av.

Testet avbryts direkt och lämnar tillbaka om en skrivning inte läses tillbaka, om batteriet inte gör som det blir
tillsagt eller om en fas närmar sig huvudsäkringen. Resultatet skickas till mig av sig självt när testet är klart.

**Avbryta.** Tryck på "Stoppa testet" i aviseringen, eller "Avbryt testet" i kortets hårdvarutest, när som helst. Vill du sluta testa
helt: avmarkera Betatestare under Support.

**Om något blir kvar** (till exempel om Home Assistant startade om mitt i testet), återställ för hand i Home
Assistant: sätt Battery Mode till Automatic och slå sedan av Modbus Control; efter en minut fungerar EcoFlow-appen
igen. Slå sedan av SpotNavs batteriomkopplare.

Jag läser testets logg och svarar dig via e-post.

SpotNav

---8<--- code
Ämne: SpotNav: din kontrollkod för EcoFlow

Hej,

Tack för att du körde hårdvarutestet. Jag har läst loggen och det ser bra ut, så här är kontrollkoden för din
installation:

{code}

Klistra in den i SpotNav-kortet: Inställningar → Support → Hårdvarutest → Kontrollkod, tryck sedan på Skriv in.
Mellanslag och radbrytningar spelar ingen roll. Koden gäller bara din Home Assistant-installation och din EcoFlow.
Sedan kan du slå på styrningen i avsnittet Hembatteri ("Batteristyrning").

Vad styrningen gör på din PowerOcean: den slår på Modbus Control och sätter Battery Mode (Hold Battery, Charge
Battery, Automatic) via integrationen, och lämnar tillbaka med Automatic och Modbus Control av. Medan SpotNav håller
den är EcoFlow-appen låst. Om Home Assistant stannar tar växelriktarens hjärtslag slut efter ungefär en minut, och den
går då själv tillbaka till appens egna inställningar.

Efter en uppdatering av firmware eller integration kontrollerar SpotNav batteriet själv; ingen ny kod behövs. Går
den kontrollen inte igenom ber kortet om hårdvarutestet igen.

I 30 dagar från nu skickar betatestet en rapport per natt, så att jag kan se hur styrningen går. Avmarkera
Betatestare under Support för att stoppa dem när du vill; koden fortsätter att gälla.

**Återställ för hand** om du någon gång behöver: Battery Mode till Automatic, sedan Modbus Control av, och slå sedan
av SpotNavs batteriomkopplare.

SpotNav
