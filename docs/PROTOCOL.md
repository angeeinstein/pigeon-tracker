# Turret Control Protocol (TCP-WS) — v1

This document is the **single source of truth** for the link between the Linux
server and the turret controller (ESP32).

Both implementations derive from it:

| Side     | Implementation                                              |
| -------- | ----------------------------------------------------------- |
| Server   | `server/app/turret/protocol.py` (Pydantic models, validated) |
| Firmware | `firmware/include/protocol_generated.h` (generated)          |

`firmware/include/protocol_generated.h` is produced by
`python server/tools/gen_protocol_header.py` so that message-type strings,
error codes and the protocol version never diverge. Re-run it after editing
`protocol.py`; CI/`make check` verifies it is up to date.

---

## 1. Transport

* WebSocket, **controller → server** (the ESP32 is the WS *client*). This makes
  reconnect logic trivial and keeps the server free of outbound connections.
* Endpoint: `ws://<server>:<port>/ws/hardware`
* Sub-protocol: none. Payloads are UTF-8 JSON text frames, one JSON object per
  frame. No frame batching, no newline framing.
* Maximum accepted frame size: **16 KiB** (server rejects and closes larger).
* The design deliberately keeps every field flat and typed so that a later
  binary encoding (CBOR/MessagePack, same field names) is a drop-in change.

### Authentication

If a controller token is configured on the server, the controller must supply
it in the `hello` message (`token` field) **or** as the `Authorization: Bearer
<token>` header. Connections that fail authentication receive
`hello_ack {accepted:false, reason:"unauthorized"}` and are closed with code
`4401`.

Tokens are compared in constant time. If no token is configured the server
accepts any controller (documented as LAN-only mode) and logs a warning.

### Connection lifecycle

```
controller                                   server
    |  ws connect                               |
    |------------------------------------------>|
    |  hello {protocol_version, fw, caps}       |
    |------------------------------------------>|
    |            hello_ack {accepted, ...}      |
    |<------------------------------------------|
    |  status (>= 5 Hz, or on change)           |
    |------------------------------------------>|
    |            ping {id, t_ms}   (every 2 s)  |
    |<------------------------------------------|
    |  pong {id, t_ms}                          |
    |------------------------------------------>|
    |            move_absolute {id, ...}        |
    |<------------------------------------------|
    |  ack {id, ok}                             |
    |------------------------------------------>|
```

* The controller **must** send `hello` as its first frame. Any other first
  frame closes the connection (`4400`).
* Only **one** controller connection is active at a time. A new connection with
  the same `controller_id` replaces the old one (the old socket is closed with
  `4409`); the server logs the takeover.
* If the server sees no frame from the controller for `link_timeout_ms`
  (default 6000) it closes the socket. If the controller sees no frame from the
  server for the same window it drops the link, **closes the valve** and stops
  motion.

---

## 2. Envelope

Every message is a JSON object with at least:

| Field  | Type   | Notes                                              |
| ------ | ------ | -------------------------------------------------- |
| `v`    | int    | Protocol version. Currently `1`.                   |
| `type` | string | Message type (see below).                          |
| `id`   | int    | Command id — required on commands, echoed in `ack`. |

Unknown fields are ignored by both sides (forward compatible).
Unknown `type` values are answered with `ack {ok:false, code:"UNSUPPORTED"}`
when an `id` is present, otherwise ignored and logged.

### Version negotiation

The server compares `hello.protocol_version` with its own
`PROTOCOL_VERSION`. On mismatch it replies
`hello_ack {accepted:false, reason:"protocol_version_mismatch"}`, refuses to
send any command, and surfaces a clear error in the UI and health endpoint.
This is a hard failure by design — silently commanding hardware that speaks a
different dialect is not acceptable.

---

## 3. Server → Controller

### `move_absolute`
Move to an absolute mechanical angle. Rejected with `NOT_HOMED` unless homing
completed (or `allow_unhomed_motion` is enabled in controller config).

