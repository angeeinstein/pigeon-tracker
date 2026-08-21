# pigeon-tracker — networked 2-axis ball turret

A camera-guided pan/tilt turret with a Linux control server and ESP-IDF
firmware. Its first job is keeping pigeons off a balcony with a water nozzle;
the architecture is deliberately generic, so with the water function switched
off it is a perfectly ordinary camera ball turret with click-to-aim, presets and
a joystick.

**The server thinks, the controller moves.** No step pulses cross the network:
the server sends angles and intents, and the ESP32 owns trajectory generation,
limits, homing and every safety timer that has to survive a server crash.

```
RTSP camera ─► latest-frame buffer ─► detector ─► tracker ─► targeting state
                     │                                            machine
                     └─► MJPEG / WebSocket preview ─► browser         │
                                                                      ▼
                                          WebSocket ─────────────► ESP32
                                                        steppers · endstops
                                                        · valve · watchdog
```

---

## Status

| Part                                                 | State                                                   |
| ---------------------------------------------------- | ------------------------------------------------------- |
| Server (FastAPI, vision, targeting, API, WebSockets)  | Working; 199 tests pass; running on a real LXC          |
| Web UI (React + TypeScript + Vite + Tailwind)         | Builds; desktop/mobile browser review complete          |
| Installer / systemd unit                              | Verified on Ubuntu 24.04 LXC: install, update, reboot   |
| YOLO detection                                        | Model loads and runs (73 ms/frame at 640 px, 2 vCPU CPU-only) |
| In-process controller simulator                       | Selectable in Settings; shown on the live camera feed   |
| ESP32 firmware (ESP-IDF/PlatformIO)                   | Builds, flashes and connects; mechanics not commissioned |

Verified end to end on a 2-vCPU / 1 GB Ubuntu 24.04 container: fresh install →
frontend build → service up and enabled → controller link with token auth →
homing → arm → manual spray (interval guard refusing the second) → calibration
→ click-to-aim (exact interpolation, inverse mapping agrees) → automatic
engagement through `DETECTED → AIMING → VERIFY_TARGET → SPRAY → VERIFY_RESULT`
→ e-stop (latched, homing invalidated, motion refused) → update preserving all
user data → reboot coming back **disarmed with the valve closed**.

Resource use with the AI stack loaded: ~394 MB RSS, ~890 MB venv, model at
`/var/lib/turret-control/models/yolov8n.pt`. 1 GB of RAM is enough but leaves
little headroom — 2 GB is a more comfortable container size.

---

## Install (Proxmox LXC, Debian/Ubuntu)

```bash
curl -fsSL https://raw.githubusercontent.com/angeeinstein/pigeon-tracker/main/install.sh | sudo bash
```

The same command later updates an existing installation. After the first run,
simply type `update` to open the management menu. It checks the installed and
GitHub versions before offering update, endpoint verification, restart, repair,
logs, an immediate backup, uninstall-with-data-kept, and a separately confirmed
full purge. If another program already owns the generic `update` command, use
`turret-update` instead. Flags remain available for automation:

```bash
sudo bash install.sh --update --yes
sudo bash install.sh --branch dev --no-ai      # skip torch: much smaller
sudo bash install.sh --status
sudo bash install.sh --repair                  # rebuild venv + UI, keep data
turret-update --backup
turret-update --uninstall                      # keep data/config/backups
turret-update --uninstall --purge              # delete all project-owned state
```

What it does: installs apt packages, creates the `turret` system user, clones
the repository to `/opt/turret-control`, builds a virtualenv and the frontend,
writes `/etc/turret-control/turret.env` (generating a controller token),
installs and starts the systemd unit, and verifies `/api/health`.
The purge option removes the service, code, configuration, database, models,
snapshots, backups, launchers and service account. Shared OS packages and the
system-wide journal remain untouched so unrelated applications are not broken.

Existing configuration, calibration, zones and events are **never** overwritten
by an update; they are backed up to `/var/backups/turret-control` first, and a
failed update rolls the code back to the previous commit. Before declaring an
update complete, the installer exercises the health, version, authentication
and OpenAPI endpoints, verifies that the running commit matches the checkout,
and fetches the web application plus one of its built assets.

