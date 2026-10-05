# 🔭 WiFi Otoscope Web Viewer

> 🌐 A Python (standard library only) tool that mirrors the video stream of a
> Beken BK7231U based WiFi ear-cleanup camera — the device family behind the
> `libWifiCamera.so` protocol — straight into your browser.

| | |
| --- | --- |
| 🖥️ **Web UI** | fullscreen live view at `http://127.0.0.1:45100/` |
| 🎯 **Orientation** | jitter-free accelerometer roll, pushed via SSE |
| 🎥 **Recording** | server-side `.mjpeg`, no browser tab required |
| 🩺 **Diagnostics** | heartbeat listener, raw camera config dump |
| 📜 **License** | GPL-2.0 (inherited from the upstream project) |

**Tested device:** `YPC BK7231U-XRH-FBPRO`, firmware `HKV41B`
(camera IP `192.168.1.1`, sensor HI708 → 480×480 JPEG).

🛒 **Developed against this unit:** [Hopefox Ear Wax Remover — 1080P HD WiFi
Ear Cleaner with Camera and 6 LEDs](https://www.amazon.de/dp/B0CVX5CJPW)
(Amazon ASIN `B0CVX5CJPW`). The listing advertises 1080p, the shipped hardware
streams 480×480 — see [⚠️ Known quirks](#-known-device-quirks).

This repository is a fork of
[SeanPesce/Suear-Web-Viewer](https://github.com/SeanPesce/Suear-Web-Viewer)
with a browser UI, low-latency orientation, on-demand recording and a number of
robustness fixes on top of the original stream mirror.

---

## 📑 Contents

| | | |
| --- | --- | --- |
| ✨ [Features](#-features) | 🚀 [Quick start](#-quick-start) | 🧬 [How it works](#-how-it-works) |
| 🌐 [HTTP API](#-http-api) | 🗂️ [Project layout](#-project-layout) | 🎞️ [Recorded files](#-recorded-files) |
| 🔋 [Charging](#-charging) | ⚠️ [Known quirks](#-known-device-quirks) | 🔬 [References](#-references) |
| 🤖 [Development notes](#-development-notes) | 📜 [License](#-license) | |

---

## ✨ Features

### 🖥️ Interface

| Icon | Feature |
| :---: | --- |
| 🔄 | **Auto-rotation** that follows the probe's accelerometer |
| 🪞 | **Mirror** and direction flip, plus one-shot calibration |
| 💡 | **Ring light** on/off (`/led`, unsupported models report `supported: false`) |
| 📸 | **Photo capture** — downloaded *and* stored server-side in `photos/` |
| 🎥 | **Recording button** → `recordings/*.mjpeg` |
| ☀️ 🔲 | **Toolbar** — brightness, contrast, thirds grid, frame freeze, info panel |
| 🌗 | Dark, keyboard-friendly layout with fullscreen mode |

### 🧠 Streaming & reliability

| Icon | Feature |
| :---: | --- |
| 🎯 | **Jitter-free rotation.** The 9-bit accelerometer quantises to ~0.44° per step and `atan2` amplifies the noise, which used to make the picture rock back and forth like a ball in a gutter. A 0.7° dead band plus an exponential moving average (τ = 70 ms, 60 fps) drives the transform instead of a CSS transition. |
| ⚡ | **Latency.** Orientation is pushed with Server-Sent Events the moment a UDP chunk arrives (`/events`), with `/position` polling as a fallback. |
| 🧵 | **One reader thread** owns the UDP sockets and fans completed JPEGs out to every consumer, so an open `recordings/*.mjpeg` keeps filling even if the tab is closed. |
| 🔁 | **Auto-reconnect** on stall, keep-alive re-send of `OpenVideo`, discard of unfinished frames instead of freezing, per-connection stream handlers sharing one camera session. |

### 🩺 Diagnostics

| Icon | Feature |
| :---: | --- |
| 💓 | Listens on **UDP 10007** for the camera's status push (heartbeat) and logs any other payload as an `EVENT` — useful to find out whether the physical button emits anything over the network. |
| 🧾 | `/cameracfg` returns a raw hex dump of read-only command `0x000d`. |
| 📊 | `/stats` reports frames, chunks, discards, errors, heartbeat age and recording state. |

---

## 🚀 Quick start

| Step | Action |
| :---: | --- |
| 1️⃣ | Power the camera on (**unplugged** from USB — see [🔋 Charging](#-charging)) |
| 2️⃣ | Join the camera's WiFi network |
| 3️⃣ | Run `./start-suear.sh` |
| 4️⃣ | Open <http://127.0.0.1:45100/> |

```bash
./start-suear.sh
# equivalent to:
PYTHONUNBUFFERED=1 SUEAR_DEBUG=1 python3 suear_mirror.py --no-ssl
```

The server starts even if the camera is not reachable yet; status is refreshed
as soon as the device comes online.

### 📺 Other MJPEG clients

| Icon | Client | Command |
| :---: | --- | --- |
| 🌐 | Browser | `http://127.0.0.1:45100/` |
| 📺 | VLC | `vlc http://127.0.0.1:45100/stream` |
| 🎮 | ffplay | `ffplay http://127.0.0.1:45100/stream` |
| 🎬 | ffmpeg | `ffmpeg -i http://127.0.0.1:45100/stream -c:v libx264 out.mp4` |

---

## 🧬 How it works

```
  camera ──UDP 10005 (commands) ───────────────▶ open video / LED / config
  camera ──UDP 10006 (open-video ack) ─────────▶ client
  camera ──UDP 10007 (heartbeat/status) ───────▶ listener → /stats, /events
  camera ──UDP 22785/22789 (JPEG frames) ──────▶ reader thread
                                                    ├─▶ /stream   (browser)
                                                    ├─▶ /events   (SSE roll)
                                                    ├─▶ Recorder ─▶ recordings/*.mjpeg
                                                    └─▶ snapshot / photo → photos/
```

---

## 🌐 HTTP API

| Icon | Path | Description |
| :---: | --- | --- |
| 🖥️ | `/` | Web UI |
| 🎥 | `/stream` | Multipart MJPEG stream |
| 📡 | `/events` | Server-Sent Events with roll/slope per UDP chunk |
| 🧭 | `/position` | Last orientation sample (JSON) |
| 📊 | `/stats` | Frames, chunks, discards, errors, heartbeat, recording state |
| 📱 | `/device` | Vendor/model/firmware/SSID/battery/charging |
| 🖼️ | `/snapshot` | Latest JPEG |
| 📸 | `/photo?source=ui` | Save the latest JPEG server-side into `photos/` |
| ⏺️ | `/record?on=1` / `?on=0` | Start/stop server-side recording into `recordings/` |
| 💡 | `/led?on=1` / `?on=0` | Ring light (models without an LED report `supported: false`) |
| 🧾 | `/cameracfg` | Raw hex dump of command `0x000d` (read-only camera config) |
| 🏷️ | `/battery` `/model` `/vendor` `/version` `/ssid` `/serial` | Scalar shortcuts |

---

## 🗂️ Project layout

| File | Purpose |
| --- | --- |
| `suear_mirror.py` | HTTP + UDP server, stream hub, recorder, API handlers |
| `soulear_ui.html` | Single-file browser UI (HTML/CSS/JS, no build step) |
| `start-suear.sh` | Launcher that enables unbuffered logging |
| `suear_struct.py` | Frame/command struct layouts |
| `suear_util.py` | Protocol helpers (roll, bit fields, JPEG slicing) |
| `photos/`, `recordings/` | Local output (git-ignored) |

---

## 🎞️ Recorded files

`recordings/*.mjpeg` is a plain concatenation of JPEG frames — playable
directly with VLC, mpv or ffplay, and easily converted:

```bash
ffmpeg -i recordings/20261005_211755.mjpeg -c:v libx264 -crf 20 out.mp4
```

📦 Rough size: ~1.4 GB per hour at 480×480 / ~15 fps.

---

## 🔋 Charging

> ⚠️ **The stream stops while the camera is charging.**

The camera switches off its WiFi access point as soon as USB power is
detected, so record or view on battery, then dock it. The device is small
(≈170 mAh battery) and powers off quickly when the charge runs out.

---

## ⚠️ Known device quirks

| Icon | Quirk |
| :---: | --- |
| 🖼️ | The frame header announces `640×480`, but the actual JPEG is `480×480` (HI708 sensor). `/stats` reports both `width/height` and `jpeg_width/jpeg_height`. |
| 💥 | Command `0x0008` with a non-empty payload **powers the camera off** — the mirror only ever sends an empty payload. |
| 🧭 | Roll is gravity-referenced (`atan2(y, z)`), with a fixed 180° mount offset for `FBPRO`/`R1` products. |
| 📉 | There is no higher resolution than 480×480 available on this hardware. |
| 🛒 | The **Amazon listing claims 1080p** ([ASIN B0CVX5CJPW](https://www.amazon.de/dp/B0CVX5CJPW), “up to 30 fps”), but the unit in hand delivers **480×480** JPEGs at ~15 fps: `/stats` reports `jpeg_width/jpeg_height`, saved photos and recordings measure 480×480, and the frame header’s `640×480` is a fixed value from a generic firmware — it does not describe the sensor. Marketing/spec-sheet mismatch typical for white-label units on the BK7231U platform. |

---

## 🔬 References

Reverse engineering of this device family has been done by several projects —
they were used to cross-check commands, ports and frame layout:

| Icon | Project | Contribution |
| :---: | --- | --- |
| 🏞️ | [SeanPesce/Suear-Web-Viewer](https://github.com/SeanPesce/Suear-Web-Viewer) *(upstream)* | Original MJPEG mirror, stream ports, licence and README |
| 📖 | [jduanen/EarCam](https://github.com/jduanen/EarCam) | Command table (`0x0001` devinfo, `0x0004` open video, `0x0009` heartbeat, `0x000A` LED, `0x000D/0x000E` camera config) and the warning about `0x0008` |
| 🧩 | [pedrodinisf/otoscope-viewer](https://github.com/pedrodinisf/otoscope-viewer) | AiSee protocol notes, frame assembler, recorder |
| 🧮 | [rbeilvert/otoscope](https://github.com/rbeilvert/otoscope) (`I4seasonProtocol.kt`) | Header layout, `OpenVideo` payload, `parseDevInfo`, accelerometer bit layout, 180° mount offset |
| 📱 | Vendor mobile app (`com.i4season.bkCamera`) | Its `libWifiCamera.so` implements the protocol |

---

## 🤖 Development notes

> 🧠 This project was written with **AI assistance**.

| Icon | Who / what | Role |
| :---: | --- | --- |
| 🤖 | [**opencode**](https://opencode.ai) — model `opencode/mimo-v2.6-flash-free` | Reverse-engineering notes, implementation, debugging, documentation, README structure |
| 🧑‍💻 | The repository author | Hardware testing, protocol verification, decisions and final review |
| 🔍 | Anyone reading the source | Plain Python standard library + hand-written single-file HTML — reviewable line by line |

AI-generated code and notes should be treated like any other patch: verify it
against your own device before relying on it (see [⚠️ Known quirks](#-known-device-quirks)).

---

## 📜 License

[GPL-2.0](LICENSE) — inherited from the upstream project.
