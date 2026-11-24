# Fake spa and sauna (for trying SpotNav's Spa and Bastu tabs)

Two FAKE integrations for a real Home Assistant with no spa or sauna. Nothing reaches a network or a device; the
temperatures are an in-memory model that starts over at every restart. Remove them once done.

| Folder | Pretends to be | Entities | Model |
|---|---|---|---|
| `balboa/` | core Balboa (local) | `climate.balboa_spa_fake` (presets ready / rest / ready_in_rest, hvac heat = Ready, off = Rest, 26.5-40 °C) | water starts at 30 °C, +2 °C/h in Ready up to the setpoint, -0.3 °C/h otherwise |
| `saunum/` | core Saunum (local) | `climate.saunum_leil_fake` (off / heat, 40-100 °C), `binary_sensor.saunum_leil_fake_door`, `number.saunum_leil_fake_sauna_duration`, `switch.saunum_leil_fake_door_open_fake_control` (opens the fake door) | air starts at 20 °C, +1.5 °C/min in a session up to the setpoint, -0.3 °C/min otherwise; a session ends after its duration or when the door opens; heat is refused with the door open |

Each overrides the core integration of the same domain (a custom component wins), so install them only where that
integration is not used. Device names end in "(fake)" and the climates carry the attribute `fake_device: yes`.

## Install

The repository keeps the manifests as `manifest.demo.json` (the repository may hold only one `manifest.json`).

```sh
HA=root@homeassistant.local   # ssh add-on, port 22222
for d in balboa saunum; do
  ssh -p 22222 $HA "test ! -e /config/custom_components/$d" || { echo "$d exists, stop"; break; }
  tar -C tools/fake_devices -cf - --transform "s,manifest.demo.json,manifest.json," $d \
    | ssh -p 22222 $HA "tar -C /config/custom_components -xf -"
done
```

Restart Home Assistant, then Settings > Devices & services > Add integration > "Balboa Spa Client (fake)" and
"Saunum (fake)" (one click each, nothing asked).

## Remove

1. In SpotNav's heat settings, unselect the spa and the sauna (or turn their control off).
2. Settings > Devices & services: delete the "Balboa spa (fake)" and "Saunum Leil (fake)" entries.
3. `ssh -p 22222 $HA "rm -r /config/custom_components/balboa /config/custom_components/saunum"`
4. Restart Home Assistant.

The test `tests/test_fake_devices.py` checks that SpotNav's detection finds them as a controllable Balboa spa and a
Saunum sauna with its door.
