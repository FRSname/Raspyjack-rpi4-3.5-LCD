 # -*- coding:UTF-8 -*-
 ##
 # | file      	:	LCD_1IN44.py
 # | note       :   COMPATIBILITY SHIM – forwards everything to LCD_480x320.py
 # |                so that payloads/modules that still `import LCD_1in44` keep
 # |                working on the MPI3501 480×320 display.
 #

from LCD_480x320 import *          # LCD class, SCAN_DIR_DFT, LCD_WIDTH, LCD_HEIGHT
from LCD_480x320 import LCD        # explicit re-export for `LCD_1in44.LCD()`