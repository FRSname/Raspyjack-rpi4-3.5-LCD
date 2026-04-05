"""
Shared input helper for RaspyJack payloads.
Checks touch/WebUI virtual input first, then falls back to GPIO.

On MPI3501 displays there are no physical buttons – all input comes
from the touch screen via rj_input (evdev).  GPIO polling is wrapped
in try/except so payloads don't crash when pins aren't available.
"""

import os
import sys

# Ensure RaspyJack root is on PYTHONPATH (for rj_input etc.)
_rj_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _rj_root not in sys.path:
    sys.path.insert(0, _rj_root)

try:
    import rj_input
    # Ensure touch + WS listeners are running in this process
    rj_input._ensure_started()
except Exception:
    rj_input = None

# ---------------------------------------------------------------------------
# Make RPi.GPIO safe on MPI3501 (no physical buttons)
# ---------------------------------------------------------------------------
# Monkey-patch GPIO.setup() and GPIO.input() so payloads that call them
# directly don't crash.  GPIO.input() always returns 1 (not pressed) so
# payloads fall through to get_button() → touch input.
# ---------------------------------------------------------------------------
_gpio_available = False
try:
    import RPi.GPIO as _GPIO
    _GPIO.setmode(_GPIO.BCM)
    # Quick test: try to read a common pin
    _GPIO.setup(16, _GPIO.IN, pull_up_down=_GPIO.PUD_UP)
    _GPIO.input(16)
    _gpio_available = True
except Exception:
    pass

if not _gpio_available:
    try:
        import RPi.GPIO as _GPIO
        _orig_setup = _GPIO.setup
        _orig_input = _GPIO.input

        def _safe_setup(*a, **kw):
            try:
                _orig_setup(*a, **kw)
            except Exception:
                pass

        def _safe_input(*a, **kw):
            try:
                return _orig_input(*a, **kw)
            except Exception:
                return 1   # 1 = not pressed

        _GPIO.setup = _safe_setup
        _GPIO.input = _safe_input
    except Exception:
        pass

_VIRTUAL_TO_BTN = {
    "KEY_UP_PIN": "UP",
    "KEY_DOWN_PIN": "DOWN",
    "KEY_LEFT_PIN": "LEFT",
    "KEY_RIGHT_PIN": "RIGHT",
    "KEY_PRESS_PIN": "OK",
    "KEY1_PIN": "KEY1",
    "KEY2_PIN": "KEY2",
    "KEY3_PIN": "KEY3",
}


def get_virtual_button():
    """Return a WebUI virtual button name or None."""
    if rj_input is None:
        return None
    try:
        name = rj_input.get_virtual_button()
    except Exception:
        return None
    if not name:
        return None
    return _VIRTUAL_TO_BTN.get(name)


def safe_gpio_setup(pins, gpio):
    """Set up GPIO button pins, ignoring errors on MPI3501 (no buttons)."""
    try:
        gpio.setmode(gpio.BCM)
    except Exception:
        pass
    for pin in pins.values():
        try:
            gpio.setup(pin, gpio.IN, pull_up_down=gpio.PUD_UP)
        except Exception:
            pass


def get_button(pins, gpio):
    """
    Return a button name using touch/WebUI virtual input if available,
    otherwise fall back to GPIO.
    """
    mapped = get_virtual_button()
    if mapped:
        return mapped
    for btn, pin in pins.items():
        try:
            if gpio.input(pin) == 0:
                return btn
        except Exception:
            pass
    return None
