# Heat pumps (read only)

SpotNav finds a heat pump by its Home Assistant integration, with no setting, and reads what the pump reports.
**It controls nothing:** it writes no setting, no offset, no setpoint and no switch on any heat pump. What it reads
is shown in the diagnostics (`heat_pump`), as groundwork for heat control in a later release.

## Which pumps are read

| Pump | Integration | Notes |
| --- | --- | --- |
| NIBE F series (F730, F750, F1155, SMO 40, VVM ...) | NIBE Heat Pump (`nibe_heatpump`, local) | Its entities are disabled by default: enable the readings under "What is read" to have them read. |
| NIBE S series (S1155, S320, VVM S ...) | NIBE Heat Pump (`nibe_heatpump`, local) | As above. |
| NIBE (F series, SMO 20) | myUplink (`myuplink`, cloud) | Read through NIBE's cloud. |
| CTC (EcoZenith, EcoLogic, EcoAir ...) | CTC Heat Pump (BMS) (`ctc_bms`) or CTC Local Easyconf (`ctc_ecozenith`) | Local Modbus. |
| Panasonic Aquarea | HeishaMon (`aquarea`, MQTT) | Needs the HeishaMon board. The HeishaMon YAML package is not read. |

## What is read

Where the pump has it: the outdoor, supply, return, room and hot water temperatures, the degree minutes, the
compressor's speed and whether it runs, the immersion heater's power or state, and whether the pump is heating the
house or the hot water now. A reading that is `unknown` or `unavailable`, belongs to a disabled entity, or is out of
any plausible range (NIBE's "no sensor" -3276.8, for example) is left out, and the diagnostics say why.

SpotNav also lists the controls a pump offers (heat curve offset, setpoint, SG Ready through the integration, extra
hot water, compressor limits) with their entities and ranges. It only lists them.

## The pump's own price control

When the pump runs its own price control (NIBE Smart Price Adaption, CTC's SmartGrid or spot price mode), or
EffektGuard is set up, the diagnostics mark the pump as **watch only**: a later SpotNav heat control will leave such
a pump to it and only watch.
