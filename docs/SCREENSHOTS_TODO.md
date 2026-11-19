---
title: Screenshots to take
description: Screenshots the documentation refers to that do not exist yet. Not a page for the website.
hidden: true
---

# Screenshots to take

The pages already refer to these files. Until a file exists, the page shows its alt text. Run
`pytest tests/test_docs_links.py -s` for the current list. Exclude this file from the website build.

| File (under `docs/images/`) | What it should show | Used in |
| --- | --- | --- |
| `readme-hero.png` | The charging card, the house card and the heat card side by side on one dashboard, light theme, about 1600×900. | README.md |
| `docs-home.png` | The same three cards, or a cropped version of `readme-hero.png`. | index.md |
| `house-card-now.png` | The house card's **Now** tab with sun, grid, battery, car and heat, and the battery sentence under the picture. | house-card.md |
| `heat-card-now.png` | The heat card's **Now** tab: the dial, Home or Away, the next twelve hours strip, with **Test** in the header. | heat-pumps.md |
| `heat-card-hot-water.png` | The heat card's **Plan** tab with planned hot-water runs (hollow droplets), measured runs and the peak ticks, and the shadow progress line. | hot-water.md |
| `settings-home-battery.png` | Settings, **Home battery**: the mode, **Battery control** with its status line, the checklist and the check's steps. | home-battery.md |
| `settings-beta.png` | Settings, **Support**: the **Beta tester** box with **Read more** open, and **Run hardware test**. | beta-testing.md |

Use made-up names and figures. No real addresses, names or device serials in a picture.