```json
{ "v":1, "type":"move_absolute", "id":1234,
  "pan_deg":42.31, "tilt_deg":-17.82,
  "max_speed_deg_s":60.0, "accel_deg_s2":180.0 }
```
`max_speed_deg_s` / `accel_deg_s2` are optional; omitted → configured defaults.
Targets are clamped to soft limits; clamping is reported in the ack
(`ok:true, clamped:true`).

### `move_relative`
```json
{ "v":1, "type":"move_relative", "id":1235,
  "pan_delta_deg":-2.5, "tilt_delta_deg":0.0, "max_speed_deg_s":30.0 }
```

### `jog`
Velocity control for joystick / hold-to-move UI. The controller accelerates
toward the requested rate and **automatically decelerates to a stop** if no new
`jog` arrives within `ttl_ms`. This is what makes a dropped packet safe.

```json
{ "v":1, "type":"jog", "id":1236,
  "pan_rate_deg_s":15.0, "tilt_rate_deg_s":-5.0, "ttl_ms":400 }
```
A rate of `0,0` stops immediately (with deceleration ramp).

### `home`
```json
{ "v":1, "type":"home", "id":1237, "axes":"both" }
```
`axes` ∈ `both` | `pan` | `tilt`. The ack is sent when homing **completes**
(or fails); progress is visible through `status.state`.

### `stop`
```json
{ "v":1, "type":"stop", "id":1238, "emergency":true }
```
`emergency:true` → immediate hard stop (no deceleration ramp), valve closed,
output disarmed, `homed` cleared (steps were certainly lost).
`emergency:false` → controlled deceleration to a stop, valve closed.

### `spray`
```json
{ "v":1, "type":"spray", "id":1239, "duration_ms":400 }
```
Rejected with `DISARMED` if output is not armed. `duration_ms` is clamped to
`max_spray_ms`. The controller arms a **hardware-backed one-shot timer**; the
valve closes when it expires regardless of what the rest of the firmware does.

### `spray_stop`
```json
{ "v":1, "type":"spray_stop", "id":1240 }
```

### `arm_output`
```json
{ "v":1, "type":"arm_output", "id":1241, "armed":false }
```
Disarming always closes the valve immediately.

### `set_config` / `get_config`
```json
{ "v":1, "type":"set_config", "id":1242,
  "config": { "pan_gear_ratio": 6.0, "max_speed_deg_s": 90.0 } }
```
Partial update; persisted to NVS. See §5 for the config keys.

### `ping`
```json
{ "v":1, "type":"ping", "id":1243, "t_ms":123456789 }
```

### `reboot`
```json
{ "v":1, "type":"reboot", "id":1244 }
```

---

## 4. Controller → Server

### `hello`
```json
{ "v":1, "type":"hello",
  "controller_id":"turret-1",
  "firmware_version":"0.1.0",
  "protocol_version":1,
  "token":"...",
  "capabilities":["pan","tilt","valve","endstops"],
  "hardware":{"chip":"esp32","mac":"aa:bb:cc:dd:ee:ff"} }
```

### `status`
Sent at `status_interval_ms` (default 100 ms) and immediately on any state
change. This is the authoritative hardware state; the server never assumes.

```json
{ "v":1, "type":"status", "seq":8421, "uptime_ms":934112,
  "state":"IDLE",
  "pan_deg":42.10, "tilt_deg":-17.75,
  "target_pan_deg":42.31, "target_tilt_deg":-17.82,
  "pan_rate_deg_s":0.0, "tilt_rate_deg_s":0.0,
  "moving":true, "homed":true, "armed":false, "valve_open":false,
  "limit_pan_min":false, "limit_pan_max":false,
  "limit_tilt_min":false, "limit_tilt_max":false,
  "estop":false, "error":null }
```

`state` ∈ `BOOT` | `IDLE` | `MOVING` | `HOMING` | `JOGGING` | `FAULT` | `ESTOP`.

### `ack`
```json
{ "v":1, "type":"ack", "id":1234, "ok":true, "clamped":false }
{ "v":1, "type":"ack", "id":1234, "ok":false,
  "code":"NOT_HOMED", "error":"absolute motion requires homing" }
```