| Path                        | Contents                                  |
| --------------------------- | ----------------------------------------- |
| `/opt/turret-control`       | code and virtualenv (root-owned)          |
| `/etc/turret-control`       | `turret.env` — secrets, 0640 root:turret  |
| `/var/lib/turret-control`   | database, models, snapshots               |
| `/var/backups/turret-control` | pre-update backups (last 5)             |

```bash
journalctl -u turret-control -f
systemctl restart turret-control
```

---

## First run

1. Open `http://<lxc-ip>:8080/`.
2. **Settings → Camera**: click **Discover cameras**, select an ONVIF device,
   enter its camera account, choose a stream profile, and add it. ONVIF obtains
   the manufacturer's RTSP URL; the live video still uses RTSP directly. Camera
   credentials are stored in `/var/lib/turret-control/camera_credentials.json`
   (0600), separately from SQLite, and are never returned by the API. If the
   server and cameras are on different subnets, enter the ONVIF device-service
   URL manually. The built-in simulated balcony remains available by choosing
   the `Simulated` backend or leaving a camera URL empty.
   The **Cameras** page shows one selected live stream plus low-rate previews
   for every other configured source. Only the selected *primary* camera feeds
   detection, tracking, zones, calibration and automatic targeting; auxiliary
   cameras are view-only for now.
3. Before the mechanics exist, choose **Settings → Controller → Simulated
   turret** and save. The server starts a virtual ESP32 using the normal
   controller protocol; home it and the amber box on the real camera feed
   becomes the virtual nozzle. Joystick, presets, limits, calibration,
   automatic targeting, arming and simulated spray all use the production
   paths. Simulator calibration is stored separately from physical calibration.
   Later, select **Physical ESP32**; the flashed controller reconnects itself.
4. **Home** the turret, then **Calibration**: click a spot in the image, jog
   the nozzle onto that spot in reality, save. Repeat — a dozen points spread
   over the balcony is plenty. Tag them by surface (railing / planter / floor)
   where the geometry differs.
5. **Zones**: draw an *active* zone where engagement is allowed and *no-spray*
   zones anywhere water must not go (the neighbour's window, the doorway).
6. **Settings → AI**: use `cpu`, class `bird`, and start with the 960 px input
   default. The capture threshold keeps uncertain proposals for review without
   allowing them into tracking. Existing installations retain their saved
   device and input-size values during updates, so change those two fields
   explicitly if they were previously `auto` / 640. Class selectors use the
   vocabulary reported by the active model and flag names that the model does
   not provide; changing models preserves the selection for explicit review.
7. **Settings → Scene motion** continuously learns the static primary-camera
   background and shows its monochrome foreground mask. Unexplained connected
   motion gets a generously padded crop from the native camera frame and a
   lower-threshold, evidence-only AI rescan. These results are saved for review
   but never enter tracking, targeting or spray. Global exposure changes are
   rejected, while area, density, speed, persistence, crop padding, event
   re-arm and per-event rescan limits remain configurable. Normal full-frame
   detection continues at its configured rate and all normal bird evidence is
   retained.
8. **Detections**: open a frame in the annotation viewer and review each model
   box as a correct bird or a false proposal. Keyboard shortcuts make it quick
   to move through boxes and frames. Draw a missing bird box on manual or
   automatic captures, and mark bird-free frames as negative examples. Fully
   reviewed positives are protected from retention. **Export YOLO dataset**
   downloads the reviewed JPEGs, YOLO labels, episode-aware train/validation
   split, dataset YAML, an audit manifest, and a self-contained Windows training
   launcher; original proposals remain stored. Extract the ZIP and double-click
   `train_windows.bat`. It installs a standard Python runtime through `winget`
   when the machine only has the limited ESP-IDF Python, creates a reusable
   training environment, and opens a GUI with dataset validation, GPU status,
   live epoch/batch metrics, elapsed time, estimated remaining time, GPU-memory
   use, settings guidance, cancellation and a direct link to the resulting
   `best.pt`. When NVIDIA hardware is present, the launcher installs and
   verifies CUDA-enabled PyTorch and refuses to silently fall back to a
   CPU-only build. The GPU is used only while training is running.
   Upload that checkpoint under **Settings → AI → Install a trained model**;
   give it a versioned name, select it, then save all settings to load it.
   Uploads are authenticated, limited to 512 MiB and installed atomically. An
   active model cannot be overwritten in place, so a failed new model load
   leaves the previous working detector available.
9. Only then: enable water output, arm, and turn on automatic targeting.

A fresh install is disarmed with water output disabled. It stays that way until
you deliberately change it.

---

## Development without hardware

```bash
cd server
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements/dev.txt

# Backend with a synthetic camera and a model-free detector:
TURRET_DATA_DIR=./data \
TURRET_FORCE_SIMULATED_CAMERA=true \
TURRET_FORCE_MOCK_DETECTOR=true \
python -m uvicorn app.main:app --reload --port 8080

# In a second terminal: a controller that speaks the real protocol.
python tools/controller_sim.py --url ws://127.0.0.1:8080/ws/hardware

# In a third: the UI dev server (proxies /api and /ws to the backend).
cd frontend && npm install && npm run dev
```

The Settings-based simulator models acceleration-limited motion, homing, jog
expiry, soft limits, emergency stop, arming, configuration and a timed virtual
valve. The command-line simulator remains useful for network-protocol tests and
failure injection such as `--fail-homing`.

```bash
pytest                                   # 209 tests, ~3 s
ruff check app tools tests && ruff format --check app tools tests
mypy app
python tools/gen_protocol_header.py --check   # firmware header is current
```

---

## How it fits together

| Directory                | Purpose                                                    |
| ------------------------ | ---------------------------------------------------------- |
| `server/app/camera/`     | RTSP ingest, latest-frame buffer, simulated source          |
| `server/app/vision/`     | detector abstraction, YOLO backend, ByteTrack, overlays     |
| `server/app/services/detection_capture.py` | detection evidence, review metadata, retention |
| `server/app/targeting/`  | calibration mapping, zones, target selection, state machine |
| `server/app/turret/`     | protocol models, controller link                            |
| `server/app/api/`        | REST, browser WebSocket, controller WebSocket               |
| `server/app/services/`   | settings store, event log, telemetry, runtime wiring        |
| `server/frontend/`       | React + TypeScript web UI                                   |
| `server/tools/`          | controller simulator, protocol header generator             |
| `firmware/`              | ESP-IDF firmware (PlatformIO)                               |
| `docs/`                  | protocol, architecture, hardware notes                      |

Further reading: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md),
[docs/PROTOCOL.md](docs/PROTOCOL.md), [docs/HARDWARE.md](docs/HARDWARE.md),
[firmware/README.md](firmware/README.md).

