# Turret controller firmware

ESP-IDF (no Arduino), built with PlatformIO. The controller owns everything
that must keep working when the network does not: step generation, limits,
homing, and the valve's hard timeout.

## Before you build

1. **Check the pins.** `include/board_config.h` carries a working set for a
   standard ESP32 DevKit, and `env:esp32dev` builds with them. They are still
   only defaults: confirm each one against your wiring before you flash. A
   wrong STEP pin is not a compile error, it is a stepper driving into a hard
   stop on first boot.

   If you rewire, comment `-DTURRET_PINS_CONFIGURED=1` out of `platformio.ini`
   so the build stops with the `#error` until you have re-checked.

2. **Add credentials.**

   ```bash
   cp include/secrets.example.h include/secrets.h
   $EDITOR include/secrets.h      # Wi-Fi, server URI, controller token
   ```

   `include/secrets.h` is git-ignored. The token must match
   `TURRET_CONTROLLER_TOKEN` in the server's environment file.

   The Wi-Fi password is set specifically in `include/secrets.h`:

   ```c
   #define TURRET_WIFI_SSID "your-network"
   #define TURRET_WIFI_PASSWORD "your-password"
   ```

   Wi-Fi, the server URI, the controller token and GPIO pins are flash-time
   settings. Once the ESP32 joins Wi-Fi it initiates the server connection, so
   no network scan or pairing step is required. Motion, mechanics, homing,
   limits, timeouts and other normal operating settings are editable in
   **Settings → Controller/Motion** and pushed to persistent ESP32 storage.

## Opening it in VS Code

The PlatformIO extension only activates when `platformio.ini` sits in a
**workspace root folder**, and this one lives in `firmware/`. So either:

* open `firmware/` directly as the VS Code folder, or
* open `pigeon-tracker.code-workspace` from the repository root — it lists
  `firmware/` as a second root, so PlatformIO activates while the server code
  stays in the same window.

Install the **PlatformIO IDE** extension (`platformio.platformio-ide`). It
brings its own Python and PlatformIO Core; nothing else is needed. The first
build downloads the Xtensa toolchain and ESP-IDF — expect a gigabyte or two and
several minutes.

## Build and flash

```bash
pio run                 # build
pio run -t upload       # flash
pio device monitor      # serial log (115200)
pio run -t menuconfig    # ESP-IDF configuration
```

From the repository root, add `-d firmware` to any of those.
To choose a specific Windows serial adapter, append
`--upload-port COM4` when uploading and use `pio device monitor -p COM4`.

### Verified build

`env:esp32dev` compiles clean against ESP-IDF 5.5 (PlatformIO espressif32
6.12), with pins assigned for a standard ESP32 DevKit:

```
RAM:   11.1% (36 424 / 327 680 bytes)
Flash: 29.6% (929 927 / 3 145 728 bytes)
```

The 3 MB comes from `partitions.csv`; on the default 1 MB single-app table
this firmware sits at 89 % before you have added anything.

**Check `include/board_config.h` against your own wiring before flashing.**
The defaults avoid the ESP32's traps (input-only pins without pull-ups for
endstops, strapping pins for outputs) but they cannot know what you soldered.
If you rewire, comment `-DTURRET_PINS_CONFIGURED=1` out again in
`platformio.ini` so the build stops until you have re-checked.

`env:esp32-s3` is still guarded on purpose: those pin numbers are wrong for an
S3, where GPIO 26-32 belong to the flash/PSRAM on most modules.

Managed components (`esp_websocket_client`) are fetched automatically from the
ESP Component Registry on the first build — see `src/idf_component.yml`.

## What runs where

| Task           | Rate    | Job                                                  |
| -------------- | ------- | ---------------------------------------------------- |
| step ISR       | 20 kHz  | step pulses, endstop/soft-limit enforcement          |
| `motion`       | 1 kHz   | velocity ramps, trapezoidal moves, homing sequence   |
| `ws_status`    | 10 Hz   | status frames, link-loss failsafe                    |
| `ws_home`      | on demand | runs homing without blocking the WebSocket loop    |
| `safety`       | 20 Hz   | e-stop input, task watchdog, status LED              |
| esp_timer      | one-shot | closes the valve when a burst expires               |

## Failsafe behaviour

The valve closes and motion stops when **any** of these happen:

* the WebSocket disconnects, or no server frame arrives within
  `link_timeout_ms` (default 6 s);
* `stop`, `arm_output {armed:false}`, or the external e-stop input;
* the burst timer expires (`max_spray_ms`, clamped in firmware);
* the task watchdog fires or the chip resets — the valve pin is driven to its
  inactive level *before* it is configured as an output.

An emergency stop also clears the homed flag: steps are lost in a hard stop, so
the recorded position is no longer trustworthy and absolute moves are refused
until the turret is re-homed.

## Status LED

| Pattern      | Meaning                          |
| ------------ | -------------------------------- |
| Solid        | connected to the server          |
| Blink 1 Hz   | network up, server not connected |
| Blink 0.5 Hz | no network                       |
| Fast blink   | emergency stop latched           |

## Testing without hardware

The server ships a simulator that speaks this exact protocol:

```bash
python server/tools/controller_sim.py --url ws://127.0.0.1:8080/ws/hardware
```

Use it to develop the server and UI, then swap in the real controller — the
server cannot tell the difference apart from the reported firmware version.

## Protocol

`include/protocol_generated.h` is generated from the server's definition:

```bash
python server/tools/gen_protocol_header.py           # regenerate
python server/tools/gen_protocol_header.py --check   # verify it is current
```

Never edit it by hand. The prose specification is `docs/PROTOCOL.md`.

## Networking

Only Wi-Fi station mode is implemented. For an Ethernet board (WT32-ETH01,
ESP32-POE, …), replace the netif setup in `src/wifi_manager.c`; nothing above
that layer changes.
