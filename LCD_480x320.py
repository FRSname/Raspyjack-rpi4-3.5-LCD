# -*- coding:UTF-8 -*-
##
# | file        : LCD_480x320.py
# | description : 480×320 MPI3501 (ILI9486) display driver using the Linux
# |               framebuffer created by the goodtft/lcd-show kernel driver.
# |               Drop-in replacement for LCD_1in44.py – same public API.
# |
# | How it works:
# |   The lcd-show driver (LCD35-show) installs an FBTFT overlay that
# |   creates a framebuffer device (typically /dev/fb0 or /dev/fb1).
# |   This module writes PIL images as RGB565 directly to the framebuffer.
# |   No userspace SPI libraries (luma.lcd etc.) are needed – the kernel
# |   handles all SPI communication.
# |
# | Public interface kept for compatibility:
# |   LCD()          – constructor
# |   LCD_Init()     – initialise display
# |   LCD_ShowImage(image, x, y)  – push a PIL Image to the screen
# |   LCD_Clear()    – fill screen with white
#

import os
import time
import numpy as np

# ---------------------------------------------------------------------------
# WebUI frame mirror (used by device_server.py) – carried over from LCD_1in44
# ---------------------------------------------------------------------------
_FRAME_MIRROR_PATH = os.environ.get("RJ_FRAME_PATH", "/dev/shm/raspyjack_last.jpg")
_FRAME_MIRROR_ENABLED = os.environ.get("RJ_FRAME_MIRROR", "1") != "0"
try:
    _frame_fps = float(os.environ.get("RJ_FRAME_FPS", "10"))
    _FRAME_MIRROR_INTERVAL = 1.0 / max(1.0, _frame_fps)
except Exception:
    _FRAME_MIRROR_INTERVAL = 0.1
_last_frame_save = 0.0

# ---------------------------------------------------------------------------
# Display geometry
# ---------------------------------------------------------------------------
LCD_WIDTH  = 480
LCD_HEIGHT = 320

# Scan direction constant kept for backward compatibility (unused by fb)
SCAN_DIR_DFT = 1

# ---------------------------------------------------------------------------
# Framebuffer configuration
# ---------------------------------------------------------------------------
# The lcd-show driver typically creates /dev/fb0 for the SPI display.
# On setups where HDMI is fb0 and the SPI LCD is fb1, change this or set
# the environment variable RJ_FB_DEVICE.
_FB_DEVICE = os.environ.get("RJ_FB_DEVICE", "")
_fb_path: str = ""   # resolved in LCD_Init
_fb_fd = None         # file descriptor for the framebuffer

# Expected framebuffer size in bytes:  480 × 320 × 2 (RGB565)
# (same byte count for 320×480 – rotation doesn't change total size)
_FB_SIZE = LCD_WIDTH * LCD_HEIGHT * 2

# ---------------------------------------------------------------------------
# Framebuffer rotation
# ---------------------------------------------------------------------------
# The MPI3501 panel is physically 320×480 portrait.  LCD35-show's FBTFT
# overlay may or may not set the rotate parameter.  If our 480×320 landscape
# image appears rotated on screen, we compensate by rotating it before
# writing to the framebuffer.
#
# RJ_FB_ROTATE:  0 = no rotation, 90/180/270 = degrees CW
#   auto = detect from fb virtual_size (default)
# ---------------------------------------------------------------------------
_FB_ROTATE_ENV = os.environ.get("RJ_FB_ROTATE", "auto")
_fb_rotate: int = 0      # resolved in LCD_Init


def _detect_fb_rotation(fb_path: str) -> int:
    """Detect whether the fb is portrait and we need to rotate."""
    if _FB_ROTATE_ENV != "auto":
        try:
            return int(_FB_ROTATE_ENV)
        except ValueError:
            return 0

    # Read the framebuffer's native resolution
    fb_name = os.path.basename(fb_path)   # e.g. "fb0"
    vsize_path = f"/sys/class/graphics/{fb_name}/virtual_size"
    try:
        with open(vsize_path) as f:
            parts = f.read().strip().split(",")
            fb_w, fb_h = int(parts[0]), int(parts[1])
        print(f"[LCD] Framebuffer native resolution: {fb_w}×{fb_h}")
        # Our image is 480×320 (landscape).  If fb is portrait (h > w),
        # we need to rotate 90° CW so the image fits correctly.
        if fb_h > fb_w:
            print(f"[LCD] Portrait framebuffer detected → rotating image 90° CW")
            return 90
        else:
            print(f"[LCD] Landscape framebuffer → no rotation needed")
            return 0
    except Exception as exc:
        print(f"[LCD] Could not read {vsize_path}: {exc} → trying 90° rotation")
        return 90    # MPI3501 default: portrait panel


