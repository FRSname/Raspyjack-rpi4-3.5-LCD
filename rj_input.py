#!/usr/bin/env python3
"""
RaspyJack input bridge – evdev touch + WebSocket virtual buttons
-----------------------------------------------------------------
Reads touch input from the kernel evdev device created by the MPI3501
(ads7846 / XPT2046) overlay, and maps touch coordinates to 8 logical
button zones on the 480×320 display.  Also keeps the WebSocket
virtual-button listener for the WebUI.

Touch driver:
  The goodtft/lcd-show installer adds a Device Tree overlay that registers
  the XPT2046 as an input device (typically /dev/input/event0).  This module
  uses python3-evdev to read ABS_X, ABS_Y and BTN_TOUCH events.

Touch input
-----------
Two-tier touch zones:

1. **Button bar** (bottom 50 px, y 270-320) – 8 visible labelled buttons
   always drawn on screen by LCD_480x320.  These have priority.

   ┌────┬────┬────┬────┬────┬────┬────┬────┐
   │ K1 │ ◀  │ ▲  │ OK │ ▼  │ ▶  │ K2 │ K3 │  (each 60 × 50 px)
   └────┴────┴────┴────┴────┴────┴────┴────┘

2. **Content area** (upper 270 px) – invisible zones for quick-gesture
   navigation (same as before, slightly adjusted to avoid overlap).

Public API (unchanged):
  get_virtual_button() -> str | None
  restart_listener()
"""

import os, json, threading, socket, queue, atexit, time
from typing import Optional

# Screen dimensions (must match LCD_480x320 / LCD_Config)
SCREEN_W = 480
SCREEN_H = 320

# ── Button bar zones (bottom 50 px – visible on-screen buttons) ──────────
BUTTON_BAR_Y = 270
_BUTTON_BAR_ZONES = {
    "KEY1_PIN":      (  0, BUTTON_BAR_Y,  60, 320),
    "KEY_LEFT_PIN":  ( 60, BUTTON_BAR_Y, 120, 320),
    "KEY_UP_PIN":    (120, BUTTON_BAR_Y, 180, 320),
    "KEY_PRESS_PIN": (180, BUTTON_BAR_Y, 240, 320),
    "KEY_DOWN_PIN":  (240, BUTTON_BAR_Y, 300, 320),
    "KEY_RIGHT_PIN": (300, BUTTON_BAR_Y, 360, 320),
    "KEY2_PIN":      (360, BUTTON_BAR_Y, 420, 320),
    "KEY3_PIN":      (420, BUTTON_BAR_Y, 480, 320),
}

# ── Content-area zones (upper 270 px – invisible gesture regions) ────────
_CONTENT_ZONES = {
    "KEY_UP_PIN":    (160,   0, 320,  70),   # top-centre
    "KEY_DOWN_PIN":  (160, 200, 320, 270),   # bottom-centre
    "KEY_LEFT_PIN":  (  0,  70, 120, 200),   # left edge
    "KEY_RIGHT_PIN": (360,  70, 480, 200),   # right edge
    "KEY_PRESS_PIN": (160,  70, 320, 200),   # centre (OK / select)
    "KEY1_PIN":      (  0,   0, 160,  70),   # top-left
    "KEY2_PIN":      (320,   0, 480,  70),   # top-right
    "KEY3_PIN":      (  0, 200, 160, 270),   # bottom-left
}

# ── Debounce ─────────────────────────────────────────────────────────────
_TOUCH_DEBOUNCE = 0.15   # seconds – ignore rapid re-touches

