"""
Display helper for payloads – automatic coordinate scaling for any LCD size.

Usage in a payload:
    from payloads._display_helper import ScaledDraw, scaled_font

    img = Image.new("RGB", (WIDTH, HEIGHT), "black")
    d = ScaledDraw(img)           # drop-in replacement for ImageDraw.Draw
    font = scaled_font()          # readable font for current resolution

All pixel coordinates passed to d.text(), d.rectangle(), d.line(), etc.
are automatically scaled from 128-base to the actual LCD resolution.
For non-square displays (e.g. 480x320) with a 50px button bar at the bottom:
  X: 128-base  ->  480  (scale 3.75)
  Y: 128-base  ->  270  (scale 2.109)  ← maps to content area only,
                                          leaving the button bar untouched
"""
import os, sys, json
from PIL import ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Read display dimensions from gui_conf.json
# ---------------------------------------------------------------------------
_LCD_W = 128
_LCD_H = 128
_CONF_PATHS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gui_conf.json"),
    "/root/Raspyjack/gui_conf.json",
]
for _p in _CONF_PATHS:
    if os.path.isfile(_p):
        try:
            with open(_p, "r") as _f:
                _conf = json.load(_f)
            _disp = _conf.get("DISPLAY", {})
            _LCD_W = int(_disp.get("WIDTH",  _disp.get("width",  128)))
            _LCD_H = int(_disp.get("HEIGHT", _disp.get("height", 128)))
        except Exception:
            pass
        break

# Button bar composited by LCD_480x320 over the bottom of the framebuffer
_LCD_BAR_H = 0
try:
    from LCD_480x320 import BUTTON_BAR_H as _LCD_BAR_H
except ImportError:
    pass

LCD_BUTTON_BAR_H = _LCD_BAR_H               # height of on-screen button bar (px)
LCD_CONTENT_H    = _LCD_H - _LCD_BAR_H      # usable pixel height above the bar

# Per-axis scale factors from 128-base to actual resolution.
# Y maps to the *content* area only so payload drawing never overlaps the bar.
LCD_SCALE_X = _LCD_W / 128
LCD_SCALE_Y = max(LCD_CONTENT_H, 128) / 128
LCD_SCALE   = min(LCD_SCALE_X, LCD_SCALE_Y)   # legacy single-axis (safe minimum)


def SX(v):
    """Scale a 128-base X value to the current display width."""
    return int(v * LCD_SCALE_X)

def SY(v):
    """Scale a 128-base Y value to the current display height."""
    return int(v * LCD_SCALE_Y)

def S(v):
    """Scale a 128-base value using the smaller axis (fits both X and Y)."""
    return int(v * LCD_SCALE)


def _best_scale():
    """Return the best available minimum-axis scale factor.

    Priority:
    1. Module-level LCD_SCALE (from gui_conf.json) if > 1.0
    2. Constants from LCD_480x320 module (always available on device)
    3. Fall back to 1.0
    """
    if LCD_SCALE > 1.0:
        return LCD_SCALE
    try:
        import importlib as _il
        _m = _il.import_module("LCD_480x320")
        _w = getattr(_m, "LCD_WIDTH",  128)
        _h = getattr(_m, "LCD_HEIGHT", 128)
        return min(_w, _h) / 128.0
    except Exception:
        return LCD_SCALE


def scaled_font(size=10):
    """Return a TrueType font scaled for the current display.

    *size* is the desired point size on a 128px screen; the returned font
    is proportionally larger on bigger panels.
    """
    scaled_size = max(8, int(size * _best_scale()))
    try:
        return ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", scaled_size
        )
    except Exception:
        return ImageFont.load_default()