def _find_fb_device() -> str:
    """Auto-detect the framebuffer device for the MPI3501 SPI display.

    Strategy:
      1. Honour RJ_FB_DEVICE env var if set.
      2. Walk /sys/class/graphics/fb* and look for the FBTFT driver
         (name contains 'fb_ili9486', 'fb_ili9488', or 'flexfb').
      3. Fall back to /dev/fb0 (the default after LCD35-show runs).
    """
    if _FB_DEVICE:
        return _FB_DEVICE

    _FBTFT_TOKENS = ("ili9486", "ili9488", "flexfb", "fb_ili", "fbtft",
                     "tft35", "mpi3501", "waveshare", "spi", "hx8357")
    try:
        for entry in sorted(os.listdir("/sys/class/graphics/")):
            if not entry.startswith("fb"):
                continue
            name_path = f"/sys/class/graphics/{entry}/name"
            if os.path.isfile(name_path):
                with open(name_path) as f:
                    name = f.read().strip().lower()
                print(f"[LCD] Detected framebuffer /dev/{entry} → \"{name}\"")
                if any(tok in name for tok in _FBTFT_TOKENS):
                    return f"/dev/{entry}"
    except Exception:
        pass

    # Default: lcd-show makes the SPI LCD the primary fb0
    return "/dev/fb0"


# ---------------------------------------------------------------------------
# fbcp & console cleanup – claim the framebuffer exclusively
# ---------------------------------------------------------------------------
def _claim_framebuffer():
    """Stop everything else that writes to our framebuffer.

    Three things can fight with Raspyjack for the SPI LCD framebuffer:
      1. fbcp         – mirrors HDMI → SPI fb (already absent on this Pi)
      2. X11/Wayland  – the display manager (lightdm, gdm3)
      3. getty/console – the login prompt on /dev/tty1

    This function kills/disables all three so Raspyjack owns the fb.
    """
    import subprocess

    # 1) Kill fbcp
    try:
        subprocess.run(["killall", "-q", "fbcp"],
                       capture_output=True, timeout=3)
    except Exception:
        pass

    # 2) Stop the display manager (X11/Wayland desktop)
    for svc in ("lightdm", "gdm3", "sddm", "display-manager"):
        try:
            subprocess.run(["systemctl", "stop", svc],
                           capture_output=True, timeout=5)
        except Exception:
            pass

    # 3) Detach the Linux text console from this framebuffer.
    #    con2fbmap moves console 1 to a non-existent fb so it stops drawing.
    #    If that fails, we blank it with VT100 escape codes.
    console_detached = False
    try:
        # Move console 1 away from fb0 (use fb127 = doesn't exist = no output)
        subprocess.run(["con2fbmap", "1", "127"],
                       capture_output=True, timeout=3)
        console_detached = True
        print("[LCD] Detached text console from framebuffer (con2fbmap)")
    except Exception:
        pass

    # Blank & hide cursor on the consoles (belt-and-suspenders)
    for tty in ("/dev/tty0", "/dev/tty1"):
        try:
            with open(tty, "w") as f:
                f.write("\033[?25l")           # hide cursor
                f.write("\033[9;0]")           # setterm: screen blank now
                f.write("\033[2J\033[H")       # clear screen
        except Exception:
            pass

    # Also try setterm directly (more reliable on some kernels)
    try:
        subprocess.run(["setterm", "--blank", "force", "--cursor", "off"],
                       stdout=open("/dev/tty1", "w"),
                       stderr=subprocess.DEVNULL, timeout=3)
    except Exception:
        pass

    if not console_detached:
        # Last resort: stop getty on tty1 so it stops redrawing the login
        try:
            subprocess.run(["systemctl", "stop", "getty@tty1"],
                           capture_output=True, timeout=5)
            print("[LCD] Stopped getty@tty1")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# On-screen touch button bar
# ---------------------------------------------------------------------------
# Draws a row of 8 labelled buttons along the bottom 50 px of every frame.
# Disable with  RJ_TOUCH_BUTTONS=0  environment variable.
# ---------------------------------------------------------------------------
BUTTON_BAR_Y = 270                      # top edge of bar (320 - 50)
BUTTON_BAR_H = 50
_TOUCH_BUTTONS_ENABLED = os.environ.get("RJ_TOUCH_BUTTONS", "1") != "0"

BUTTON_BAR_LAYOUT = [
    # (label, x_start, x_end)
    ("K1",   0,  60),
    ("\u25C0",  60, 120),       # ◀
    ("\u25B2", 120, 180),       # ▲
    ("OK", 180, 240),
    ("\u25BC", 240, 300),       # ▼
    ("\u25B6", 300, 360),       # ▶
    ("K2", 360, 420),
    ("K3", 420, 480),
]

_button_bar_rgb  = None     # pre-rendered RGB strip (480 × BUTTON_BAR_H)
_button_bar_mask = None     # alpha mask for compositing


