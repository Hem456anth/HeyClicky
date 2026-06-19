"""Floating panel UI that pops up from the tray icon.

Layout:

    +--------------------------------------+
    | HeyClicky                  [⚙] [✕]   |
    +--------------------------------------+
    |  State chip: idle | listening | ...  |
    |  --------------------------------    |
    |  Chat scrollback (user / assistant)  |
    |  --------------------------------    |
    |  [ type a message...      ] [Send]   |
    |  Hint: Hold Ctrl+Alt anywhere to     |
    |        talk to HeyClicky.            |
    +--------------------------------------+
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSlot
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core.companion_manager import CompanionManager, CompanionState
from ..models.config import AppConfig
from ..models.message import Message, Role
from .settings_panel import SettingsPanel

_STATE_LABELS = {
    CompanionState.IDLE: ("Idle", "#2b8a3e"),
    CompanionState.LISTENING: ("Listening...", "#1971c2"),
    CompanionState.PROCESSING: ("Processing...", "#9c36b5"),
    CompanionState.RESPONDING: ("Responding...", "#0c8599"),
    CompanionState.ERROR: ("Error", "#c92a2a"),
}


class MainPanel(QWidget):
    def __init__(self, config: AppConfig, manager: CompanionManager) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | (Qt.WindowType.WindowStaysOnTopHint if config.ui.always_on_top else Qt.WindowType.Widget),
        )
        self.config = config
        self.manager = manager
        # Wired by main.py so the Settings dialog can rebind without restart.
        self.hotkey_monitor = None

        self.resize(config.ui.panel_width, config.ui.panel_height)
        self.setObjectName("MainPanel")
        self._apply_theme(config.ui.theme)
        self._build_ui()
        self._wire_signals()

        # Auto-clear timer for the error banner so stale errors don't sit
        # there forever during normal turn cycling.
        self._error_clear_timer = QTimer(self)
        self._error_clear_timer.setSingleShot(True)
        self._error_clear_timer.timeout.connect(self._clear_error_banner)

    # ---- layout ----
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        # Title bar
        title_row = QHBoxLayout()
        title = QLabel("HeyClicky")
        title_font = QFont("Segoe UI", 14, QFont.Weight.DemiBold)
        title.setFont(title_font)

        self.settings_btn = QPushButton("⚙")
        self.settings_btn.setFixedWidth(32)
        self.settings_btn.setToolTip("Settings")
        self.settings_btn.clicked.connect(self.open_settings)

        self.close_btn = QPushButton("✕")
        self.close_btn.setFixedWidth(32)
        self.close_btn.setToolTip("Hide panel")
        self.close_btn.clicked.connect(self.hide)

        title_row.addWidget(title)
        title_row.addStretch(1)
        title_row.addWidget(self.settings_btn)
        title_row.addWidget(self.close_btn)
        root.addLayout(title_row)

        # State chip
        self.state_chip = QLabel("Idle")
        self.state_chip.setObjectName("stateChip")
        self.state_chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state_chip.setFixedHeight(26)
        self._set_state_chip(CompanionState.IDLE)
        root.addWidget(self.state_chip)

        # Error banner (initially hidden; revealed by _show_error)
        self.error_banner = QLabel("")
        self.error_banner.setObjectName("errorBanner")
        self.error_banner.setWordWrap(True)
        self.error_banner.setVisible(False)
        root.addWidget(self.error_banner)

        # Chat scrollback
        self.chat_container = QWidget()
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setContentsMargins(0, 0, 0, 0)
        self.chat_layout.setSpacing(8)
        self.chat_layout.addStretch(1)

        self.scroll = QScrollArea()
        self.scroll.setWidget(self.chat_container)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        root.addWidget(self.scroll, 1)

        # Composer
        composer = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("Type a message, or hold Ctrl+Alt to talk...")
        self.input.returnPressed.connect(self._send_typed)
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self._send_typed)
        composer.addWidget(self.input, 1)
        composer.addWidget(self.send_btn)
        root.addLayout(composer)

        hint = QLabel(f"Hold <b>{self.config.hotkey.upper()}</b> anywhere to talk.")
        hint.setObjectName("hint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(hint)

    def _apply_theme(self, theme: str) -> None:
        if theme == "dark":
            self.setStyleSheet(
                """
                #MainPanel { background-color: #1a1d24; border: 1px solid #2d3340;
                             border-radius: 12px; color: #e6e8eb; }
                QLabel { color: #e6e8eb; }
                QLabel#hint { color: #8a93a3; font-size: 11px; }
                QLineEdit { background:#0f1218; border:1px solid #2d3340;
                            border-radius:6px; padding:6px 8px; color:#e6e8eb; }
                QPushButton { background:#2e90fa; border:none; border-radius:6px;
                              padding:6px 12px; color:white; font-weight:600; }
                QPushButton:hover { background:#1f7ad8; }
                QLabel#stateChip { border-radius: 12px; padding: 2px 12px;
                                   color:white; font-weight:600; }
                QLabel.userBubble { background:#2e3a4f; padding:8px 10px;
                                    border-radius:10px; }
                QLabel.assistantBubble { background:#0f3a52; padding:8px 10px;
                                         border-radius:10px; }
                QLabel#errorBanner { background:#3a1f24; color:#ffb1b1;
                                     border:1px solid #5a2a30; border-radius:8px;
                                     padding:6px 10px; }
                """
            )
        else:
            self.setStyleSheet(
                """
                #MainPanel { background-color: #ffffff; border: 1px solid #d0d4dc;
                             border-radius: 12px; }
                QLabel#stateChip { border-radius: 12px; padding: 2px 12px;
                                   color:white; font-weight:600; }
                """
            )

    # ---- signal wiring ----
    def _wire_signals(self) -> None:
        self.manager.state_changed.connect(self._set_state_chip)
        self.manager.message_appended.connect(self._append_message)
        self.manager.error_occurred.connect(self._show_error)
        # Phase 2 — live transcription preview in the input placeholder.
        self.manager.transcription_partial.connect(
            lambda text: self.input.setPlaceholderText(
                f"...{text}" if text else
                f"Type a message, or hold {self.config.hotkey.upper()} to talk..."
            )
        )
        self.manager.transcription_final.connect(
            lambda _text: self.input.setPlaceholderText(
                f"Type a message, or hold {self.config.hotkey.upper()} to talk..."
            )
        )

    # ---- slots ----
    def _set_state_chip(self, state: CompanionState) -> None:
        label, color = _STATE_LABELS.get(state, ("...", "#5c5f66"))
        self.state_chip.setText(label)
        self.state_chip.setStyleSheet(
            f"background-color: {color}; border-radius: 12px; color: white;"
            f"padding: 2px 12px; font-weight: 600;"
        )
        # Clear stale errors as soon as the manager moves out of ERROR.
        # `getattr` because this slot is called during _build_ui to set the
        # initial chip color BEFORE the error banner widget exists.
        banner = getattr(self, "error_banner", None)
        if state != CompanionState.ERROR and banner is not None and banner.isVisible():
            self._clear_error_banner()
        # Transient cursor mode: collapse the panel as soon as we start
        # listening; reappearance is manual (tray click) so the user has
        # explicit control over when the panel comes back.
        if self.config.transient_cursor_mode and state == CompanionState.LISTENING:
            if self.isVisible():
                self.hide()

    def _append_message(self, msg: Message) -> None:
        bubble = QLabel(msg.content)
        bubble.setWordWrap(True)
        bubble.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        cls = "userBubble" if msg.role == Role.USER else "assistantBubble"
        bubble.setProperty("class", cls)
        # Re-apply style after dynamic property change
        bubble.setStyleSheet(
            "background:#2e3a4f; padding:8px 10px; border-radius:10px;"
            if msg.role == Role.USER
            else "background:#0f3a52; padding:8px 10px; border-radius:10px;"
        )
        # Insert before the trailing stretch
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, bubble)
        # Scroll to bottom on next tick
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _show_error(self, text: str) -> None:
        self.error_banner.setText(f"⚠ {text}")
        self.error_banner.setVisible(True)
        # Show the panel even in transient mode so the user actually sees the
        # error rather than wondering why nothing happened.
        if not self.isVisible():
            self.show()
            self.raise_()
        self._error_clear_timer.start(8_000)

    def _clear_error_banner(self) -> None:
        self.error_banner.setVisible(False)
        self.error_banner.setText("")

    def _send_typed(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self.input.clear()
        self.manager.send_text(text, include_screenshot=self.config.screen_capture_enabled)

    @pyqtSlot()
    def open_settings(self) -> None:
        dlg = SettingsPanel(self.config, self)
        dlg.settings_saved.connect(lambda cfg: self._apply_theme(cfg.ui.theme))
        dlg.exec()

    # ---- show/hide near tray ----
    @pyqtSlot()
    def toggle(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()
            self.activateWindow()