# ── Touch calibration ─────────────────────────────────────────────────────
# LCD35-show writes an X11 calibration file with SwapAxes and Calibration
# values.  X11/libinput uses that automatically – but since we read evdev
# directly, we must apply the same transforms ourselves.
#
# MPI3501 defaults (used when no calibration file is found):
#   SwapAxes = 1          (raw X/Y are swapped relative to screen)
#   Calibration = 3936 227 268 3880
#     → screen-X maps from raw 3936 (left) to 227 (right)  – INVERTED
#     → screen-Y maps from raw 268 (top) to 3880 (bottom)  – normal
#
# Override everything:  RJ_TOUCH_SWAP_XY=1  RJ_TOUCH_INVERT_X=1
#                       RJ_TOUCH_CAL=3936,227,268,3880
# Debug:               RJ_TOUCH_DEBUG=1
# ──────────────────────────────────────────────────────────────────────────

# MPI3501 / LCD35-show defaults (used when no calibration file is found)
_touch_swap_xy:  bool  = True
_touch_invert_x: bool  = False     # derived from cal values automatically
_touch_invert_y: bool  = False
_touch_cal_x:    tuple = (3936, 227)    # (left-edge raw, right-edge raw)
_touch_cal_y:    tuple = (268, 3880)    # (top-edge raw,  bottom-edge raw)
_touch_cal_loaded: bool = False
_touch_debug:    bool  = os.environ.get("RJ_TOUCH_DEBUG", "0") == "1"
_touch_debug_count: int = 0


def _load_touch_calibration() -> None:
    """Read LCD35-show's X11 calibration so touch matches the desktop."""
    global _touch_swap_xy, _touch_cal_x, _touch_cal_y, _touch_cal_loaded
    if _touch_cal_loaded:
        return
    _touch_cal_loaded = True
    import re

    # ── env-var overrides (highest priority) ─────────────────────
    env_swap = os.environ.get("RJ_TOUCH_SWAP_XY")
    if env_swap is not None:
        _touch_swap_xy = env_swap == "1"

    env_cal = os.environ.get("RJ_TOUCH_CAL")
    if env_cal:
        try:
            parts = [int(v) for v in env_cal.split(",")]
            if len(parts) == 4:
                _touch_cal_x = (parts[0], parts[1])
                _touch_cal_y = (parts[2], parts[3])
                print(f"[rj_input] Calibration (env): swap={_touch_swap_xy} "
                      f"X={_touch_cal_x} Y={_touch_cal_y}")
                return
        except Exception:
            pass

    # ── auto-detect from LCD35-show config ───────────────────────
    _CAL_PATHS = (
        "/usr/share/X11/xorg.conf.d/99-calibration.conf",
        "/etc/X11/xorg.conf.d/99-calibration.conf",
        "/usr/share/X11/xorg.conf.d/99-fbturbo.conf",
        "/etc/X11/xorg.conf.d/40-libinput.conf",
    )
    for cfg in _CAL_PATHS:
        try:
            with open(cfg) as fh:
                text = fh.read()
        except FileNotFoundError:
            continue

        m = re.search(r'Option\s+"SwapAxes"\s+"(\d)"', text, re.I)
        if m and env_swap is None:
            _touch_swap_xy = m.group(1) == "1"

        m = re.search(
            r'Option\s+"Calibration"\s+"(\d+)\s+(\d+)\s+(\d+)\s+(\d+)"', text)
        if m:
            _touch_cal_x = (int(m.group(1)), int(m.group(2)))
            _touch_cal_y = (int(m.group(3)), int(m.group(4)))

        print(f"[rj_input] Calibration ({cfg}):\n"
              f"           swap={_touch_swap_xy}  X={_touch_cal_x}  Y={_touch_cal_y}")
        return

    print(f"[rj_input] No calibration file found – using MPI3501 defaults:\n"
          f"           swap={_touch_swap_xy}  X={_touch_cal_x}  Y={_touch_cal_y}")


# ═══════════════════════════════════════════════════════════════════════════
# WebSocket virtual-button listener (unchanged from original rj_input.py)
# ═══════════════════════════════════════════════════════════════════════════
_SOCK_PATH = os.environ.get("RJ_INPUT_SOCK", "/dev/shm/rj_input.sock")

