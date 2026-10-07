# Translating SpotNav

Every text SpotNav shows lives in a JSON file per language. English is the source: every other
language has exactly the English keys, with the same `{placeholders}`. Today SpotNav speaks English
(`en`), Swedish (`sv`), Danish (`da`), Norwegian Bokmål (`nb`) and Finnish (`fi`).

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
(`notifications/messages.py`); a language that needs another rule needs a line of code there.
