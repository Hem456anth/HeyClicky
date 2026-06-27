"""Centralized design tokens — colors, radii, spacing, typography.

This module is the single source of truth for every visual constant used in
the UI. The project convention (per `CLAUDE.md`): **no hardcoded colors,
radii, or spacing values anywhere outside this file.** Consumers import the
token they need.

Why a separate module:

* Theme changes (e.g. dark -> light, or rebranding accents) become one-file
  edits instead of grep-and-replace across `main_window.py`,
  `overlay_window.py`, `settings_panel.py`, etc.
* New widgets get a consistent look by default — there's no "what color was
  the panel background again?" guesswork.
* Future Phase-4 work (theming, accessibility contrast modes) only needs
  to override values here, not rewrite layout code.

Conventions in this file:

* Numeric tokens are integers in **pixels at 1.0 DPI scale**. Qt and the
  per-monitor DPI awareness layer handle scaling.
* Colors are CSS hex strings (the form Qt's stylesheets accept) so they
  can drop directly into `setStyleSheet(...)` calls.
* Tokens are grouped into small classes (`Color`, `Radius`, `Spacing`,
  `Typography`) instead of being loose module-level constants. The class
  prefix gives IDE autocomplete (`Color.<TAB>`) without bloating the
  importer's namespace.

This file is data only — no behavior, no Qt imports. It is safe to import
from any module, including non-UI ones if they ever need to format a
color string (e.g. logging an error in the brand red).
"""
from __future__ import annotations


class Color:
    """Color tokens. CSS hex strings, ready for Qt stylesheets."""

    # ----- backgrounds -----
    # Frameless panel background (the dark slab the chat lives on).
    BG_PANEL = "#1a1d24"
    # Alpha (0-255) used for BG_PANEL when DWM blur-behind/Mica/Acrylic is
    # composited behind the window. Used as `rgba(BG_PANEL, BG_PANEL_BLUR_ALPHA)`.
    # On builds without DWM support the chrome stays fully opaque, so this
    # constant is irrelevant there.
    BG_PANEL_BLUR_ALPHA = 215
    # Inset surface: input fields, code blocks, anything that should look
    # "lower" than the panel itself.
    BG_INSET = "#0f1218"
    # Borders, dividers, disabled-button backgrounds.
    BG_BORDER = "#2d3340"

    # ----- text -----
    TEXT_PRIMARY = "#e6e8eb"     # body text on BG_PANEL
    TEXT_DIM = "#8a93a3"         # hints, captions, secondary labels
    TEXT_INVERTED = "#ffffff"    # text on saturated brand/state pills

    # ----- brand / interactive -----
    # The "Clicky blue" used for the cursor overlay dot, the primary
    # button, and the focus accent. Matches upstream macOS Clicky.
    ACCENT = "#2e90fa"
    ACCENT_HOVER = "#1f7ad8"
    ACCENT_PRESSED = "#1864ab"

    # ----- state machine chip colors -----
    # One per `core.companion_manager.CompanionState`. Used by the state
    # row in the panel and (later) by the per-state tray icon tint.
    STATE_IDLE = "#2b8a3e"
    STATE_LISTENING = "#1971c2"
    STATE_PROCESSING = "#9c36b5"
    STATE_RESPONDING = "#0c8599"
    STATE_ERROR = "#c92a2a"

    # ----- chat bubbles -----
    USER_BUBBLE = "#2e3a4f"
    ASSISTANT_BUBBLE = "#0f3a52"

    # ----- error banner -----
    ERROR_BG = "#3a1f24"
    ERROR_BORDER = "#5a2a30"
    ERROR_TEXT = "#ffb1b1"

    # ----- diagnostics (Settings panel status line) -----
    SUCCESS_TEXT = "#9ee493"
    WARNING_TEXT = "#ffd591"

    # ----- cursor overlay -----
    # Same hue as ACCENT but reserved as its own token so theming the panel
    # never accidentally moves the on-screen "where Claude is pointing"
    # signal away from the upstream blue.
    OVERLAY_DOT = "#2e90fa"
    OVERLAY_LABEL_BG = "#141e30"       # RGBA-friendly via stylesheet
    OVERLAY_LABEL_BG_ALPHA = 220       # 0-255; consumers compose this in

    # ----- waveform meter -----
    # Live mic-level sparkline shown only while the panel state is LISTENING.
    # Separate token so re-tinting the meter doesn't accidentally move the
    # accent color used by buttons and the overlay.
    METER_BAR = "#2e90fa"


