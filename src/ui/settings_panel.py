"""Settings dialog: Worker URL, model, hotkey, modes, autostart, diagnostics."""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from ..models.config import AppConfig
from ..utils.logger import get_logger
from ..utils.permissions import check_microphone, ping_worker
from ..utils.win32 import (
    HOTKEY_PRESETS,
    autostart_command_for_current_process,
    chord_display_name,
    disable_autostart,
    enable_autostart,
    format_vk_chord,
    is_autostart_enabled,
)
from . import theme

log = get_logger(__name__)


def _enumerate_input_devices() -> list[tuple[int, str]]:
    """Return [(device_index, display_name), ...] for usable input devices.

    Wrapped in try/except because sounddevice raises if no audio host is
    present (rare, but it happens on stripped-down Windows installs and
    in CI). A failure returns an empty list; the Settings panel then
    shows only "System default", which is still functional.
    """
    try:
        import sounddevice as sd
        devices = sd.query_devices()
    except Exception:
        log.exception("sounddevice.query_devices failed; mic picker will be empty")
        return []
    inputs: list[tuple[int, str]] = []
    for index, device in enumerate(devices):
        # Only show devices that can actually record.
        if int(device.get("max_input_channels", 0)) <= 0:
            continue
        name = device.get("name") or f"device {index}"
        inputs.append((index, name))
    return inputs


