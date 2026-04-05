##
 #  @filename   :   LCD_Config.py
 #  @brief      :   LCD hardware configuration for 480×320 MPI3501
 #
 # The MPI3501 display is driven by the FBTFT kernel driver installed via
 # goodtft/lcd-show (LCD35-show).  The kernel handles all SPI communication
 # and creates a framebuffer device (/dev/fb0 or /dev/fb1).
 #
 # This file is kept for backward compatibility.  LCD_480x320.py and many
 # payloads import LCD_Config for the dimension constants and legacy helpers.
 #

import time

# ── Display geometry ──────────────────────────────────────────────────────
LCD_WIDTH   = 480
LCD_HEIGHT  = 320

# ── Pin definitions (for reference – the kernel driver handles these) ────
# These are the standard MPI3501 / Waveshare 3.5" wiring.
# They are NOT used in software – the FBTFT overlay configures them.
LCD_RST_PIN = 25        # Reset
LCD_DC_PIN  = 24        # Data / Command
LCD_CS_PIN  = 8         # CE0 – display chip select
LCD_BL_PIN  = 18        # Backlight (GPIO18 / PWM0 on MPI3501)

# Touch controller is handled by the kernel (ads7846 / XPT2046 overlay)
# The evdev interface is used in rj_input.py – no manual SPI needed.
TOUCH_CS_PIN  = 7       # CE1 – touch chip select (kernel-managed)
TOUCH_IRQ_PIN = 17      # Touch IRQ             (kernel-managed)

# ── Legacy helpers ────────────────────────────────────────────────────────
# These are no-ops so that existing callers (payloads etc.) don't break.
# The FBTFT kernel driver owns the hardware – userspace doesn't touch it.

GPIO = None
SPI  = None

try:
    import RPi.GPIO as _GPIO
    GPIO = _GPIO
except Exception:
    pass


def epd_digital_write(pin, value):
    """No-op – kernel handles GPIO for the MPI3501."""
    pass


def Driver_Delay_ms(xms):
    time.sleep(xms / 1000.0)


def SPI_Write_Byte(data):
    """No-op – kernel handles SPI for the MPI3501."""
    pass


def GPIO_Init():
    """No-op – kernel handles GPIO for the MPI3501.
    Returns 0 for backward compatibility with payloads that check the result.
    """
    return 0

### END OF FILE ###