_BTN_MAP = {
    "UP": "KEY_UP_PIN",
    "DOWN": "KEY_DOWN_PIN",
    "LEFT": "KEY_LEFT_PIN",
    "RIGHT": "KEY_RIGHT_PIN",
    "OK": "KEY_PRESS_PIN",
    "KEY1": "KEY1_PIN",
    "KEY2": "KEY2_PIN",
    "KEY3": "KEY3_PIN",
}

_q: "queue.Queue[str]" = queue.Queue()
_sock: Optional[socket.socket] = None
_ws_thread: Optional[threading.Thread] = None
_touch_thread: Optional[threading.Thread] = None
_touch_dev = None          # evdev.InputDevice – kept for ungrab on stop
_stop_event = threading.Event()


# ── WebSocket listener ────────────────────────────────────────────────────

def _ws_cleanup():
    global _sock
    try:
        if _sock is not None:
            _sock.close()
    except Exception:
        pass
    try:
        if os.path.exists(_SOCK_PATH):
            os.unlink(_SOCK_PATH)
    except Exception:
        pass
    _sock = None


def _ws_listen():
    global _sock
    try:
        if os.path.exists(_SOCK_PATH):
            os.unlink(_SOCK_PATH)
    except Exception:
        pass

    _sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    _sock.bind(_SOCK_PATH)
    try:
        os.chmod(_SOCK_PATH, 0o666)
    except Exception:
        pass

    while True:
        try:
            data, _addr = _sock.recvfrom(4096)
        except Exception:
            break
        try:
            msg = json.loads(data.decode("utf-8", "ignore"))
        except Exception:
            continue
        if msg.get("type") != "input":
            continue
        button = str(msg.get("button", ""))
        state  = str(msg.get("state", ""))
        if state != "press":
            continue
        mapped = _BTN_MAP.get(button)
        if mapped:
            try:
                _q.put_nowait(mapped)
            except Exception:
                pass


# ── evdev touch listener ─────────────────────────────────────────────────

def _find_touch_device():
    """Find the evdev input device for the XPT2046 / ADS7846 touch panel."""
    try:
        import evdev
    except ImportError:
        print("[rj_input] python3-evdev not installed – touch disabled")
        return None

    for path in sorted(evdev.list_devices()):
        dev = evdev.InputDevice(path)
        name_lower = dev.name.lower()
        # The kernel driver registers as "ADS7846 Touchscreen" or similar
        if any(tok in name_lower for tok in ("ads7846", "xpt2046", "touch")):
            print(f"[rj_input] Using touch device: {dev.path}  ({dev.name})")
            return dev
        dev.close()

    print("[rj_input] No touch device found – touch disabled")
    return None


def _zone_for_pixel(px: int, py: int) -> Optional[str]:
    """Return the logical button name for the zone at (px, py), or None.
    Button-bar zones (visible) are checked first."""
    for name, (x0, y0, x1, y1) in _BUTTON_BAR_ZONES.items():
        if x0 <= px < x1 and y0 <= py < y1:
            return name
    for name, (x0, y0, x1, y1) in _CONTENT_ZONES.items():
        if x0 <= px < x1 and y0 <= py < y1:
            return name
    return None