class _HotkeyCaptureDialog(QDialog):
    """Modal that records a chord by listening to its own key events.

    Open the dialog, hold whatever chord you want as a push-to-talk key
    combo, then release ALL keys to commit. Esc cancels without saving.

    Why Qt key events instead of a temporary LL hook: when this dialog
    is the foreground window, Qt's keyPressEvent / keyReleaseEvent
    receive every key — including modifier-only chords (Ctrl, Alt, Win)
    — and `event.nativeVirtualKey()` returns the exact Win32 VK code
    we need to round-trip through `format_vk_chord`. An LL hook would
    also capture them but would conflict with the live push-to-talk
    hook for the duration of the capture and adds threading complexity
    for negligible UX gain (the user is staring at a modal anyway).

    Auto-repeat events are filtered (a held key fires keyPressEvent
    many times per second on Windows; without the filter we'd never
    notice releases). The dialog tracks the highest cardinality set
    seen during the session — that's the "peak" chord — so a fumbled
    chord like "Ctrl, Ctrl+Alt, Ctrl" still records the larger set.
    """

    captured = pyqtSignal(tuple)  # tuple of VK ints, sorted ascending

    def __init__(self, parent: QDialog | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Capture push-to-talk chord")
        self.setMinimumWidth(340)
        layout = QVBoxLayout(self)

        instructions = QLabel(
            "Press the keys you want to use as your push-to-talk chord, "
            "then release them all to save.\n\n"
            "Press Esc to cancel."
        )
        instructions.setWordWrap(True)
        layout.addWidget(instructions)

        # Live readout of the chord-so-far. Updated on every key event
        # so the user sees what they're committing before they release.
        self._live_label = QLabel("(no keys pressed)")
        self._live_label.setStyleSheet(
            f"color: {theme.Color.ACCENT}; font-weight: 600; padding: 6px 0px;"
        )
        layout.addWidget(self._live_label)

        # Held = currently pressed (decreases on release).
        # Peak  = largest held-set seen so far (only ever grows during
        # this session; reset between dialog opens).
        self._held_vks: set[int] = set()
        self._peak_vks: set[int] = set()
        self._captured_vks: tuple[int, ...] | None = None

    @property
    def captured_chord(self) -> tuple[int, ...] | None:
        """The committed chord, or None if the user cancelled."""
        return self._captured_vks

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.isAutoRepeat():
            return
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        vk = int(event.nativeVirtualKey())
        if vk == 0:
            # Qt couldn't determine a native VK (rare; happens for some
            # IME-composed keys). Skip — capturing a zero-VK would
            # serialize to garbage.
            return
        self._held_vks.add(vk)
        if len(self._held_vks) > len(self._peak_vks):
            self._peak_vks = set(self._held_vks)
        self._refresh_live_label()
        event.accept()

    def keyReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.isAutoRepeat():
            return
        vk = int(event.nativeVirtualKey())
        if vk == 0:
            return
        self._held_vks.discard(vk)
        # Chord commits when EVERY key has been released and the user
        # actually pressed something. Empty peak means they hit Esc-only
        # paths or zero-VK keys — nothing to save.
        if not self._held_vks and self._peak_vks:
            self._captured_vks = tuple(sorted(self._peak_vks))
            self.captured.emit(self._captured_vks)
            self.accept()
        else:
            self._refresh_live_label()
        event.accept()

    def _refresh_live_label(self) -> None:
        if not self._peak_vks:
            self._live_label.setText("(no keys pressed)")
            return
        # Preview using the same renderer the save path uses, so what
        # the user sees here is what shows up in the dropdown after save.
        serialized = format_vk_chord(tuple(sorted(self._peak_vks)))
        self._live_label.setText(chord_display_name(serialized))


class SettingsPanel(QDialog):
    """Modal settings dialog.

    Emits `settings_saved(AppConfig)` after persisting. The owner is expected
    to re-apply anything that needs a runtime poke (theme, hotkey rebind);
    this dialog does NOT mutate live runtime objects directly.
    """

    settings_saved = pyqtSignal(object)  # AppConfig

    AVAILABLE_MODELS = [
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
    ]

    HOTKEY_LABELS = {
        "ctrl+alt": "Ctrl + Alt  (default)",
        "ctrl+shift": "Ctrl + Shift",
        "alt+shift": "Alt + Shift",
        "right_alt": "Right Alt only",
        "alt+space": "Alt + Space",
    }

    def __init__(self, config: AppConfig, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("HeyBuddy Settings")
        self.config = config
        # Apply the shared button styling so the dialog's buttons (including
        # the auto-created Save/Cancel from QDialogButtonBox) look like the
        # ones in MainPanel and respond to hover / press.
        self.setStyleSheet(theme.button_stylesheet())

        form = QFormLayout()

        # ---- Worker URL + ping ----
        self.worker_url = QLineEdit(config.worker_url)
        worker_row = QHBoxLayout()
        worker_row.addWidget(self.worker_url, 1)
        self.ping_btn = QPushButton("Ping")
        self.ping_btn.setToolTip(
            "POST /transcribe-token to verify the Worker is reachable."
        )
        self.ping_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.ping_btn.clicked.connect(self._ping_worker)
        worker_row.addWidget(self.ping_btn)
        form.addRow("Worker URL", worker_row)

        # ---- Hotkey preset + Capture button ----
        self.hotkey = QComboBox()
        for preset_name in HOTKEY_PRESETS.keys():
            self.hotkey.addItem(self.HOTKEY_LABELS.get(preset_name, preset_name), preset_name)
        # If the user previously saved a custom "vk:..." chord, restore
        # it as an extra dropdown item showing its friendly name.
        # Otherwise findData below picks one of the preset rows.
        if config.hotkey.startswith("vk:"):
            self.hotkey.addItem(
                f"Custom: {chord_display_name(config.hotkey)}",
                config.hotkey,
            )
        current_preset_index = self.hotkey.findData(config.hotkey)
        if current_preset_index >= 0:
            self.hotkey.setCurrentIndex(current_preset_index)

        # "Capture" button opens the modal capture dialog. On success,
        # the captured chord is inserted (or refreshed) as a single
        # "Custom: ..." dropdown row and selected. The actual hot-swap
        # of the live LowLevelKeyboardHook happens in _save (re-uses
        # the existing hotkey_changed path).
        self.hotkey_capture_btn = QPushButton("Capture")
        self.hotkey_capture_btn.setToolTip(
            "Press a key combination to use as the push-to-talk chord."
        )
        self.hotkey_capture_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.hotkey_capture_btn.clicked.connect(self._open_hotkey_capture)

        hotkey_row = QHBoxLayout()
        hotkey_row.addWidget(self.hotkey, 1)
        hotkey_row.addWidget(self.hotkey_capture_btn)
        form.addRow("Push-to-talk", hotkey_row)

        # ---- Model ----
        self.model = QComboBox()
        self.model.addItems(self.AVAILABLE_MODELS)
        if config.model in self.AVAILABLE_MODELS:
            self.model.setCurrentText(config.model)
        else:
            self.model.addItem(config.model)
            self.model.setCurrentText(config.model)
        form.addRow("Model", self.model)

        # ---- Behavior toggles ----
        self.tts_enabled = QCheckBox("Speak responses with ElevenLabs")
        self.tts_enabled.setChecked(config.tts_enabled)
        form.addRow(self.tts_enabled)

        self.screen_enabled = QCheckBox("Send screenshot with each turn")
        self.screen_enabled.setChecked(config.screen_capture_enabled)
        form.addRow(self.screen_enabled)

        self.transient_mode = QCheckBox(
            "Transient cursor mode (hide panel during turns)"
        )
        self.transient_mode.setChecked(config.transient_cursor_mode)
        form.addRow(self.transient_mode)

        self.autostart = QCheckBox("Launch HeyBuddy at Windows sign-in")
        # Trust the registry, not the persisted config, on dialog open — the
        # user may have toggled it from outside the app.
        self.autostart.setChecked(is_autostart_enabled())
        form.addRow(self.autostart)

        # ---- Microphone picker ----
        # Populated from sounddevice.query_devices(); "System default"
        # (data=None) is always the first row so users have a known-good
        # fallback if their saved device disappeared (e.g. USB mic
        # unplugged). The selected item's `data` carries the integer
        # device index that AudioRecorder feeds to sd.InputStream.
        self.mic_device = QComboBox()
        self.mic_device.addItem("System default", None)
        for device_index, device_name in _enumerate_input_devices():
            self.mic_device.addItem(device_name, device_index)
        # Restore the saved selection. findData returns -1 if the saved
        # index is no longer present — fall back to "System default".
        saved_device_index = config.audio.input_device_index
        restore_idx = self.mic_device.findData(saved_device_index)
        if restore_idx < 0:
            restore_idx = 0
        self.mic_device.setCurrentIndex(restore_idx)
        form.addRow("Microphone", self.mic_device)

        # ---- Diagnostics: mic test ----
        self.mic_btn = QPushButton("Test microphone")
        self.mic_btn.setToolTip(
            "Record half a second and report whether anything came through."
        )
        self.mic_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mic_btn.clicked.connect(self._test_microphone)
        form.addRow("Diagnostics", self.mic_btn)

        # ---- Status line for diagnostic feedback ----
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color:#8a93a3; font-size:11px;")

        # ---- Buttons ----
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        # Save/Cancel are auto-created by QDialogButtonBox, so we can't
        # setCursor at construction. Iterate the box's children and stamp
        # the pointer cursor on each one — matches the rest of the app.
        for button in buttons.buttons():
            button.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.status_label)
        layout.addWidget(buttons)

    # ----- diagnostics -----
    def _ping_worker(self) -> None:
        self._set_status("Pinging Worker...", ok=True)
        # Use the *current text*, not the persisted config — the user may be
        # mid-edit when they want to test.
        result = ping_worker(self.worker_url.text())
        self._set_status(f"{result.title}: {result.detail}", ok=result.ok)

    def _test_microphone(self) -> None:
        self._set_status("Capturing 0.4s of audio...", ok=True)
        result = check_microphone()
        self._set_status(f"{result.title}: {result.detail}", ok=result.ok)

    def _open_hotkey_capture(self) -> None:
        """Show the capture modal; on accept, install the captured chord.

        The serialized "vk:..." string is added to the dropdown as a
        "Custom: <display>" item (or refreshed if one was already
        there) and selected. The hot-swap to the live LL hook waits
        until the user clicks Save — same path as preset changes.
        """
        dialog = _HotkeyCaptureDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        captured = dialog.captured_chord
        if not captured:
            return
        serialized = format_vk_chord(captured)
        display = f"Custom: {chord_display_name(serialized)}"
        # If we already added a "Custom: ..." row in __init__ from a
        # previously-saved chord, replace it instead of stacking up
        # multiple custom rows in the dropdown.
        existing_custom_idx = -1
        for i in range(self.hotkey.count()):
            data = self.hotkey.itemData(i)
            if isinstance(data, str) and data.startswith("vk:"):
                existing_custom_idx = i
                break
        if existing_custom_idx >= 0:
            self.hotkey.setItemText(existing_custom_idx, display)
            self.hotkey.setItemData(existing_custom_idx, serialized)
            self.hotkey.setCurrentIndex(existing_custom_idx)
        else:
            self.hotkey.addItem(display, serialized)
            self.hotkey.setCurrentIndex(self.hotkey.count() - 1)

    def _set_status(self, message: str, ok: bool) -> None:
        color = "#9ee493" if ok else "#ff8787"
        self.status_label.setText(message)
        self.status_label.setStyleSheet(f"color:{color}; font-size:11px;")

    # ----- save -----
    def _save(self) -> None:
        self.config.worker_url = self.worker_url.text().strip()
        self.config.model = self.model.currentText().strip()
        new_hotkey = self.hotkey.currentData() or "ctrl+alt"
        hotkey_changed = new_hotkey != self.config.hotkey
        self.config.hotkey = new_hotkey
        self.config.tts_enabled = self.tts_enabled.isChecked()
        self.config.screen_capture_enabled = self.screen_enabled.isChecked()
        self.config.transient_cursor_mode = self.transient_mode.isChecked()
        # Mic picker: currentData() returns None for "System default" or
        # an int for a real device. Track whether it changed so we only
        # poke the live recorder when needed.
        new_mic_index = self.mic_device.currentData()
        mic_changed = new_mic_index != self.config.audio.input_device_index
        self.config.audio.input_device_index = new_mic_index

        # Push autostart change through to the registry. We persist the
        # checkbox state into config as a hint for the next dialog opening,
        # but the registry is the source of truth.
        want_autostart = self.autostart.isChecked()
        if want_autostart and not is_autostart_enabled():
            ok = enable_autostart(autostart_command_for_current_process())
            if not ok:
                QMessageBox.warning(
                    self,
                    "Autostart",
                    "Failed to write the autostart registry entry. "
                    "Try running HeyBuddy as your user (not as admin).",
                )
        elif not want_autostart and is_autostart_enabled():
            disable_autostart()
        self.config.autostart_enabled = is_autostart_enabled()

        self.config.save()

        # Rebind the live hotkey monitor so the change takes effect
        # immediately — the owning panel hung the monitor off itself in
        # main.py specifically so we could reach it here.
        if hotkey_changed:
            owning_panel = self.parent()
            monitor = getattr(owning_panel, "hotkey_monitor", None)
            if monitor is not None:
                try:
                    monitor.rebind(new_hotkey)
                except Exception:
                    pass

        # Hot-swap the recorder's input device so the next push-to-talk
        # uses the new mic without a restart. Reached via the panel's
        # manager → recorder chain. set_input_device only pins the index
        # for subsequent start() calls; any in-flight recording finishes
        # on the old device, which is the correct behavior.
        if mic_changed:
            owning_panel = self.parent()
            recorder = getattr(getattr(owning_panel, "manager", None), "recorder", None)
            if recorder is not None:
                try:
                    recorder.set_input_device(new_mic_index)
                except Exception:
                    log.exception("Failed to hot-swap recorder input device")

        self.settings_saved.emit(self.config)
        self.accept()