class Radius:
    """Corner-radius tokens in pixels."""

    # Tight rounding — input fields, small buttons, badges.
    SMALL = 6
    # Default rounding — most buttons, bubbles, dialogs.
    MEDIUM = 8
    # Friendlier rounding — chat bubbles, status banners.
    LARGE = 10
    # Pill / capsule — state chip, fully rounded micro-controls.
    PILL = 12
    # Frameless panel itself.
    PANEL = 12
    # Status-row indicator dot (circle radius, not corner radius). Lives
    # here so the dot stays visually balanced with other UI radii.
    STATUS_DOT = 6


class Spacing:
    """Spacing tokens in pixels.

    Pick by **role**, not by raw pixel value: `Spacing.MD` between related
    fields, `Spacing.LG` between sections. That way a future density change
    is one constant edit.
    """

    XS = 4         # icon padding, hairline gaps
    SM = 6         # input padding, micro-gaps in a row
    MD = 8         # default vertical/horizontal gap inside a section
    LG = 12        # outer panel padding, gap between sections
    XL = 16        # large outer margin, dialog edges
    XXL = 24       # generous separation between unrelated areas


class Shadow:
    """Drop-shadow tokens for elevated surfaces (frameless panel, dialogs).

    On Windows, a frameless `QWidget` doesn't inherit the OS drop shadow that
    native title-bar windows get for free, so we paint our own via
    `QGraphicsDropShadowEffect`. The effect needs three things:

    * `BLUR_RADIUS` — softness of the shadow halo
    * `OFFSET_Y`    — vertical drop distance (a small positive value reads as
                      "this window sits above its background")
    * `COLOR_ALPHA` — opacity of the (always-black) shadow, 0-255

    Plus we need the outer layout to reserve `MARGIN` pixels around the
    visible chrome so the shadow has somewhere to render. That margin is
    listed here (not under `Spacing`) because its size is dictated by the
    blur radius, not by general layout density.
    """

    # Panel-level shadow.
    PANEL_BLUR_RADIUS = 24
    PANEL_OFFSET_Y = 4
    PANEL_COLOR_ALPHA = 140
    PANEL_MARGIN = 16          # outer-layout margin so the blur isn't clipped


class Typography:
    """Font-family and font-size tokens.

    Centralized so future Phase-4 work (font fallback chains, internationaliz-
    ation, accessibility scale) only changes one file.
    """

    # Windows-native UI font. Falls back via Qt's font matcher.
    FONT_FAMILY_UI = "Segoe UI"
    # Used for the cursor overlay label so it reads at distance.
    FONT_FAMILY_OVERLAY = "Segoe UI"

    # Sizes in points (Qt's default unit for QFont). Per Qt convention,
    # points scale with the system DPI so we don't have to remap them.
    SIZE_SMALL = 10
    SIZE_BODY = 11
    SIZE_TITLE = 14

    # Common weights — Qt accepts 0-99 via QFont.Weight enum; we name the
    # ones we actually use rather than the full set.
    WEIGHT_REGULAR = 400
    WEIGHT_DEMI = 600
    WEIGHT_BOLD = 700


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def button_stylesheet() -> str:
    """CSS for `QPushButton` + `:hover` + `:pressed`, composed from tokens.

    Single source of truth so every button across the app looks identical
    and re-themes in one place. Returned as a string so callers can paste
    it into a parent widget's `setStyleSheet(...)` alongside their own
    rules — the panel does this to fold button styling into its larger
    chrome stylesheet, while the Settings dialog applies it standalone.
    """
    return (
        f"QPushButton {{ "
        f"background: {Color.ACCENT}; "
        f"border: none; "
        f"border-radius: {Radius.SMALL}px; "
        f"padding: {Spacing.SM}px {Spacing.LG}px; "
        f"color: {Color.TEXT_INVERTED}; "
        f"font-weight: 600; "
        f"}} "
        f"QPushButton:hover {{ background: {Color.ACCENT_HOVER}; }} "
        f"QPushButton:pressed {{ background: {Color.ACCENT_PRESSED}; }} "
        f"QPushButton:disabled {{ background: {Color.BG_BORDER}; color: {Color.TEXT_DIM}; }} "
    )


def rgba(hex_color: str, alpha_0_to_255: int) -> str:
    """Compose a `rgba(R,G,B,A)` CSS string from a `#RRGGBB` token + alpha.

    Used when an existing Color token needs to be drawn semi-transparent
    (e.g. the cursor overlay's glow halo). Keeps the underlying hue in
    `Color` instead of inventing a second token for every translucency.
    """
    if not hex_color.startswith("#") or len(hex_color) != 7:
        raise ValueError(f"Expected #RRGGBB, got {hex_color!r}")
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    a = max(0, min(255, alpha_0_to_255))
    return f"rgba({r}, {g}, {b}, {a})"