def _touch_listen():
    """Background thread: read evdev touch events and enqueue button presses."""
    try:
        import evdev
        from evdev import ecodes
    except ImportError:
        return

    global _touch_dev
    _stop_event.clear()
    dev = _find_touch_device()
    if dev is None:
        return
    _touch_dev = dev

    # Grab the device exclusively so X11 / libinput can't also read it.
    # This prevents touch events from leaking to the desktop.
    try:
        dev.grab()
        print(f"[rj_input] Grabbed {dev.path} exclusively")
    except Exception as exc:
        print(f"[rj_input] Could not grab {dev.path}: {exc}  (touch may leak to X11)")

    # Load calibration (swap + axis range) from LCD35-show's X11 config
    _load_touch_calibration()

    cur_x = 0
    cur_y = 0
    touching = False
    last_btn  = None
    last_time = 0.0
    global _touch_debug_count

    try:
        for event in dev.read_loop():
            if event.type == ecodes.EV_ABS:
                if event.code == ecodes.ABS_X:
                    cur_x = event.value
                elif event.code == ecodes.ABS_Y:
                    cur_y = event.value

            elif event.type == ecodes.EV_KEY:
                if event.code == ecodes.BTN_TOUCH:
                    touching = (event.value == 1)
                    if not touching:
                        last_btn = None

            elif event.type == ecodes.EV_SYN and touching:
                # Apply calibration: swap axes, then map raw → screen pixels
                if _touch_swap_xy:
                    rx, ry = cur_y, cur_x
                else:
                    rx, ry = cur_x, cur_y

                # Map raw ADC range → 0.0 .. 1.0 (handles inverted ranges)
                x_range = _touch_cal_x[1] - _touch_cal_x[0]
                y_range = _touch_cal_y[1] - _touch_cal_y[0]
                if x_range == 0:
                    x_range = 1
                if y_range == 0:
                    y_range = 1
                fx = (rx - _touch_cal_x[0]) / x_range
                fy = (ry - _touch_cal_y[0]) / y_range
                # Clamp to 0..1
                fx = max(0.0, min(1.0, fx))
                fy = max(0.0, min(1.0, fy))
                px = int(fx * (SCREEN_W - 1))
                py = int(fy * (SCREEN_H - 1))

                # Debug: print first 20 touches so we can diagnose issues
                if _touch_debug or _touch_debug_count < 20:
                    btn_preview = _zone_for_pixel(px, py) or "???"
                    print(f"[touch] raw=({cur_x},{cur_y}) → swap→({rx},{ry}) "
                          f"→ px=({px},{py}) → {btn_preview}")

                btn = _zone_for_pixel(px, py)
                now = time.monotonic()
                if btn and (btn != last_btn or (now - last_time) >= _TOUCH_DEBOUNCE):
                    try:
                        _q.put_nowait(btn)
                        _touch_debug_count += 1
                    except Exception:
                        pass
                    last_btn  = btn
                    last_time = now

    except Exception as exc:
        print(f"[rj_input] Touch read loop ended: {exc}")
    finally:
        try:
            dev.ungrab()
        except Exception:
            pass


# ── Public API ────────────────────────────────────────────────────────────

def get_virtual_button() -> Optional[str]:
    """Return next virtual button name (e.g. 'KEY_LEFT_PIN') or None."""
    try:
        return _q.get_nowait()
    except queue.Empty:
        return None


def _ensure_started():
    global _ws_thread, _touch_thread
    if _ws_thread is None or not _ws_thread.is_alive():
        _ws_thread = threading.Thread(target=_ws_listen, daemon=True)
        _ws_thread.start()
    if _touch_thread is None or not _touch_thread.is_alive():
        _touch_thread = threading.Thread(target=_touch_listen, daemon=True)
        _touch_thread.start()


def stop_listener():
    """Stop touch & WS listeners and release the touch device.

    Call this before launching a payload subprocess so it can grab
    the evdev touch device itself.
    """
    global _touch_dev, _touch_thread, _ws_thread
    _stop_event.set()
    # Ungrab so the child process can grab
    if _touch_dev is not None:
        try:
            _touch_dev.ungrab()
            print("[rj_input] Ungrabbed touch device")
        except Exception:
            pass
        try:
            _touch_dev.close()
        except Exception:
            pass
        _touch_dev = None
    _ws_cleanup()
    _touch_thread = None
    _ws_thread = None
    # Drain the queue
    while not _q.empty():
        try:
            _q.get_nowait()
        except Exception:
            break


def restart_listener():
    """Recreate both the WebSocket and touch listeners."""
    global _ws_thread, _touch_thread
    _ws_cleanup()
    _ws_thread = None
    _touch_thread = None
    _ensure_started()


# Start on import and register cleanup
_ensure_started()
atexit.register(_ws_cleanup)
