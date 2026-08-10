# Hardware notes

Nothing in this repository depends on final hardware choices. Every pin, ratio,
and limit is configuration. This file records what the firmware expects and
what still has to be filled in.

## Fill these in before flashing

`firmware/include/board_config.h` contains **placeholder GPIO numbers** guarded
by `TURRET_PINS_CONFIGURED`. The firmware refuses to build unless you either
set that flag in `platformio.ini` (`-D TURRET_PINS_CONFIGURED=1`) or override
the pins there. This is deliberate: a wrong step/dir pin can drive a stepper
into a hard stop on first boot.

Pins to assign:

| Signal              | Macro                     |
| ------------------- | ------------------------- |
| Pan step / dir / en | `PIN_PAN_STEP/DIR/EN`     |
| Tilt step / dir / en| `PIN_TILT_STEP/DIR/EN`    |
| Pan min/max endstop | `PIN_PAN_MIN/MAX_ENDSTOP` |
| Tilt min/max endstop| `PIN_TILT_MIN/MAX_ENDSTOP`|
| Valve output        | `PIN_VALVE`               |
| E-stop input        | `PIN_ESTOP`               |
| Status LED          | `PIN_STATUS_LED`          |
| TMC UART (optional) | `PIN_TMC_UART_TX/RX`      |

Set `PIN_*` to `-1` to disable an optional input (e.g. max endstops, e-stop).

## Steppers

Assumed drivers: TMC2209 (or any STEP/DIR driver). The firmware generates step
pulses from a 20 kHz hardware-timer ISR (`gptimer`) that only toggles pins and
checks limits, so pulse timing is unaffected by what Wi-Fi or the planner are
doing. That rate is also the per-axis step ceiling: 20 kHz ÷ steps-per-degree.

Angle conversion (per axis):

```
steps_per_output_deg = steps_per_rev * microsteps * gear_ratio / 360
```

`gear_ratio` is motor revolutions per output revolution (a 6:1 belt reduction
is `6.0`).

TMC UART configuration (current, stealthChop) is **not** implemented — the
drivers are expected to be configured by their onboard potentiometer/straps.
The UART pins are reserved so it can be added without a board change.

## Valve

The valve output drives a MOSFET/relay for a 12 V solenoid.

* Idle level is configurable (`VALVE_ACTIVE_HIGH`), and the GPIO is driven to
  the **inactive** level before it is switched to output mode, so a reset never
  produces a pulse.
* Use a flyback diode across the solenoid.
* The firmware enforces `max_spray_ms` with an esp_timer one-shot armed *before*
  the valve opens. Independently, the link watchdog and the task watchdog both
  close it.
* Recommended: a normally-closed solenoid, so loss of power = no water.

## Endstops

Mechanical or optical, wired to ground with the internal pull-up enabled
(`endstop_active_low = true`, the default). Debounced in firmware (5 ms) and
also checked inside the step ISR path for immediate stop.

Homing sequence per axis: seek toward `home_dir` at `homing_speed_deg_s` →
stop on endstop → back off `homing_backoff_deg` → re-seek at ¼ speed → set
position to `*_home_offset_deg`.

## Power

Motors and the solenoid must **not** share the ESP32's 3V3 regulator. Common
ground only. Brown-out during a spray is the most likely cause of a controller
reset in this design; size the supply for the solenoid inrush.

## Camera

Any RTSP source. Tested targets: UniFi Protect G5 Turret, Reolink Duo WiFi.

* UniFi Protect: enable the RTSP stream per channel in Protect, then use
  `rtsp://<nvr-ip>:7447/<stream-key>`. Prefer the *low* or *medium* substream
  for detection — full 4K costs latency and CPU for no accuracy gain at these
  object sizes.
* Reolink: `rtsp://<user>:<pass>@<ip>:554/h264Preview_01_sub` (substream) or
  `..._main`. The Duo's two lenses appear as separate channels.

Put the password in the environment file and reference it in the URL as
`${CAM_PASSWORD}` — see `server/.env.example`.
