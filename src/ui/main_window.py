"""Floating panel UI that pops up from the tray icon.

Widget tree (Cycle 2 introduces the chrome / shadow split):

    MainPanel (top-level QWidget, transparent background, frameless)
      outer QVBoxLayout(margins = Shadow.PANEL_MARGIN on all sides)
        └── _PanelChrome (QFrame, visible rounded slab, drop-shadowed)
             inner QVBoxLayout
               ├── _DraggableTitleBar  ← grab here to move the window
               │     ├── "HeyBuddy" label
               │     ├── stretch
               │     ├── ⚙ Settings button
               │     └── ✕ Hide button
               ├── State chip (Idle / Listening / ...)
               ├── Error banner (initially hidden)
               ├── Chat scrollback (user / assistant bubbles)
               ├── Composer (input + Send)
               └── "Hold CTRL+ALT to talk" hint

Why the outer/chrome split: Qt's `QGraphicsDropShadowEffect` paints into
the widget's own bounding rect. If we applied it to MainPanel directly,
the blur would be clipped at the window edge. The outer layout reserves
`theme.Shadow.PANEL_MARGIN` pixels of transparent slack so the shadow
has somewhere to render.
"""
from __future__ import annotations

from PyQt6.QtCore import QPoint, QRect, Qt, QTimer, pyqtSlot
from PyQt6.QtGui import (
    QColor,
    QFont,
    QMouseEvent,
    QMoveEvent,
    QShowEvent,
)
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
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
from ..utils.logger import get_logger
from ..utils.win32 import apply_blur_behind
from . import theme
from .settings_panel import SettingsPanel

log = get_logger(__name__)

# Mapping from manager state to the chip label + the theme token for its
# background color. Sourced from `theme.Color` so future re-skinning is a
# one-file change.
_STATE_LABELS: dict[CompanionState, tuple[str, str]] = {
    CompanionState.IDLE: ("Idle", theme.Color.STATE_IDLE),
    CompanionState.LISTENING: ("Listening...", theme.Color.STATE_LISTENING),
    CompanionState.PROCESSING: ("Processing...", theme.Color.STATE_PROCESSING),
    CompanionState.RESPONDING: ("Responding...", theme.Color.STATE_RESPONDING),
    CompanionState.ERROR: ("Error", theme.Color.STATE_ERROR),
}


