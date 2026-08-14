# Architecture

```
                 ┌──────────────────────────────────────────────────────────┐
 RTSP camera(s)  │  server (FastAPI, single process, asyncio + workers)      │
 ──────────────► │                                                          │
                 │  CameraSource ──► LatestFrameBuffer ──┬──► VisionPipeline │
                 │   (thread)          (drop-old)        │      detector     │
                 │                                       │      tracker      │
                 │                                       │        │          │
                 │                                       │        ▼          │
                 │                                       │   TargetingEngine │
                 │                                       │   (state machine) │
                 │                                       │        │          │
                 │                    MJPEG/WS preview ◄─┘        ▼          │
                 │                          │              TurretManager     │
                 └──────────────────────────┼─────────────────────┼──────────┘
                                            │                     │ WebSocket
                                     browser │                     ▼
                                             ▼                  ESP32 (ESP-IDF)
                                        React UI            steppers / endstops
                                                            / valve / watchdog
```

## Principles

**The server thinks, the controller moves.** No step pulses cross the network.
The server sends angles, rates, and intents; the ESP32 owns trajectory
generation, limits, and every safety timer that must survive a server crash.

**Newest frame wins.** `LatestFrameBuffer` holds exactly one frame. A slow
detector never builds a backlog — it simply skips frames. Every consumer
(detector, preview, snapshots) pulls independently at its own configured rate.

**No tight coupling.** Each subsystem is constructed from settings and exposes
a narrow interface. `Runtime` (`app/services/runtime.py`) wires them together
and is the only place that knows about all of them.

**Degrade, don't die.** A missing camera, an absent AI model, or a disconnected
controller each disable *their* feature and surface a status — the rest of the
application keeps running. `/api/health` reports each subsystem separately.

## Process model

The application holds live hardware connections, camera decoders, and model
state in memory, so it runs as **one** process. Gunicorn supervises a
**single** Uvicorn worker (`--workers 1`); it is there for graceful reloads and
robust signal handling, not for horizontal scaling. Adding workers would give
you N competing controller links and N model copies — the systemd unit and
`gunicorn.conf.py` both pin it to one and say why.

CPU-bound work (JPEG encoding, inference) runs in a bounded thread pool, so the
event loop stays responsive; the ML runtimes release the GIL during inference.

## Data flow, in detail

1. **Ingest** — `RtspCameraSource` runs a decode loop in a dedicated thread
   (GStreamer `appsink` with `drop=true max-buffers=1`, falling back to OpenCV
   FFmpeg). Each decoded frame replaces the buffer contents.
   Every enabled source is ingested, but only the configured primary source is
   sent through the vision and targeting pipeline. The browser camera gallery
   uses one live MJPEG stream and low-rate snapshots for auxiliary previews to
   avoid multiplying full-rate JPEG encoding work.
2. **Detection** — `VisionPipeline` ticks at `detector.fps`, grabs the newest
   frame, runs the detector in a worker thread, feeds detections to the
   tracker, and publishes a `VisionResult` (tracks + timing) to subscribers.
   Evidence frames keep the immutable source JPEG and original proposals.
   Per-box review metadata is stored alongside each proposal: accepted bird
   boxes become positive labels, rejected boxes remain auditable, and fully
   rejected frames become negative examples. Manual boxes cover detector
   misses. Dataset export writes YOLO labels plus a manifest and keeps adjacent
   camera episodes in the same train/validation partition to reduce leakage.
3. **Targeting** — `TargetingEngine` consumes `VisionResult`s, applies zone
   rules and the selection policy, maps the chosen aim point to pan/tilt with
   the calibration model, and runs the state machine that decides whether to
   move, verify, spray, or back off.
4. **Command** — `TurretManager` serialises commands, matches acks to command
   ids with futures, tracks link health, and holds the last known hardware
   status. In simulated mode an in-process virtual ESP32 implements that same
   connection interface and protocol; no REST, targeting, calibration or
   safety path bypasses `TurretManager`.
5. **Telemetry** — `TelemetryHub` fans a merged snapshot out to browser
   WebSocket clients at a fixed rate; REST handles configuration.

Simulator calibration uses a separate storage namespace per camera. Its amber
nozzle marker comes from simulator ground-truth geometry rather than the fitted
model, so calibration errors remain visible and physical-turret measurements
stay untouched when switching modes.

## Extension points

| Want to…                        | Do this                                                |
| ------------------------------- | ------------------------------------------------------ |
| Use a different model           | Implement `Detector`, register in `vision/detector.py`  |
| Use a different tracker         | Implement `Tracker` in `vision/tracker.py`              |
| Add a turret-mounted camera     | Add a second entry to `settings.cameras` (named source) |
| Replace 2-D calibration with 3-D | Implement `MappingModel` in `targeting/mapping.py`      |
| Swap JSON for a binary protocol | Change the codec in `turret/protocol.py`; models stay   |

The dual-camera closed loop (overview camera for acquisition, turret camera for
fine tracking) is not implemented, but nothing blocks it: cameras are named and
plural, the pipeline is per-source, and `TargetingEngine` takes its aim point
from a `MappingModel` that can be swapped for a closed-loop corrector.
