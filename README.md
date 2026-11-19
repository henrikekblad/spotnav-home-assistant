# SpotNav for Home Assistant

**Learns your home. Plans your energy.**

Local control for your EV, battery and heating, planned around spot prices, solar and weather. Built for Home
Assistant.

![The SpotNav charging, house and heat cards on a Home Assistant dashboard](docs/images/readme-hero.png)

## What it does

- **Charges your car in the cheapest hours** before you leave, to the level you ask for, within your main fuse.
- **Uses your solar surplus** for the car, and plans around the solar forecast.
- **Plans your home battery** against the spot price, in shadow first: it shows what it would do before it controls
  anything.
- **Moves heating and hot water** away from expensive hours, behind an opt-in and its own checks.
- **Runs locally** in Home Assistant, with a dashboard card for the car, the house and the heat, and an Android app.

## Install

1. In Home Assistant, open **Settings → Marketplace** (HACS on versions before 2026.11), search for **SpotNav** and
   install it.
2. Go to **Settings → Devices & services → Add integration → SpotNav**, and add your charger or your site. SpotNav
   finds your charger, grid meter, battery and heat pump.
3. Add the **SpotNav** card to a dashboard. The card comes with the integration: there is
   no dashboard resource to add.

**[Read the documentation]({{SITE_URL}})** (the same pages are in [docs/](docs/index.md)).

## Support

- Questions and problems: [Troubleshooting](docs/troubleshooting.md), then a
  [GitHub issue](https://github.com/henrikekblad/spotnav-home-assistant/issues/new/choose).
- Tell us how your hardware works: a short hardware report on the same page helps the next person.
- Like it? Star the repository, or support the project through
  [GitHub Sponsors](https://github.com/sponsors/henrikekblad).

SpotNav sends nothing about your installation unless you ask it to. See [Privacy](docs/privacy.md).

## License

[MIT](LICENSE). Copyright Sensnology AB.
