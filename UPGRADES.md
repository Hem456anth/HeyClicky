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
- [ ] Glowing blue dot + halo + comet trail
- [ ] Idle breathing animation
- [ ] Bezier-arc flight from POINT tags, ease-in-out
- [ ] Per-monitor DPI coordinate mapping
- [ ] Arrival ripple + label caption fade
- [ ] Transient mode fade in/out

## Etc
- [ ] Tray menu: Open, Toggle Companion, Settings, Quit
- [ ] Tray icon changes per state
- [ ] Settings: model, voice_id, hotkey capture, mic picker, test mic
- [ ] Settings persist + apply live
- [ ] Start/stop chimes + TTS-playing indicator
