---
title: Privacy
description: What leaves your Home Assistant, when, and what is kept.
order: 82
section: Help
---

# Privacy

SpotNav runs inside your Home Assistant. It has no account and no cloud service of its own, and it sends nothing
about your installation unless you ask it to.

## What your Home Assistant fetches

These requests go from your own Home Assistant to public services. They carry no data about your home beyond what
is listed.

| What | From | What is sent |
| --- | --- | --- |
| Spot prices | ENTSO-E, when you use your own key (Nord Pool, PVPC, Octopus Energy and Zonneplan are fetched by their own Home Assistant integrations) | The price area and your key. |
| Exchange rates | The European Central Bank | Nothing. |
| Irradiance forecast, for SpotNav's own solar forecast | Open-Meteo | Your home position rounded to 0.1° (about 10 km). Only on a site with solar production. |
| Great Britain region lookup | Octopus Energy's public lookup | The postcode you type, once. It is not stored or logged. |

## What you can choose to send

- **Send debug info** (administrators, in **Settings → Support**). One redacted file, sent only when you press
  **Send**. Kept 30 days. See [Troubleshooting](troubleshooting.md#send-debug-info).
- **Beta tester** (administrators, off by default). The same file once a night for 14 days, then it stops by itself.
  Beta reports and an optional e-mail address are kept 90 days. See [Beta testing](beta-testing.md).

The file holds versions, settings, entity names and recent logs. It never holds passwords, tokens, e-mail addresses
or your exact location. The solar forecast's review states only the latitude in whole degrees.

## The SpotNav app

- Pairing hands the app one private webhook per charger. No Home Assistant password or token is stored on the
  phone. See [The SpotNav app](app.md).
- **Instant notifications** are opt-in in the app. When an event happens, Home Assistant sends the SpotNav relay only
  an opaque reference. The relay wakes the app with an empty message, and the app then asks your Home Assistant
  directly. Nothing about the charge passes through the relay.

## The camera

If you use a camera to tell cars apart, the pictures go to the AI Task entity you chose in Home Assistant. With a
local model they never leave your home. See [Which car is plugged in?](vehicle-identification.md#the-camera).

The full [privacy policy](https://spotnav.sensnology.se/privacy) is on the SpotNav website.
