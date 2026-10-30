# PVPC archive days

Red Eléctrica's ESIOS archive 70 (`/archives/70/download_json?locale=es&date=…`), the file Home Assistant's
`pvpc_hourly_pricing` integration downloads by default (library `esios_api`, `URL_PUBLIC_PVPC_RESOURCE`).
Copied unchanged from `spotnav-relay/testdata/upstream/` (relay commit `dc28226`), where the relay's own PVPC
client is tested with them:

| file | what it is |
|---|---|
| `esios_PVPC_2026-10-04.json` | an ordinary day, 24 hours |
| `esios_PVPC_2025-10-26_dst_autumn.json` | the autumn clock change, 25 hours |
| `esios_PVPC_2026-03-29_dst_spring.json` | the spring clock change, 23 hours |

The tests turn a file into the integration's coordinator data the way the library does
(`esios_api.parser.extract_prices_from_esios_public`): the day's first hour at midnight of Home Assistant's
zone, one hour per row, `PCB` (or `CYM`) with its decimal comma divided by 1000 and rounded to five decimals.
