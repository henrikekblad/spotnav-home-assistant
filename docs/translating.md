# Translating SpotNav

Every text SpotNav shows lives in a JSON file per language. English is the source: every other
language has exactly the English keys, with the same `{placeholders}`. Today SpotNav speaks English
(`en`), Swedish (`sv`), Danish (`da`), Norwegian Bokmål (`nb`), Finnish (`fi`), German (`de`), Dutch
(`nl`), French (`fr`) and Spanish (`es`).

## Where the texts are

| Folder | What it holds | Format |
| --- | --- | --- |
| `custom_components/spotnav/translations/<lang>.json` | What Home Assistant itself shows: setup and options dialogs, entity names and states, Repairs issues, error messages. | Home Assistant's own [translation format](https://developers.home-assistant.io/docs/internationalization/core). |
| `custom_components/spotnav/i18n/<lang>.json` | What SpotNav words in code: notifications, dropdown labels, the summaries in the setup dialogs, the default site name. | `namespace → key → text`. |
| `frontend/src/i18n/<lang>.json` | The card. | `key → text`, one flat object. |

Some sentences appear in more than one place and should read the same, for example the card's
`state.addCharger` and `add_charger.hint` in `i18n/`. Keep a language's terms the same in all
three folders: the card's file is the reference for established words (charger, site, departure,
charge limit and so on).

Placeholders such as `{name}` or `{amps}` are filled in by the code. Keep each one exactly, and
write the sentence so it reads well whatever is filled in. Leave Markdown (`**bold**`, line breaks),
product names and links as they are.

## Adding a language

1. Copy `translations/en.json`, `i18n/en.json` and `frontend/src/i18n/en.json` to `<lang>.json` in
   the same folders, with the language's code as Home Assistant uses it (`de`, `nl`, ...), and
   translate the values. Never change a key.
2. In `frontend/src/i18n/index.ts`, import the new file and add the code to `LANGUAGES` and
   `TRANSLATIONS`. The integration's side needs no code: it reads every file in `i18n/`, and Home
   Assistant reads every file in `translations/`.
3. Run the checks:
   - `pytest tests/test_translation_files.py`: every file has exactly the English keys, none empty,
     with the same placeholders.
   - In `frontend/`: `npm run type-check` (a missing key is a compile error), `npm test` (extra keys,
     placeholders, and a file not listed in `LANGUAGES`), then `npm run build` for the card's asset.

A key missing from an `i18n/` file falls back to English for that key, so a half-done language does
not break anything, but the tests above fail until it is complete.

Numbers in notifications are written with a decimal comma in every language except English
(`notifications/messages.py`); a language that needs another rule needs a line of code there. Dutch
writes the euro sign before the amount (`€ 4,50`), the other languages after it (`4,50 €`). The card
takes its numbers and dates from `Intl`, but the plural weekday in "Sundays were 30 % cheaper" is
built per language in `weekdayPlural` (`frontend/src/format.ts`), so a new language needs a case there.

## Glossaries

The words each language uses for SpotNav's recurring terms. Use them in all three folders and in the
Android app (its `strings.xml`, its status line tables and its store texts), so a new text reads like
the old ones and the card and the app call the same thing by the same name. This table is the single
reference for both. Product names (SpotNav, Home Assistant, Easee, OCPP, Zaptec, Tibber,
Octopus Energy), entity ids and the price areas' names, which come from the relay, are never
translated.