def _create_button_bar():
    """Pre-render the touch button bar (called once, then cached)."""
    global _button_bar_rgb, _button_bar_mask
    from PIL import Image as _Img, ImageDraw as _IDraw, ImageFont as _IFont

    bar = _Img.new("RGBA", (LCD_WIDTH, BUTTON_BAR_H), (0, 0, 0, 200))
    d = _IDraw.Draw(bar)

    try:
        font = _IFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
    except Exception:
        try:
            font = _IFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18)
        except Exception:
            font = _IFont.load_default()

    accent = (5, 255, 0, 230)       # Raspyjack green
    border_c = (5, 255, 0, 120)

    for label, x0, x1 in BUTTON_BAR_LAYOUT:
        # cell outline
        d.rectangle([(x0 + 2, 2), (x1 - 2, BUTTON_BAR_H - 2)],
                    outline=border_c, width=1)
        # centred label
        bbox = d.textbbox((0, 0), label, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        tx = x0 + (x1 - x0 - tw) // 2
        ty = (BUTTON_BAR_H - th) // 2
        d.text((tx, ty), label, fill=accent, font=font)

    _button_bar_rgb  = bar.convert("RGB")
    _button_bar_mask = bar.split()[3]            # alpha channel


def _pil_to_rgb565(image) -> bytes:
    """Convert a PIL RGB image to raw RGB565 bytes (little-endian)."""
    arr = np.asarray(image, dtype=np.uint16)
    r = (arr[:, :, 0] >> 3) & 0x1F
    g = (arr[:, :, 1] >> 2) & 0x3F
    b = (arr[:, :, 2] >> 3) & 0x1F
    rgb565 = (r << 11) | (g << 5) | b
    return rgb565.astype("<u2").tobytes()      # little-endian uint16


class LCD:
    """Drop-in replacement for LCD_1in44.LCD – same public API.
    Writes to the MPI3501 FBTFT framebuffer instead of issuing SPI commands.
    """

    show_touch_buttons = True   # set False to hide the on-screen button bar

    def __init__(self):
        self.width  = LCD_WIDTH
        self.height = LCD_HEIGHT

    # -- public API kept identical to LCD_1in44 ----------------------------

    def LCD_Init(self, scan_dir=SCAN_DIR_DFT):
        """Open the framebuffer device.  scan_dir is ignored (kernel handles it)."""
        global _fb_path, _fb_fd
        _fb_path = _find_fb_device()

        # Stop fbcp, desktop, and console – claim the framebuffer exclusively
        _claim_framebuffer()

        print(f"[LCD] Opening framebuffer: {_fb_path}")
        try:
            _fb_fd = open(_fb_path, "wb", buffering=0)
        except PermissionError:
            print(f"[LCD] Cannot open {_fb_path} – run as root or "
                  f"add your user to the 'video' group.")
            _fb_fd = None
        except FileNotFoundError:
            print(f"[LCD] Framebuffer {_fb_path} not found.  Has lcd-show been "
                  f"installed?  (sudo ./LCD35-show)")
            _fb_fd = None

        # Detect rotation needed for this panel
        global _fb_rotate
        _fb_rotate = _detect_fb_rotation(_fb_path)
        print(f"[LCD] Rotation: {_fb_rotate}°")
        return 0

    def LCD_Clear(self):
        """Fill the screen with white."""
        if _fb_fd is None:
            return
        from PIL import Image
        blank = Image.new("RGB", (self.width, self.height), "WHITE")
        self._write_fb(blank)

    def LCD_ShowImage(self, image, x=0, y=0):
        """Push *image* (PIL.Image, mode RGB) to the display."""
        if _fb_fd is None or image is None:
            return
        # Ensure correct size
        if image.size != (self.width, self.height):
            image = image.resize((self.width, self.height))
        if image.mode != "RGB":
            image = image.convert("RGB")

        # Composite the on-screen touch button bar (if enabled)
        if self.show_touch_buttons and _TOUCH_BUTTONS_ENABLED:
            image = self._composite_button_bar(image)

        self._write_fb(image)

        # Mirror the LCD frame for WebUI (throttled)
        if _FRAME_MIRROR_ENABLED:
            global _last_frame_save
            try:
                now = time.monotonic()
                if (now - _last_frame_save) >= _FRAME_MIRROR_INTERVAL:
                    image.save(_FRAME_MIRROR_PATH, "JPEG", quality=80)
                    _last_frame_save = now
            except Exception:
                pass

    # -- internal helpers --------------------------------------------------

    def _composite_button_bar(self, image):
        """Overlay the pre-rendered touch button bar at the bottom."""
        global _button_bar_rgb, _button_bar_mask
        if _button_bar_rgb is None:
            _create_button_bar()
        frame = image.copy()
        frame.paste(_button_bar_rgb, (0, BUTTON_BAR_Y), _button_bar_mask)
        return frame

    def _write_fb(self, image):
        """Rotate (if needed), convert to RGB565 and write to the framebuffer."""
        try:
            from PIL import Image as _Img
            # Rotate to match the physical panel orientation
            if _fb_rotate == 90:
                image = image.transpose(_Img.Transpose.ROTATE_270)
            elif _fb_rotate == 180:
                image = image.transpose(_Img.Transpose.ROTATE_180)
            elif _fb_rotate == 270:
                image = image.transpose(_Img.Transpose.ROTATE_90)
            raw = _pil_to_rgb565(image)
            _fb_fd.seek(0)
            _fb_fd.write(raw)
            _fb_fd.flush()
        except Exception as exc:
            print(f"[LCD] framebuffer write error: {exc}")
