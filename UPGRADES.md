# HeyBuddy Upgrades

Tracked task list for the rolling upgrade loop. One task per cycle. Source
of truth for architecture, file layout, and conventions remains
[`CLAUDE.md`](CLAUDE.md). When a task here changes architecture, the same
commit updates `CLAUDE.md`.

## GUI
- [x] ui/theme.py: color, radius, spacing tokens
- [x] Frameless panel: rounded corners, shadow, draggable, remembers position
- [x] DWM blur-behind translucent background
- [x] Status row bound to state (dot + label + pulse)
- [x] Mic level waveform meter while listening
- [x] Streaming reply area fills token-by-token
- [x] Hover states + pointer cursor on all buttons
- [x] Panel non-activating (WS_EX_NOACTIVATE + WS_EX_TOOLWINDOW)

## Cursor overlay
- [x] Overlay click-through + non-activating flags verified
- [x] Glowing blue dot + halo + comet trail
- [x] Idle breathing animation
- [x] Bezier-arc flight from POINT tags, ease-in-out
- [x] Per-monitor DPI coordinate mapping
- [x] Arrival ripple + label caption fade
- [x] Transient mode fade in/out

## Etc
- [x] Tray menu: Open, Toggle Companion, Settings, Quit
- [x] Tray icon changes per state
- [ ] Settings: model, voice_id, hotkey capture, mic picker, test mic
  - [x] model (done in earlier work)
  - [x] test mic (done in earlier work)
  - [x] mic picker (cycle 18)
  - [x] hotkey capture (cycle 19)
  - [ ] voice_id
- [ ] Settings persist + apply live
- [ ] Start/stop chimes + TTS-playing indicator
