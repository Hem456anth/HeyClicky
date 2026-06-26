# CLAUDE.md — HeyBuddy source of truth

This file is the canonical reference for HeyBuddy's architecture, file layout,
conventions, and Worker contract. Every future turn should read this first.
When you change something documented here, update this file per the
[Self-Update](#self-update-instructions) section at the bottom.

HeyBuddy is the Windows port of
[farzaa/clicky](https://github.com/farzaa/clicky). The Cloudflare Worker proxy
is reused unchanged. The shipped client holds **no API keys**.

---

## Overview

HeyBuddy is a Windows system-tray companion app with no taskbar button. The
tray icon opens a custom floating panel. Audio capture is push-to-talk via a
global `Ctrl+Alt` low-level keyboard hook. Recorded PCM goes to AssemblyAI
realtime v3 over websocket. The transcript plus a screenshot is sent to Claude
via the Worker's SSE `/chat` route. Claude responds with text and may embed
`[POINT:x,y:label:screenN]` tags; an always-on-top, click-through overlay
animates a blue cursor along a bezier arc to those points. The response text
is voiced by ElevenLabs via the Worker's `/tts` route.

## Phase boundary (current)

We are at the end of **Phase 3 — project complete**. The full pipeline is
wired end-to-end and all polish items have landed:

- pystray tray icon + PyQt6 floating panel (Phase 1)
- `CompanionManager` 4-state machine: `idle → listening → processing → responding → idle`
- Low-level Win32 `WH_KEYBOARD_LL` push-to-talk hook for Ctrl+Alt (Phase 1)
- `sounddevice` PCM16 16 kHz mono recorder, with an `on_chunk` push hook so
  PCM streams into the STT websocket as it's captured
- `mss` multi-monitor screen capture with `PIL.ImageGrab` fallback
- Click-through, non-activating blue cursor overlay; flies along a
  quadratic bezier arc with DPI-correct multi-monitor coordinate mapping
- **AssemblyAI realtime v3** websocket streaming transcription
  (`src/api/assemblyai_streaming.py`), token fetched from `/transcribe-token`
- **Claude `/chat` SSE** streaming with proper Anthropic event parsing,
  POINT-protocol system prompt, screenshot attachment
- **ElevenLabs `/tts`** synthesis + sounddevice playback
- POINT marker queue: multi-point responses fly the dot sequentially
- **Hotkey rebinding** via preset chords (`utils.win32.HOTKEY_PRESETS`)
- **Diagnostics**: mic test (RMS sanity) + Worker ping (`utils.permissions`)
- **Transient cursor mode**: panel auto-hides when listening begins
- **Auto-clearing error banner** at the top of the panel
- **Windows autostart** via HKCU Run key (`utils.win32.enable_autostart`)
- **PyInstaller one-file build** via `heybuddy.spec` + `build.bat`

Runtime prerequisite: `config/settings.json -> worker_url` must point at the
deployed Cloudflare Worker. Without it, the network calls fail at first turn.

Future work (not in scope here, candidates for Phase 4):
- Auto-reconnect of the AssemblyAI WS on mid-stream token expiry
  (current behavior: surfaces error, user re-presses hotkey)
- Multi-monitor screenshots so Claude can point at any screen
- Per-delta POINT parsing so the cursor starts flying mid-stream
- A custom-chord capture UI for hotkeys beyond the preset list

---

## Architecture

### Threading model

PyQt6 owns the main thread and runs the event loop.

| Thread | Owner | Purpose |
| --- | --- | --- |
| `main` | Qt | UI, signals/slots, all widget mutation |
| `Win32HookPump` | `utils.win32.LowLevelKeyboardHook` | `SetWindowsHookExW(WH_KEYBOARD_LL, …)` + `GetMessageW` pump; emits press/release callbacks |
| `SoundDeviceCallback` | `sounddevice.InputStream` | sounddevice's own callback thread; appends PCM frames AND pushes them to the STT `on_chunk` listener |
| `PystrayThread` | `pystray.Icon.run` | Tray icon event loop; menu callbacks marshal back to Qt via `QMetaObject.invokeMethod` |
| `StartCapture` | `core.companion_manager` | Short-lived: opens the AssemblyAI WS + starts the recorder on hotkey press (off the hook thread, which has a hard time limit) |
| `TurnPipeline` | `core.companion_manager` | One thread per turn: STT finalize + Claude SSE + TTS network work |
| `AssemblyAIReader` | `api.assemblyai_streaming` | `WebSocketApp.run_forever`; reads JSON Turn / Termination / Error frames, fires partial/final callbacks |
| `AudioPlayer` | `core.audio_player` | Decodes ElevenLabs MP3 with pydub and plays via sounddevice |

Rules:

- **Never touch a `QWidget` off the main thread.** Use `pyqtSignal` from the
  worker → connect on the main thread. The hotkey hook fires its callbacks
  from its own pump thread; `CompanionManager` re-emits as signals.
- **Never call `time.sleep` on the Qt thread.** Use `QTimer.singleShot` or a
  worker thread.
- **All `ctypes` / Win32 lives in [`src/utils/win32.py`](src/utils/win32.py).**
  No raw `ctypes` import anywhere else in the tree.

### State machine

`CompanionManager` is the single source of truth. States and transitions
(Phase 1 — Phase 2 adds intermediate steps inside `processing`):

```
IDLE ──hotkey press──▶ LISTENING                  (opens AssemblyAI WS + recorder)
LISTENING ──hotkey release──▶ PROCESSING          (stop recorder + finalize STT)
LISTENING ──empty buffer / empty transcript──▶ IDLE
PROCESSING ──Claude SSE finished──▶ RESPONDING    (parsed POINTs queued into overlay)
RESPONDING ──TTS playback ended──▶ IDLE
ANY ──exception──▶ ERROR ──auto──▶ IDLE
```

`state_changed = pyqtSignal(CompanionState)` is emitted on every transition.

### Worker contract

The Cloudflare Worker is reused unchanged from upstream. The Worker holds all
secrets; the client never sees them.

| Route | Method | Request body | Response |
| --- | --- | --- | --- |
| `/chat` | POST | Anthropic Messages API JSON, e.g. `{"model","system","messages","max_tokens","stream":true}` | Forwarded Anthropic response; SSE when `stream=true` |
| `/tts` | POST | `{"text": "..."}` — **no `voice_id`**, Worker injects `ELEVENLABS_VOICE_ID` | `audio/mpeg` bytes |
| `/transcribe-token` | POST | empty | JSON `{...}` containing the short-lived AssemblyAI v3 token |

Notes:

- The Worker's top-level guard rejects every non-POST request, so even
  `/transcribe-token` must be a POST from the client.
- The `/tts` body must not include `voice_id`. The Worker hard-codes the voice
  in env var `ELEVENLABS_VOICE_ID`. Voice changes happen by changing the
  Worker config, not the client.
- `/chat` is a passthrough: whatever you POST is sent to
  `api.anthropic.com/v1/messages` with the Anthropic key injected.

### POINT protocol

Claude is instructed (via the system prompt) to emit pointing tags of the form:

```
[POINT:x,y:label:screenN]
```

- `x`, `y` — pixel coordinates in **that monitor's** logical coordinate space
- `label` — short caption shown above the dot
- `screenN` — monitor index (1-based; matches the index the system prompt
  enumerated for Claude)

Regex (in [`src/utils/constants.py`](src/utils/constants.py)):

```
\[POINT:(\d+),(\d+):([^:\]]+)(?::screen(\d+))?\]
```

`screenN` is optional so partial-tag fallbacks still parse. Coordinates are
mapped through each monitor's DPI scale (`utils.win32.get_dpi_scale_for_monitor`)
before the overlay animates the cursor.

### Cursor overlay

A frameless, top-most, click-through, **non-activating** `QWidget` covering
the virtual screen. Win32 extended styles:

- `WS_EX_TRANSPARENT` — clicks pass through
- `WS_EX_LAYERED` — required for `WS_EX_TRANSPARENT`
- `WS_EX_NOACTIVATE` — never steals focus from the user's foreground app
- `WS_EX_TOOLWINDOW` — keeps it off Alt+Tab and the taskbar

All four flags are applied via `utils.win32.apply_overlay_window_styles`.

---

## File layout

```
heybuddy/
├── CLAUDE.md                              ← you are here
├── README.md                              ← user-facing quick start, credits upstream
├── requirements-desktop.txt           ← Python deps (renamed from requirements.txt so Cloudflare Workers Builds doesn't auto-detect this as a Python project)
├── wrangler.toml                      ← top-level Workers config for Cloudflare auto-detect
├── package.json                       ← top-level Workers manifest (wrangler devDep)
├── heybuddy.spec                         ← PyInstaller one-file build recipe
├── build.bat                              ← convenience wrapper around PyInstaller
├── config/
│   └── settings.json                      ← user-editable runtime config
├── assets/                                ← tray icon, etc. (optional)
├── src/
│   ├── __init__.py
│   ├── main.py                            ← PyQt bootstrap + pystray + hotkey wiring
│   │
│   ├── ui/
│   │   ├── theme.py                       ← design tokens (color, radius, spacing, typography, shadow) — single source of truth, no hardcoded values elsewhere
│   │   ├── main_window.py                 ← floating panel: transparent outer + _PanelChrome inner with drop shadow + _DraggableTitleBar + position persistence to UIConfig
│   │   ├── overlay_window.py              ← click-through blue cursor overlay
│   │   └── settings_panel.py              ← settings dialog (Phase 3 expands this)
│   │
│   ├── core/
│   │   ├── companion_manager.py           ← central state machine
│   │   ├── hotkey_monitor.py              ← wraps utils.win32.LowLevelKeyboardHook
│   │   ├── audio_recorder.py              ← sounddevice PCM16 16 kHz mono
│   │   └── screen_capture.py              ← mss with PIL.ImageGrab fallback
│   │
│   ├── api/
│   │   ├── cloudflare_proxy.py            ← HTTP transport + SSE event parser for /chat, /tts, /transcribe-token
│   │   ├── claude_client.py               ← payload assembly, SSE text-delta extraction, POINT tag parsing
│   │   ├── assemblyai_streaming.py        ← realtime v3 WS session (open/feed/stop)
│   │   ├── transcription_provider.py      ← batch interface (kept for Phase 3 fallbacks)
│   │   └── elevenlabs_client.py           ← /tts wrapper
│   │
│   ├── models/
│   │   ├── message.py                     ← Message, PointMarker (incl. screenN)
│   │   └── config.py                      ← AppConfig dataclasses, load/save
│   │
│   ├── tools/
│   │   └── smoke_test.py                  ← Phase 1 manual test: hotkey + recording, no network
│   │
│   └── utils/
│       ├── constants.py                   ← APP_NAME, paths, POINT regex, system prompt
│       ├── logger.py                      ← rotating file + console
│       ├── permissions.py                 ← mic + Worker diagnostics for Settings
│       └── win32.py                       ← ALL ctypes/Win32 + winreg autostart + DWM blur-behind/Mica/Acrylic
```

---

## Conventions

These mirror upstream `AGENTS.md` adapted for Python.

### Naming and clarity

- **Clarity over concision.** A reader with zero context should understand the
  variable, method, or class name. Prefer `pending_user_transcript` to `txt`.
- No single-character names except loop indices in trivial 2-line loops.
- Preserve original variable names when forwarding them through function
  signatures — don't abbreviate at call boundaries.
- Long descriptive names are preferred even if they make a line wrap.

### Code style

- **Type hints on everything.** `from __future__ import annotations` at the
  top of every module so forward refs and `X | None` work on 3.10+.
- **Comments explain "why", not "what".** Code says what; comments say why
  (hidden constraints, Win32 quirks, why a callback runs on a non-Qt thread,
  why we don't use the obvious library).
- **No `print`.** Use `utils.logger.get_logger(__name__)`.
- **No `time.sleep` on the Qt thread.**
- **No raw `ctypes` outside `utils/win32.py`.**

### UI

- Frameless windows use a single visible `QWidget` so the dark theme is
  consistent. No native chrome.
- The overlay window is never activated and never accepts input.
- All buttons set a pointer cursor on hover.
- **All colors, corner radii, and spacing values come from
  [`src/ui/theme.py`](src/ui/theme.py)**. No hardcoded `#hexcolor`,
  `border-radius: 8px`, or `padding: 12px` anywhere else in the UI. If
  you need a token that doesn't exist there, add it to theme.py first.

### Do NOT

- Add features, refactors, or "improvements" beyond what was requested.
- Add docstrings or comments to code you didn't change.
- Block the Qt main thread.
- Import `ctypes`, `win32api`, or `pywin32` outside `src/utils/win32.py`.
- Ship API keys in the client; everything goes through the Worker.
- Rename folders/modules listed above without updating this file.

---

## Running

### Phase 1 — smoke test (no network)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-desktop.txt

# Verify hotkey + recording end-to-end before anything else:
python -m src.tools.smoke_test
# Hold Ctrl+Alt, speak, release. A .wav drops into ./recordings/ and stats print.

# Then launch the tray app:
python -m src.main
```

### Phase 2 — what works in the app

- Tray icon → toggle floating panel.
- Holding Ctrl+Alt anywhere opens an AssemblyAI realtime WS, records PCM,
  and streams it live to the WS while the key is held. Partial transcripts
  appear in the input placeholder as the user speaks.
- Releasing the chord finalizes the transcript, captures the cursor's
  monitor as a PNG, and streams Claude's reply via `/chat` SSE.
- The reply text appears in the panel; any `[POINT:x,y:label:screenN]`
  tags are parsed out, fired into the cursor overlay one by one, and the
  blue dot flies along a bezier arc to each one.
- TTS playback runs in parallel; state returns to IDLE when audio finishes.
- The captured wav is also persisted to `./recordings/<ts>.wav` for debugging.

### Phase 3 — not yet

- Settings/permissions diagnostic panel (mic / DPI sanity check, Worker
  reachability ping)
- Transient cursor mode (panel hidden; just fly the cursor + speak)
- Error/reconnect polish (auto-retry STT WS, friendlier panel errors)
- PyInstaller packaging + autostart

---

## Configuration

`config/settings.json` (also written by the in-app Settings dialog):

| key | default | meaning |
| --- | --- | --- |
| `worker_url` | `https://your-worker.workers.dev` | Cloudflare Worker base URL |
| `hotkey` | `ctrl+alt` | Push-to-talk chord. Phase 1 hard-codes Ctrl+Alt in the LL hook; rebinding lands in Phase 3 |
| `model` | `claude-sonnet-4-6` | Anthropic model id; alternative is `claude-opus-4-7` |
| `tts_enabled` | `true` | Speak responses (Phase 2) |
| `screen_capture_enabled` | `true` | Attach a screenshot to every turn (Phase 2) |

Notably **absent**: `voice_id`. The Worker holds the voice id as
`ELEVENLABS_VOICE_ID`; changing the voice means redeploying the Worker.

---

## Self-Update Instructions

When making changes that affect this file's accuracy, update it in the same
turn as the code change:

1. **New file added to `src/`** → add a row to the [file layout](#file-layout)
   tree with a one-line purpose.
2. **File removed** → delete its row from the tree.
3. **State machine change** → update the [state machine](#state-machine) ASCII
   diagram.
4. **New Worker route or signature change** → update the
   [Worker contract](#worker-contract) table.
5. **Threading model change (new long-lived thread, new owner)** → update the
   [threading model](#threading-model) table.
6. **New convention or constraint adopted** → add to [conventions](#conventions).
7. **Phase boundary crossed** → update the
   [Phase boundary](#phase-boundary-current) section and move "what works"
   items from the future bullet list into the present.

Do **not** update for typo fixes, in-function refactors, or anything that
doesn't change documented behavior or layout.