### `event`
Asynchronous notifications that are not tied to a command.
```json
{ "v":1, "type":"event", "event":"homing_completed",
  "detail":{"pan_deg":0.0,"tilt_deg":0.0} }
```
Events: `boot`, `homing_started`, `homing_completed`, `homing_failed`,
`limit_hit`, `estop`, `estop_cleared`, `valve_opened`, `valve_closed`,
`watchdog_reset`, `config_saved`, `fault`.

### `pong`
```json
{ "v":1, "type":"pong", "id":1243, "t_ms":123456789 }
```

### `log`
```json
{ "v":1, "type":"log", "level":"warn", "msg":"tilt endstop bounced" }
```
Forwarded into the server's structured log with a `controller` marker.

---

## 5. Error codes

| Code            | Meaning                                                  |
| --------------- | -------------------------------------------------------- |
| `NOT_HOMED`     | Absolute motion attempted before homing                  |
| `LIMIT`         | Target outside soft limits and clamping disabled         |
| `DISARMED`      | Output command while output disarmed                     |
| `ESTOP`         | Emergency stop latched; clear it first                   |
| `INVALID_PARAM` | Malformed / out-of-range field                           |
| `BUSY`          | Conflicting operation in progress (e.g. homing)          |
| `TIMEOUT`       | Operation exceeded its allowed time (e.g. homing search) |
| `UNSUPPORTED`   | Unknown message type or unsupported capability           |
| `FAULT`         | Controller is in a fault state and refuses commands      |

---

## 6. Controller configuration keys

Settable via `set_config`, persisted in NVS, reported by `get_config`.

| Key                     | Unit   | Default | Notes                              |
| ----------------------- | ------ | ------- | ---------------------------------- |
| `steps_per_rev`         | steps  | 200     | Motor full steps per revolution    |
| `pan_microsteps`        | —      | 16      |                                    |
| `tilt_microsteps`       | —      | 16      |                                    |
| `pan_gear_ratio`        | —      | 1.0     | Output revs per motor rev, inverse |
| `tilt_gear_ratio`       | —      | 1.0     |                                    |
| `pan_invert`            | bool   | false   |                                    |
| `tilt_invert`           | bool   | false   |                                    |
| `pan_min_deg`           | deg    | -90     | Soft limit                         |
| `pan_max_deg`           | deg    | 90      |                                    |
| `tilt_min_deg`          | deg    | -45     |                                    |
| `tilt_max_deg`          | deg    | 45      |                                    |
| `max_speed_deg_s`       | deg/s  | 60      |                                    |
| `accel_deg_s2`          | deg/s² | 180     |                                    |
| `homing_speed_deg_s`    | deg/s  | 15      |                                    |
| `homing_backoff_deg`    | deg    | 3       |                                    |
| `pan_home_dir`          | ±1     | -1      |                                    |
| `tilt_home_dir`         | ±1     | -1      |                                    |
| `pan_home_offset_deg`   | deg    | 0       | Angle assigned at the pan endstop  |
| `tilt_home_offset_deg`  | deg    | 0       |                                    |
| `endstop_active_low`    | bool   | true    | Endstop polarity                   |
| `max_spray_ms`          | ms     | 2000    | Hard clamp on a single spray       |
| `link_timeout_ms`       | ms     | 6000    | Failsafe on link loss              |
| `status_interval_ms`    | ms     | 100     |                                    |
| `allow_unhomed_motion`  | bool   | false   |                                    |

---

## 7. Failsafe summary (normative)

The valve **must** close and motion **must** stop when any of these occur:

1. WebSocket link lost or `link_timeout_ms` elapsed without a server frame.
2. Task watchdog expiry / firmware crash / reboot (GPIO defaults to inactive
   and the valve driver is initialised closed before anything else).
3. `stop`, `arm_output {armed:false}`, or emergency stop input asserted.
4. Invalid or out-of-range command.
5. Single-spray hard timer expiry (`max_spray_ms`).

The server enforces an *additional*, independent budget (max single duration,
cumulative duty over a window, cooldown, retry limit) — see
`server/app/targeting/state_machine.py`. Neither layer relies on the other.