| English | German (`de`) | Dutch (`nl`) | French (`fr`) | Spanish (`es`) |
| --- | --- | --- | --- | --- |
| charger | Ladestation | laadpaal | borne | cargador |
| onboard charger (car) | Bordlader | boordlader | chargeur embarqué | cargador de a bordo |
| site | Standort | locatie | site | instalación |
| departure / departure time | Abfahrt / Abfahrtszeit | vertrek / vertrektijd | départ / heure de départ | salida / hora de salida |
| deadline | Abfahrtszeit | deadline | échéance | hora límite |
| charge limit (the car's) | Ladegrenze | laadlimiet | limite de charge | límite de carga |
| minimum charge level | Mindestladestand | minimaal laadniveau | niveau de charge minimum | nivel de carga mínimo |
| charge target | Ladeziel | laaddoel | objectif de charge | objetivo de carga |
| state of charge, charge level | Ladestand | laadniveau | niveau de charge | nivel de carga |
| plan / charging plan | Plan / Ladeplan | plan / laadplan | plan / plan de charge | plan / plan de carga |
| charging (the act), a planned charge | Laden, Ladevorgang | laden | charge | carga |
| charging period | Ladezeitraum | laadperiode | période de charge | periodo de carga |
| solar surplus | Solarüberschuss | zonne-overschot | surplus solaire | excedente solar |
| grid fee (grid transfer) | Netzentgelt | nettarief | frais de réseau | peaje de acceso |
| energy tax | Stromsteuer | energiebelasting | taxe sur l'électricité | impuesto sobre la electricidad |
| VAT | MwSt. | btw | TVA | IVA |
| spot price | Spotpreis | dynamische prijs | prix spot | precio spot |
| price area | Preiszone | prijsgebied | zone de prix | zona de precio |
| load balancing | Lastmanagement | load balancing | équilibrage de charge | equilibrado de carga |
| home battery (short: battery) | Hausbatterie (Batterie) | thuisbatterij (batterij) | batterie domestique (batterie) | batería doméstica (batería) |
| main fuse | Hauptsicherung | hoofdzekering | fusible principal | fusible general |
| safety margin | Sicherheitsreserve | veiligheidsmarge | marge de sécurité | margen de seguridad |
| grid meter | Netzzähler | netmeter | compteur réseau | contador de red |
| energy meter, energy register | Energiezähler | energiemeter | compteur d'énergie | contador de energía |
| import / export (grid) | Bezug / Einspeisung | afname / teruglevering | import / export | importación / exportación |
| vehicle / car | Fahrzeug / Auto | auto | véhicule / voiture | vehículo / coche |
| a charge (history) | Ladevorgang | laadsessie | recharge | carga |
| plugged in / unplugged | angeschlossen / abgesteckt | aangesloten / losgekoppeld | branchée / débranchée | enchufado / desenchufado |
| Which car is plugged in? | Welches Auto ist angeschlossen? | Welke auto is aangesloten? | Quelle voiture est branchée ? | ¿Qué coche está enchufado? |
| Change car | Auto wechseln | Andere auto | Changer de voiture | Cambiar coche |
| the car's position (identification) | Position | positie | position | posición (source: ubicación) |
| reference picture | Referenzbild | referentiefoto | image de référence | imagen de referencia |
| parking spot / Crop parking spot | Stellplatz / Stellplatz zuschneiden | parkeerplaats / Parkeerplaats bijsnijden | place de stationnement / Recadrer la place | plaza de aparcamiento / Recortar la plaza |
| notification | Benachrichtigung | melding | notification | notificación |
| start / stop / pause / resume | starten / stoppen / pausieren / fortsetzen | starten / stoppen / pauzeren / hervatten | démarrer / arrêter / mettre en pause / reprendre | iniciar / detener / pausar / reanudar |
| automatic price planning (Auto) | automatische Preisplanung | automatische prijsplanning | planification automatique selon le prix | planificación automática por precio |
| Cheapest / Solar / Hybrid | Günstigste / Solar / Hybrid | Goedkoopst / Zon / Hybride | Moins cher / Solaire / Hybride | Más barata / Solar / Híbrida |
| strategy | Strategie | strategie | stratégie | estrategia |
| schedule (paused / active) | Zeitplan | schema | programmation | programación |

The site is never the car's position: German and Dutch call the position Position / positie, so it
does not read as Standort / locatie. French uses "charge" for charging and the plan (charge planifiée,
plan de charge) and "recharge" only for a recorded charge in the history.

The app words Home Assistant's status codes with tables in `HaStatusWording.kt` that are copies of
this card's `status.*`, `issue.*`, `control.paused*` and `strategy.status.*` texts. When one of those
texts changes here, copy it into the app's table for the same language.

German uses "du", as Home Assistant's German does, Dutch "je", Spanish "tú" and French "vous". German
never shortens automatic price planning to "Auto", which means car.