# ---------------------------------------------------------------------------
# ScaledDraw – wraps ImageDraw.Draw, scaling all 128-base coordinates
# ---------------------------------------------------------------------------
class ScaledDraw:
    """Drop-in replacement for ``ImageDraw.Draw`` that auto-scales coordinates.

    Scale factors are derived directly from the *image* dimensions so that
    the helper works correctly regardless of whether gui_conf.json was found.

    On a 128×128 image no scaling is applied (passthrough mode).
    On a 480×320 image X is scaled by 3.75 and Y by 2.50.
    """

    def __init__(self, image):
        self._draw = ImageDraw.Draw(image)
        w, h = image.size
        # Subtract the button bar so payload content stays in the content area
        bar_h = 0
        try:
            from LCD_480x320 import BUTTON_BAR_H as bar_h
        except ImportError:
            pass
        content_h = max(h - bar_h, 1)
        self._sx = w / 128.0           # X: full width
        self._sy = content_h / 128.0   # Y: content area only (above button bar)
        self._s  = min(self._sx, self._sy)
        self._passthrough = (abs(self._sx - 1.0) < 0.001
                             and abs(self._sy - 1.0) < 0.001)

    # -- Internal coordinate scalers ----------------------------------------

    def _px(self, v):  return int(v * self._sx)
    def _py(self, v):  return int(v * self._sy)
    def _ps(self, v):  return int(v * self._s)

    def _sp(self, pt):
        """Scale a (x, y) point."""
        return (self._px(pt[0]), self._py(pt[1]))

    def _sc(self, coords):
        """Scale a flat 4-value box, list of (x,y) tuples, or 2-value point."""
        if not coords:
            return coords
        if isinstance(coords[0], (list, tuple)):
            return [(self._px(p[0]), self._py(p[1])) for p in coords]
        if len(coords) == 4:
            return [self._px(coords[0]), self._py(coords[1]),
                    self._px(coords[2]), self._py(coords[3])]
        if len(coords) == 2:
            return (self._px(coords[0]), self._py(coords[1]))
        return coords

    # -- Scaled drawing primitives ------------------------------------------

    def text(self, xy, text, fill=None, font=None, anchor=None, **kw):
        if not self._passthrough:
            xy = self._sp(xy)
        self._draw.text(xy, text, fill=fill, font=font, anchor=anchor, **kw)

    def rectangle(self, xy, fill=None, outline=None, width=1, **kw):
        if not self._passthrough:
            xy = self._sc(xy)
            width = max(1, self._ps(width)) if width > 1 else width
        self._draw.rectangle(xy, fill=fill, outline=outline, width=width, **kw)

    def line(self, xy, fill=None, width=1, **kw):
        if not self._passthrough:
            xy = self._sc(xy)
            width = max(1, self._ps(width)) if width > 1 else width
        self._draw.line(xy, fill=fill, width=width, **kw)

    def ellipse(self, xy, fill=None, outline=None, width=1, **kw):
        if not self._passthrough:
            xy = self._sc(xy)
            width = max(1, self._ps(width)) if width > 1 else width
        self._draw.ellipse(xy, fill=fill, outline=outline, width=width, **kw)

    def polygon(self, xy, fill=None, outline=None, **kw):
        if not self._passthrough:
            xy = self._sc(xy)
        self._draw.polygon(xy, fill=fill, outline=outline, **kw)

    def arc(self, xy, start, end, fill=None, width=1, **kw):
        if not self._passthrough:
            xy = self._sc(xy)
            width = max(1, self._ps(width)) if width > 1 else width
        self._draw.arc(xy, start, end, fill=fill, width=width, **kw)

    def pieslice(self, xy, start, end, fill=None, outline=None, width=1, **kw):
        if not self._passthrough:
            xy = self._sc(xy)
            width = max(1, self._ps(width)) if width > 1 else width
        self._draw.pieslice(xy, start, end, fill=fill, outline=outline, width=width, **kw)

    def textbbox(self, xy, text, font=None, **kw):
        if not self._passthrough:
            xy = self._sp(xy)
        return self._draw.textbbox(xy, text, font=font, **kw)

    def textlength(self, text, font=None, **kw):
        return self._draw.textlength(text, font=font, **kw)

    # -- Passthrough for anything else --------------------------------------
    def __getattr__(self, name):
        return getattr(self._draw, name)
