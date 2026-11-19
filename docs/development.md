---
title: Development
description: Run the tests, build the card, and contribute code or translations.
order: 97
---

# Development

```sh
pip install -r requirements_test.txt
pytest tests/
cd frontend && npm ci && npm test && npm run check-dist
```

The card source is in `frontend/`. The compiled bundle in `custom_components/spotnav/www/` and its language files in
`www/i18n/` are committed. `npm run build` regenerates both, and `npm run check-dist` fails while either is stale.

`docs/supported.md` is generated from the integration's own tables: edit `tools/gen_supported.py` and run
`python tools/gen_supported.py`, never the page itself.

Pull requests are welcome, for code as well as [translations](translating.md).

## Reporting a problem

Open an issue with the [bug report form](https://github.com/henrikekblad/spotnav-home-assistant/issues/new/choose),
then send the debug info from the card (**Settings → Support → Send debug info**) and mention the reference it shows.
Or download the file and e-mail it to **support@sensnology.se** with the issue's number in the subject. The file holds
your entity names and charging times, so do not attach it to the public issue. See
[Troubleshooting](troubleshooting.md).
