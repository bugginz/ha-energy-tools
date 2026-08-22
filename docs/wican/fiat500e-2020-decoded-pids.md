# Fiat 500e (2020+) — decoded UDS PIDs for WiCAN

Derived from two Car Scanner adapter logs of Rob's 2024 500e (VIN ZFAEFA265R…):
2026-07-13 (car parked, cold, ~93 % SoC) and 2026-08-18 (just driven, ~90 % SoC,
Car Scanner "full scan" + its built-in *Fiat 500e (2020-) BEV* profile polling).
Re-run the analysis with `tools/wican_log_decode.py <log> --decode`.

Bus: ISO 15765-4 CAN 29-bit 500 kbit (`ATSP7`, `ATCP18`). Requests go to
`18DA<ecu>F1`, replies come from `18DAF1<ecu>`. Cross-checks between independent
fields (cell sum vs pack voltage, A009 min/max vs the 96 cell readings, HA's
recorded `sensor.wican_soc_real`) are what give the confidence ratings below.

## WiCAN byte indexing (matters for every expression here)

WiCAN's `B<n>` indexes the **raw CAN frames including every ISO-TP PCI byte**
([meatpiHQ/wican-fw#663](https://github.com/meatpiHQ/wican-fw/discussions/663)):

```
single frame   :  B0=0L  B1=62  B2=DIDhi  B3=DIDlo  B4=data0  B5=data1 …
multi  frame FF:  B0=1x  B1=len B2=62     B3=DIDhi  B4=DIDlo  B5=data0 B6=data1 B7=data2
             CF1:  B8=21  B9=data3 … B15=data9
             CF2:  B16=22 B17=data10 … B23=data16
```

So for `22A010` (`10 09 62 A0 10 d0 d1 d2 | 21 d3 d4 d5 …`) the existing
`soc_real = B9*100/255` reads **d3**. Confirmed: HA recorded 89.8 % (0xE5) at
20:20 on 2026-08-18 while d3 was 0xE5. A 16-bit value that straddles a frame
boundary (data2/data3, data9/data10 …) can't use `[Bx:By]` — write `Bx*256+By`.
`tools/wican_log_decode.py --wican-index <payloadhex>` prints the mapping.

Verify any new expression with the **Test** button in the WiCAN UI while the
car is awake (charging, or ignition on) — the BPCM sleeps otherwise.

## ECUs seen

| Addr | Role (best guess) | Notes |
|------|-------------------|-------|
| 0x44 | BPCM — battery pack control module | all the battery data below; community-confirmed for SoC |
| 0x42 | EV/hybrid control processor (HCP/EVCU) | answers VIN (mode 09, 22F190), HV voltage, powertrain temps; OVMS also targets it |
| 0x40, 0x1F, 0x50, 0x60, 0xCB | present (answered tester-present 3E00) | not probed |

## ECU 0x44 (init `ATSHDA44F1;`) — battery

| DID | Field (data byte offsets) | WiCAN expression | Value seen | Confidence |
|-----|---------------------------|------------------|------------|------------|
| `22A010` | d0 = SoC of lowest cell, d1 = SoC of highest cell, d2/d3/d4 = pack SoC estimates, d5 ≈ d0-2 (unknown) | existing `B9*100/255` (d3); d0 = `B5*100/255`, d1 = `B6*100/255` | 89.4 / 91.0 / 89.8 / 89.8 / 89.8 / 88.6 % | high (d0/d1 = min/max of the 108 per-cell SoCs in `22B0xx`) |
| `22A011` | d0-1 pack voltage ×0.1 V (d2-3, d4-5, d22-23 near-identical alt. measurements) | `[B5:B6]/10` | 390.4 V (Aug), 393.1 V (Jul) | high (= sum of 96 cells) |
| `22A009` | d0 max-cell index, d1 min-cell index (0-based), d2-3 max cell mV, d4-5 min cell mV, d8-9 max temp, d10-11 min temp (°C, 0x8000 offset) | max mV `B7*256+B9`, min mV `[B10:B11]`, Tmax `[B14:B15]-32768`, Tmin `B17*256+B18-32768` | cells 27 / 67, 4070 / 4054 mV, 16 / 15 °C | high (matches `22A1xx` cell list exactly) |
| `22A021` | d0-1 max temp, d2-3 min temp, d4-5 third temp (°C, 0x8000 offset) | `[B5:B6]-32768`, `B7*256+B9-32768`, `[B10:B11]-32768` | 17 / 15 / 23-25 °C | high for the first two |
| `22A029` | d3-4 nominal capacity ×0.1 Ah, d5-6 current capacity ×0.1 Ah; d0-1 slowly declining (SOH-like ×1/655.35) | `[B9:B10]/10`, `[B11:B12]/10`; SOH `[B11:B12]/[B9:B10]*100`; alt `[B5:B6]/655.35` | 120.0 Ah / 117.6 Ah → 98.0 %; alt 98.0 % (Jul) → 97.6 % (Aug) | medium — units fit a 42 kWh 96s pack of 120 Ah cells; needs months of trend |
| `22A001` | five 24-bit counters: f0 (d0-2), f1 (d3-5), f2 (d6-8), f3 (d9-11), f4 (d12-14) | f0 `(B5*65536+B6*256+B7)/10`, f1 `[B9:B11]/10`, f2 `[B12:B14]/10`, f3 `(B15*65536+B17*256+B18)/10`, f4 `[B19:B21]/10` | f0 26 785.8→40 809.0 (+14 023 in 5 wk); f1 11 269→11 769 Ah; f2 11 168→11 670 Ah; f3 4 140.7→4 327.1 kWh; f4 4 247.8→4 436.4 kWh | medium — f3/f1 = 368 V, f4/f2 = 380 V (avg pack V), so f1-f4 are lifetime Ah / kWh throughput (charge vs discharge pairs). f0 is **not** the odometer (dash 17 569 km on 2026-08-22) — an unknown fast-growing counter |
| `22A200` | 18 module temperature sensors, °C = byte-40 | `B5-40`, `B6-40`, `B7-40`, `B9-40` … | 16-17 °C (Aug), 14-15 °C (Jul) | high (agrees with A009/A021) |
| `22A100`…`22A107` | 12 cell voltages each (mV, u16), 96 cells; `22A108` = zeros | e.g. cell 1 `[B5:B6]` | 4041-4072 mV, spread 16-18 mV, cell #67 always lowest | high |
| `22B004`…`22B00C` | 12 per-cell SoC bytes each (×100/255) | — | 89.4-91.0 % | high |
| `22A042` | 12 V battery, per community (`fiat_500e_2020_simple.txt`, [wican-fw#335](https://github.com/meatpiHQ/wican-fw/issues/335)) | probably `[B4:B5]/1000` (single frame) — untested, not in these logs | — | unverified |
| `22A060`, `22A070`, `22A071` | unknown (A060: nine slowly rising 16-bit counters) | — | — | — |

## ECU 0x42 (init `ATSHDA42F1;`) — vehicle controller

| DID | Field | WiCAN expression | Value seen | Confidence |
|-----|-------|------------------|------------|------------|
| `223062` | HV voltage ×0.02 V (u16) | `[B4:B5]/50` | 389.6 V (Aug), 394-396 V (Jul) | high (tracks A011) |
| `22B562` | same quantity, other source | `[B4:B5]/50` | 389-391 V | high |
| `224052` | d4-5 HV bus voltage, V | `[B10:B11]` | 391 (Aug), 394-396 (Jul) | high |
| `223027` | **odometer** ×0.1 **miles** (u24) | `[B4:B6]*0.160934` for km | 10 875.9 mi = 17 503 km (Aug 18); live check 2026-08-22: 10 916.9 mi × 1.60934 = 17 569.0 km = dash exactly | high — dash-verified |
| `223065` | 9 bytes, °C = byte-40, 0x28 = not fitted | `B5-40`, `B7-40`, `B9-40`, `B10-40` | 27-33 °C after driving (Aug), 16-17 °C cold (Jul) | high — powertrain (motor/inverter) temps |
| `224053` | d1 = °C+40 | `B6-40` | 17-18 (Aug), 12-13 (Jul) | medium (ambient/coolant?) |
| `224001` | single byte | `B4` | 18-22 (Aug, heater on), 9 (Jul cold) | medium — cabin or ambient temp |
| `224012` | single byte | `B4` | 96-97 | guess: 12 V battery SoC % |
| `22019E` | u16 = 849-857, slowly declining | — | **not** display SoC (the July profile guessed it was — 85.6 vs BMS 93 %, and it barely moves as SoC changes) | unknown |
| `22F190` | VIN (ASCII) | — | ZFAEFA265RX186237 | — |
| `223063`, `223064`, `220301`, `224011`, `224055` | static / unexplained | — | — | — |

## SoC: raw vs dash

`sensor.wican_soc_real` (d3) peaks at **95.7 % (0xF4)** when the car is full
(HA history 2026-08-14 and 08-17). **Calibrated 2026-08-22**: raw 87.84 →
dash 94 %. The [#95](https://github.com/meatpiHQ/wican-fw/issues/95) formula
wins:

    dash % = min(100, raw - (40 - raw)/7)        # = (8·raw − 40)/7, capped

It predicts 94.7 at that point (dash shows integers) and clips to 100 for
raw ≥ 92.5, consistent with raw peaking at 95.7 while the dash reads 100.
Rejected fits at raw 87.84: `raw/0.9-5` → 92.6, `(raw-4.5)/0.912` → 91.4,
tidbyt's `raw/95.5` → 92.0. Implemented as HA template sensor
`sensor.car_soc_display` (`template.yaml` on the Pi); it holds its last value
while the car sleeps, same as `soc_real`. A future low-SoC data point (< 50 %)
would further verify the slope.

Dead end (tested 2026-08-22): WiCAN Pro **Custom Filters** cannot sniff broadcast frame `0xC10A040`
(MSG31B_EVCU) which OVMS decodes as `d[0]` = estimated range km and `d[1]>>1` =
**display SoC** ([OVMS vehicle_fiat500e.cpp](https://github.com/openvehicles/Open-Vehicle-Monitoring-System-3/blob/master/vehicle/OVMS.V3/components/vehicle_fiat500/src/vehicle_fiat500e.cpp)),
but a filter on that ID reports **"no frame"** at the OBD port even with the car
awake and polling healthy — the gateway filters broadcast traffic off the OBD
pins (OVMS taps the C-CAN behind the gateway). Display SoC therefore has to be
computed from `soc_real` in HA.

## Behaviour notes

- Continuous polling keeps the car awake: during the 17-minute Aug scan the
  min-cell SoC fell 90.6 → 89.4 % (Car Scanner hammers dozens of PIDs; WiCAN at
  30 s is far gentler — July note estimated ~0.4 %/25 min).
- **An ELM TCP client (e.g. the Car Scanner app) connected to :35000 pauses the
  whole Automate engine** — ecu_status goes offline, every Test fails, and only
  the status webhook keeps flowing. Force-quit the app before debugging WiCAN
  (observed 2026-08-22 on v4.51p_beta-01).
- The BPCM answers only while awake (charging or ignition on). HA sees gaps
  otherwise; foxctl already ages the value out (`ev_soc_max_age_min`).
- Car Scanner's own Fiat 500e profile polls exactly `22A010/A011/A009/A021/
  A029/A001/A060/A070/A071/A200/A1xx/B0xx` on 0x44 and `223062/3027/3063/3064/
  3065/4001/4011/4012/4052/4053/4055/B562/019E/0301` on 0x42 — a good list of
  what the app authors consider meaningful.

## Recommended additions to WiCAN "User Custom" PIDs

Keep `soc_real` (22A010, `B9*100/255`). Add, all with init `ATSHDA44F1;` unless noted:

| Name | PID | Expression | Unit | Class |
|------|-----|-----------|------|-------|
| hv_voltage | 22A011 | `[B5:B6]/10` | V | voltage |
| cell_v_max | 22A009 | `B7*256+B9` | mV | voltage |
| cell_v_min | 22A009 | `[B10:B11]` | mV | voltage |
| batt_temp_max | 22A009 | `[B14:B15]-32768` | °C | temperature |
| batt_temp_min | 22A009 | `B17*256+B18-32768` | °C | temperature |
| batt_cap_ah | 22A029 | `[B11:B12]/10` | Ah | — |
| batt_soh | 22A029 | `[B11:B12]/[B9:B10]*100` | % | — |
| lifetime_kwh_a | 22A001 | `(B15*65536+B17*256+B18)/10` | kWh | energy (total_increasing) |
| lifetime_kwh_b | 22A001 | `[B19:B21]/10` | kWh | energy (total_increasing) |
| odometer | 223027, init `ATSHDA42F1;` | `[B4:B6]*0.160934` | km | distance (total_increasing) — native counter is 0.1 mi; dash-verified 2026-08-22 |

`cell_v_max - cell_v_min` (in HA) is the imbalance figure to watch; 16-18 mV at
90 % is healthy. Cell #67 is the persistent runt in both sessions.
