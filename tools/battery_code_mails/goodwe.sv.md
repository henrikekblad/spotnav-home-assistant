Ämne: SpotNav: test av styrningen av ditt GoodWe-batteri

Hej,

Tack för att du vill testa SpotNavs styrning av ditt GoodWe-batteri. Så här går det till.

**Före testet.** Styrningen behöver HACS-integrationen (mletenay/home-assistant-goodwe-inverter): integrationen i
Home Assistant core saknar eco-laddningen och dess värden. Operation Mode ska stå på General. Anteckna gärna Operation
Mode, Eco Mode Power, Eco Mode SoC och Depth of Discharge (on grid) innan. Styr ett annat system batteriet, pausa det
först: testet vägrar annars att köras.

**Hårdvarutestet.** I SpotNav-kortet: Inställningar → Support → Hårdvarutest, tryck sedan på Start. Det är allt: du
behöver inte göra eller titta på något mer. Testet väntar själv på ett lämpligt tillfälle (huset drar mer än solen
ger, batteriet ligger mellan sina gränser, ingen bil laddar) och körs bara dagtid, 08–21. Det tar ungefär tio
minuter. Medan det pågår får dina telefoner med SpotNavs aviseringar ett meddelande med knappen "Stoppa testet".

Vad det gör, via HACS-integrationen för GoodWe:

1. Hålla: Depth of Discharge (on grid) så att gränsen hamnar på nivån nu; batteriet ska sluta täcka huset. Det här
   är inte bekräftat på en växelriktare: laddar batteriet från nätet i stället lämnas det tillbaka.
2. En liten nätladdning: Eco Mode SoC till 100, Eco Mode Power till en andel av batteriets märkeffekt (högst 2 kW,
   mindre om säkringen kräver det) och Operation Mode till Eco Charge. Laddningen bedöms på batteriets effekt, eftersom
   en del firmware struntar i SoC-taket.
3. Täcka huset: Operation Mode till General.
4. Lämna tillbaka: Operation Mode, Eco Mode Power, Eco Mode SoC och Depth of Discharge till de värden de hade.

Inget av detta slutar av sig självt om Home Assistant stannar, så nätladdningen hålls under SpotNavs tak för
säkringen. Integrationen läser inte av värdena av sig själv, så en ändring syns först i Home Assistants eget eko.
Testet avbryts direkt och lämnar tillbaka om en skrivning inte läses tillbaka, om batteriet inte gör som det blir
tillsagt eller om en fas närmar sig huvudsäkringen. Resultatet skickas till mig av sig självt när testet är klart,
så länge Betatestare är på.

**Avbryta.** Tryck på "Stoppa testet" i aviseringen, eller "Avbryt testet" i kortets hårdvarutest, när som helst.
Vill du sluta testa helt (då skickas inget mer): avmarkera Betatestare under Support.

**Om något blir kvar** (till exempel om Home Assistant startade om mitt i testet), återställ för hand i Home
Assistant: Operation Mode till General (eller ditt eget läge), Depth of Discharge (on grid) till ditt värde och Eco
Mode Power och Eco Mode SoC till vad de var, och slå sedan av SpotNavs batteriomkopplare.

Jag läser testets logg och svarar dig via e-post.

SpotNav

---8<--- code
Ämne: SpotNav: din kontrollkod för GoodWe

Hej,

Tack för att du körde hårdvarutestet. Jag har läst loggen och det ser bra ut, så här är kontrollkoden för din
installation:

{code}

Klistra in den i SpotNav-kortet: Inställningar → Support → Hårdvarutest → Kontrollkod, tryck sedan på Skriv in.
Mellanslag och radbrytningar spelar ingen roll. Koden gäller bara din Home Assistant-installation och din GoodWe.
Sedan kan du slå på styrningen i avsnittet Hembatteri ("Batteristyrning").

Vad styrningen gör på din GoodWe: en hållning sätter Depth of Discharge så att gränsen är nivån nu, en nätladdning
är Eco Charge med Eco Mode SoC på 100 och Eco Mode Power på en andel av märkeffekten, och att täcka huset är General.
Den säljer aldrig från batteriet. Inget slutar av sig självt om Home Assistant stannar, så en nätladdning hålls
alltid under taket för säkringen och slutar med planens kvart. En hållning eller laddning som tar mer från nätet än
den fick lämnas tillbaka och provas inte igen på sex timmar. Varje återlämning sätter läget, eco-värdena och Depth of
Discharge tillbaka som de var.

Efter en uppdatering av firmware eller integration kontrollerar SpotNav batteriet själv; ingen ny kod behövs. Går
den kontrollen inte igenom ber kortet om hårdvarutestet igen.

I 30 dagar från nu skickar betatestet en rapport per natt, så att jag kan se hur styrningen går. Avmarkera
Betatestare under Support för att stoppa dem när du vill; koden fortsätter att gälla.

**Återställ för hand** om du någon gång behöver: Operation Mode till General, Depth of Discharge till ditt värde och
eco-värdena till vad de var, och slå sedan av SpotNavs batteriomkopplare.

SpotNav