### Design decisions worth knowing

**Newest frame wins.** `LatestFrameBuffer` holds exactly one published frame; a
slow detector skips frames instead of building a backlog. The normal image is
camera-downscaled, while the same slot may retain its native decoded image for
short-lived motion crop rescans. No native video queue is accumulated.

**One process, one worker.** The server owns live hardware state, so gunicorn
runs a single uvicorn worker. Two workers would mean two controller links, two
copies of the model and two state machines fighting over one turret.

**Calibration by measurement, not by trigonometry.** The camera and the turret
are not co-located, and a balcony is not one plane. Rather than model the
geometry, the system interpolates between measured pixel↔angle pairs, per
surface, and tells you when a click falls outside the calibrated region instead
of inventing an answer.

**Two independent safety layers.** The firmware clamps every burst with a
hardware one-shot timer, closes the valve on link loss, watchdog expiry, e-stop
or reset, and refuses absolute motion until homed. The server separately
enforces cooldowns, retry limits and a cumulative duty budget. Neither trusts
the other. See [docs/PROTOCOL.md §7](docs/PROTOCOL.md).

**One protocol definition.** `server/app/turret/protocol.py` is the source of
truth; `firmware/include/protocol_generated.h` is generated from it and a test
fails if it drifts.

---

## Security

Designed for a trusted LAN, but not carelessly:

* No secrets in git. Credentials live in `/etc/turret-control/turret.env`
  (0640) and `firmware/include/secrets.h` (git-ignored).
* Optional web login (`TURRET_AUTH_ENABLED`), signed session cookie.
* Optional pre-shared controller token, compared in constant time; a controller
  with the wrong protocol version is refused rather than commanded blindly.
* Every REST body and every WebSocket frame is schema-validated; frames are
  size-capped; snapshot paths are confined to their directory.
* Do not expose this to the internet. If you need remote access, use a VPN.

---

## Licence

No licence is granted. All rights reserved by the repository owner.