class _DraggableTitleBar(QWidget):
    """Custom title bar — clicking and dragging moves the parent panel.

    Frameless windows don't get title-bar dragging from Qt. We implement it
    here by capturing the mouse on press, computing the offset between the
    click point and the panel's top-left, and translating the panel during
    the drag.

    The drag cursor changes to `SizeAllCursor` on hover so the affordance
    is obvious without a visible chrome.
    """

    def __init__(self, panel_to_move: QWidget, parent: QWidget) -> None:
        super().__init__(parent)
        self._panel_to_move = panel_to_move
        # Offset from the panel's top-left to the global cursor position at
        # mouse-down. We subtract this from each subsequent global cursor
        # position to compute where the panel should move.
        self._drag_grab_offset: QPoint | None = None
        self.setCursor(Qt.CursorShape.SizeAllCursor)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            global_pos = event.globalPosition().toPoint()
            self._drag_grab_offset = global_pos - self._panel_to_move.pos()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if (
            self._drag_grab_offset is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            global_pos = event.globalPosition().toPoint()
            self._panel_to_move.move(global_pos - self._drag_grab_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        self._drag_grab_offset = None
        event.accept()


class MainPanel(QWidget):
    """The floating tray-launched window.

    Public attributes worth knowing about from outside:

    * `hotkey_monitor` — set by `main.py` after construction so the
      Settings dialog can rebind the chord without restart.
    """

    # How long after the last move() to persist the new position. Avoids
    # writing settings.json on every pixel of drag.
    _POSITION_SAVE_DEBOUNCE_MS = 400

    def __init__(self, config: AppConfig, manager: CompanionManager) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | (
                Qt.WindowType.WindowStaysOnTopHint
                if config.ui.always_on_top
                else Qt.WindowType.Widget
            ),
        )
        # Critical for the drop shadow: we draw the visible chrome inside an
        # inner QFrame and leave the outer widget transparent so the blur
        # bleeds through the margin instead of being clipped.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self.config = config
        self.manager = manager
        self.hotkey_monitor = None  # wired by main.py post-construction
        # Remembered so showEvent can re-call `_apply_theme(...)` with
        # `dwm_blur_active=True` once the HWND exists and we know whether
        # DWM accepted a backdrop.
        self._current_theme_name = config.ui.theme
        self._dwm_blur_active = False
        # Set True the first time we try `apply_blur_behind`; prevents
        # re-attempting (and re-styling) on every show/hide cycle.
        self._dwm_backdrop_attempted = False

        # Include the shadow margin in the overall window size so the
        # visible chrome stays the configured panel_width / panel_height.
        margin_total = theme.Shadow.PANEL_MARGIN * 2
        self.resize(
            config.ui.panel_width + margin_total,
            config.ui.panel_height + margin_total,
        )
        self.setObjectName("MainPanel")
        self._apply_theme(config.ui.theme)
        self._build_ui()
        self._wire_signals()

        # Debounce timer for moveEvent → persist position.
        self._position_save_timer = QTimer(self)
        self._position_save_timer.setSingleShot(True)
        self._position_save_timer.timeout.connect(self._persist_position_now)
        # Suppressed until the first showEvent so the initial restore-position
        # move() doesn't immediately overwrite the saved coords.
        self._position_save_armed = False

        # Auto-clear timer for the error banner so stale errors don't sit
        # there forever during normal turn cycling.
        self._error_clear_timer = QTimer(self)
        self._error_clear_timer.setSingleShot(True)
        self._error_clear_timer.timeout.connect(self._clear_error_banner)

    # ---- layout ----
    def _build_ui(self) -> None:
        # Outer transparent layout — reserves shadow margin only.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            theme.Shadow.PANEL_MARGIN,
            theme.Shadow.PANEL_MARGIN,
            theme.Shadow.PANEL_MARGIN,
            theme.Shadow.PANEL_MARGIN,
        )
        outer.setSpacing(0)

        # Inner chrome — the rounded, opaque slab that carries the shadow.
        self.chrome = QFrame(self)
        self.chrome.setObjectName("PanelChrome")
        drop_shadow = QGraphicsDropShadowEffect(self.chrome)
        drop_shadow.setBlurRadius(theme.Shadow.PANEL_BLUR_RADIUS)
        drop_shadow.setOffset(0, theme.Shadow.PANEL_OFFSET_Y)
        drop_shadow.setColor(QColor(0, 0, 0, theme.Shadow.PANEL_COLOR_ALPHA))
        self.chrome.setGraphicsEffect(drop_shadow)
        outer.addWidget(self.chrome)

        root = QVBoxLayout(self.chrome)
        root.setContentsMargins(
            theme.Spacing.LG,
            theme.Spacing.LG,
            theme.Spacing.LG,
            theme.Spacing.LG,
        )
        root.setSpacing(theme.Spacing.MD)

        # ---- title bar (draggable) ----
        title_bar = _DraggableTitleBar(panel_to_move=self, parent=self.chrome)
        title_bar_layout = QHBoxLayout(title_bar)
        title_bar_layout.setContentsMargins(0, 0, 0, 0)

        title = QLabel("HeyBuddy")
        title_font = QFont(
            theme.Typography.FONT_FAMILY_UI,
            theme.Typography.SIZE_TITLE,
            theme.Typography.WEIGHT_DEMI,
        )
        title.setFont(title_font)

        self.settings_btn = QPushButton("⚙")
        self.settings_btn.setFixedWidth(32)
        self.settings_btn.setToolTip("Settings")
        self.settings_btn.clicked.connect(self.open_settings)

        self.close_btn = QPushButton("✕")
        self.close_btn.setFixedWidth(32)
        self.close_btn.setToolTip("Hide panel")
        self.close_btn.clicked.connect(self.hide)

        title_bar_layout.addWidget(title)
        title_bar_layout.addStretch(1)
        title_bar_layout.addWidget(self.settings_btn)
        title_bar_layout.addWidget(self.close_btn)
        root.addWidget(title_bar)

        # ---- state chip ----
        self.state_chip = QLabel("Idle")
        self.state_chip.setObjectName("stateChip")
        self.state_chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state_chip.setFixedHeight(26)
        self._set_state_chip(CompanionState.IDLE)
        root.addWidget(self.state_chip)

        # ---- error banner ----
        self.error_banner = QLabel("")
        self.error_banner.setObjectName("errorBanner")
        self.error_banner.setWordWrap(True)
        self.error_banner.setVisible(False)
        root.addWidget(self.error_banner)

        # ---- chat scrollback ----
        self.chat_container = QWidget()
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setContentsMargins(0, 0, 0, 0)
        self.chat_layout.setSpacing(theme.Spacing.MD)
        self.chat_layout.addStretch(1)

        self.scroll = QScrollArea()
        self.scroll.setWidget(self.chat_container)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        root.addWidget(self.scroll, 1)

        # ---- composer ----
        composer = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText(
            f"Type a message, or hold {self.config.hotkey.upper()} to talk..."
        )
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

    def _apply_theme(self, theme_name: str) -> None:
        """Apply the dark stylesheet using `theme.py` tokens.

        Cycle 2 introduced the panel/chrome split; cycle 3 adds the
        DWM-translucent variant of the chrome background. When DWM has
        composited a blur/Mica/Acrylic backdrop, the chrome switches to a
        semi-transparent fill (`rgba(BG_PANEL, BG_PANEL_BLUR_ALPHA)`) so
        the desktop tint reads through. Without DWM, the chrome stays
        fully opaque or it would look like floating text on an empty
        invisible window.

        Other rules (buttons, bubbles, etc.) keep their existing hex
        literals; later cycles ("Hover states", etc.) migrate them.
        """
        # Remember the choice so showEvent can re-call us after the DWM
        # attempt without the caller having to thread the flag through.
        self._current_theme_name = theme_name

        if theme_name == "dark":
            if self._dwm_blur_active:
                panel_bg = theme.rgba(
                    theme.Color.BG_PANEL, theme.Color.BG_PANEL_BLUR_ALPHA,
                )
            else:
                panel_bg = theme.Color.BG_PANEL
            self.setStyleSheet(
                f"""
                #MainPanel {{ background-color: transparent; }}
                #PanelChrome {{
                    background-color: {panel_bg};
                    border: 1px solid {theme.Color.BG_BORDER};
                    border-radius: {theme.Radius.PANEL}px;
                    color: {theme.Color.TEXT_PRIMARY};
                }}
                QLabel {{ color: {theme.Color.TEXT_PRIMARY}; }}
                QLabel#hint {{ color: {theme.Color.TEXT_DIM}; font-size: 11px; }}
                QLineEdit {{ background:#0f1218; border:1px solid #2d3340;
                            border-radius:6px; padding:6px 8px; color:#e6e8eb; }}
                QPushButton {{ background:#2e90fa; border:none; border-radius:6px;
                              padding:6px 12px; color:white; font-weight:600; }}
                QPushButton:hover {{ background:#1f7ad8; }}
                QLabel#stateChip {{ border-radius: 12px; padding: 2px 12px;
                                   color:white; font-weight:600; }}
                QLabel.userBubble {{ background:#2e3a4f; padding:8px 10px;
                                    border-radius:10px; }}
                QLabel.assistantBubble {{ background:#0f3a52; padding:8px 10px;
                                         border-radius:10px; }}
                QLabel#errorBanner {{ background:#3a1f24; color:#ffb1b1;
                                     border:1px solid #5a2a30; border-radius:8px;
                                     padding:6px 10px; }}
                """
            )
        else:
            self.setStyleSheet(
                f"""
                #MainPanel {{ background-color: transparent; }}
                #PanelChrome {{
                    background-color: #ffffff;
                    border: 1px solid #d0d4dc;
                    border-radius: {theme.Radius.PANEL}px;
                }}
                QLabel#stateChip {{ border-radius: 12px; padding: 2px 12px;
                                   color:white; font-weight:600; }}
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
            f"background-color: {color}; "
            f"border-radius: {theme.Radius.PILL}px; "
            f"color: {theme.Color.TEXT_INVERTED}; "
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
        self.manager.send_text(
            text, include_screenshot=self.config.screen_capture_enabled,
        )

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

    # ---- position persistence ----
    def showEvent(self, event: QShowEvent) -> None:  # type: ignore[override]
        """One-time setup that requires the native HWND to exist.

        We can't do these in __init__ because the window has no native handle
        yet — `move()` calls would be ignored on some Windows builds, and
        DWM APIs need a real HWND to attach to.
        """
        super().showEvent(event)
        if not self._position_save_armed:
            self._restore_position()
            # Arm AFTER the restore so the move() above doesn't trigger a
            # save with stale data.
            self._position_save_armed = True
        if not self._dwm_backdrop_attempted:
            self._dwm_backdrop_attempted = True
            try:
                effect = apply_blur_behind(int(self.winId()))
            except Exception:
                log.exception("apply_blur_behind raised")
                effect = "none"
            # Only flip the chrome to translucent if a backdrop was actually
            # applied. Otherwise the panel would look like floating widgets
            # on top of the desktop with no slab behind them.
            if effect != "none":
                self._dwm_blur_active = True
                self._apply_theme(self._current_theme_name)

    def moveEvent(self, event: QMoveEvent) -> None:  # type: ignore[override]
        """Debounce position saves during a drag."""
        super().moveEvent(event)
        if self._position_save_armed:
            self._position_save_timer.start(self._POSITION_SAVE_DEBOUNCE_MS)

    def _restore_position(self) -> None:
        """Move the panel to its last known position, if valid."""
        x = self.config.ui.panel_x
        y = self.config.ui.panel_y
        if x is None or y is None:
            return  # never saved — let Qt place the window
        # Validate: a monitor that held the panel may be disconnected now,
        # leaving the panel offscreen. Require the saved rect to intersect
        # the live virtual desktop before honoring it.
        primary_screen = QApplication.primaryScreen()
        if primary_screen is None:
            return
        virtual_desktop = primary_screen.virtualGeometry()
        saved_rect = QRect(x, y, self.width(), self.height())
        if not virtual_desktop.intersects(saved_rect):
            log.info(
                "Saved panel position (%d, %d) is offscreen on the current "
                "monitor layout — falling back to default placement.", x, y,
            )
            return
        self.move(x, y)

    def _persist_position_now(self) -> None:
        """Write the current position to config. Called by the debounce timer."""
        pos = self.pos()
        self.config.ui.panel_x = pos.x()
        self.config.ui.panel_y = pos.y()
        try:
            self.config.save()
        except Exception:
            log.exception("Failed to persist panel position to config")
