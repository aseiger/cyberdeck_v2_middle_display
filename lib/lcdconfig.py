# /*****************************************************************************
# * | File        :	  epdconfig.py
# * | Author      :   Waveshare team
# * | Function    :   Hardware underlying interface
# * | Info        :
# *----------------
# * | This version:   V1.0
# * | Date        :   2019-06-21
# * | Info        :   
# ******************************************************************************
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documnetation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to  whom the Software is
# furished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS OR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#

import os
import subprocess
import sys
import time
import spidev
import logging
import numpy as np
from gpiozero import *

logger = logging.getLogger(__name__)


class HardwarePWM:
    """Flicker-free backlight control via the kernel's hardware PWM (sysfs).

    Works on both Pi 4 and Pi 5.  On Pi 5 the pin mux is set automatically
    via ``pinctrl`` so no device-tree overlay is strictly required (though
    adding ``dtoverlay=pwm`` to /boot/firmware/config.txt is still
    recommended for a clean boot-time setup).
    """

    # BCM GPIOs that have a hardware PWM alternate function.
    # Pi 4 (BCM2711) has 2 PWM channels shared across 4 GPIOs:
    _GPIO_TO_CHANNEL_PI4 = {12: 0, 13: 1, 18: 0, 19: 1}
    # Pi 5 (RP1) has 4 independent PWM channels:
    _GPIO_TO_CHANNEL_PI5 = {12: 0, 13: 1, 18: 2, 19: 3}

    # Pin-mux alt-function index needed for PWM on each GPIO (Pi 5 / RP1).
    _GPIO_TO_ALT_PI5 = {12: 0, 13: 0, 18: 3, 19: 3}

    # RP1 (Pi 5) base addresses for PWM0 and PWM1.
    _RP1_PWM0_ADDR = "98000"   # pwm@98000 — channels 0-3 for GPIO 12/13/18/19
    _RP1_PWM1_ADDR = "9c000"   # pwm@9c000 — used internally (fan, etc.)

    # Which RP1 PWM *block* each GPIO lives on (always PWM0 for user GPIOs).
    _GPIO_TO_RP1_BLOCK = {12: _RP1_PWM0_ADDR, 13: _RP1_PWM0_ADDR,
                          18: _RP1_PWM0_ADDR, 19: _RP1_PWM0_ADDR}

    @staticmethod
    def _is_pi5():
        """Detect Raspberry Pi 5 by checking /proc/device-tree/model."""
        try:
            with open("/proc/device-tree/model") as f:
                return "Pi 5" in f.read()
        except OSError:
            return False

    def __init__(self, gpio_pin, frequency=1000):
        self._pi5 = self._is_pi5()
        mapping = self._GPIO_TO_CHANNEL_PI5 if self._pi5 else self._GPIO_TO_CHANNEL_PI4
        channel = mapping.get(gpio_pin)
        if channel is None:
            raise ValueError(f"GPIO {gpio_pin} does not support hardware PWM")

        self._gpio_pin = gpio_pin
        self._channel = channel

        # On Pi 5 the PWM0 block (for user GPIOs) requires the 'pwm' overlay.
        # Try to load it at runtime if the chip isn't present yet.
        if self._pi5:
            self._ensure_pwm0_overlay()

        self._chip = self._find_chip(channel, self._pi5,
                                     self._GPIO_TO_RP1_BLOCK.get(gpio_pin))
        self._base = os.path.join(self._chip, f"pwm{channel}")
        self._frequency = frequency
        self._duty_frac = 0.0

        # Export the channel if the sysfs directory doesn't exist yet
        if not os.path.isdir(self._base):
            self._chip_write("export", str(channel))
            for _ in range(50):                       # wait for udev
                if os.path.isdir(self._base):
                    break
                time.sleep(0.02)
            else:
                raise OSError(f"Timeout waiting for {self._base}")

        self._period_ns = int(1_000_000_000 / frequency)
        self._write("duty_cycle", "0")
        self._write("period", str(self._period_ns))
        self._write("enable", "1")

        # On Pi 5 the pin mux must be set to the correct alt function
        # so the PWM peripheral is actually routed to the physical pin.
        if self._pi5:
            self._set_pinmux_pi5(gpio_pin)

        logger.info("Using hardware PWM on GPIO %d (%s channel %d, %d Hz)",
                     gpio_pin, os.path.basename(self._chip), channel, frequency)

    # ── chip discovery ───────────────────────────────────────────────

    @classmethod
    def _ensure_pwm0_overlay(cls):
        """Load ``dtoverlay=pwm`` at runtime if PWM0 chip is not yet visible."""
        pwm_root = "/sys/class/pwm"
        for entry in os.listdir(pwm_root):
            if not entry.startswith("pwmchip"):
                continue
            device = os.path.realpath(os.path.join(pwm_root, entry, "device"))
            if cls._RP1_PWM0_ADDR in device:
                return                              # already present
        # PWM0 not found — try loading the overlay
        try:
            subprocess.check_call(["dtoverlay", "pwm"], timeout=5)
            time.sleep(0.3)                         # wait for the chip to appear
            logger.info("Loaded dtoverlay=pwm at runtime for PWM0")
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            raise OSError(
                "PWM0 chip not found and could not load dtoverlay=pwm. "
                "Add 'dtoverlay=pwm' to /boot/firmware/config.txt and reboot."
            ) from exc

    @staticmethod
    def _find_chip(channel, is_pi5=False, required_addr=None):
        """Find the sysfs pwmchipN path that owns *channel*.

        On Pi 5 there are two PWM blocks (PWM0 at 98000 and PWM1 at 9c000).
        *required_addr* selects the correct one by matching the device path.
        """
        pwm_root = "/sys/class/pwm"
        if not os.path.isdir(pwm_root):
            raise FileNotFoundError("No /sys/class/pwm — enable the PWM overlay")
        for entry in sorted(os.listdir(pwm_root)):
            if not entry.startswith("pwmchip"):
                continue
            chip = os.path.join(pwm_root, entry)
            # On Pi 5, filter by device address to pick the right PWM block
            if is_pi5 and required_addr:
                device = os.path.realpath(os.path.join(chip, "device"))
                if required_addr not in device:
                    continue
            try:
                with open(os.path.join(chip, "npwm")) as f:
                    if channel < int(f.read().strip()):
                        return chip
            except (OSError, ValueError):
                continue
        raise FileNotFoundError(
            f"No PWM chip found for channel {channel}. "
            "Add 'dtoverlay=pwm' to /boot/firmware/config.txt and reboot."
        )

    # ── sysfs helpers ────────────────────────────────────────────────

    def _write(self, attr, val):
        with open(os.path.join(self._base, attr), "w") as f:
            f.write(str(val))

    def _chip_write(self, attr, val):
        with open(os.path.join(self._chip, attr), "w") as f:
            f.write(str(val))

    # ── Pi 5 pin-mux ────────────────────────────────────────────────

    @classmethod
    def _set_pinmux_pi5(cls, gpio_pin):
        """Use ``pinctrl`` to route the PWM peripheral to the physical pin.

        On Pi 5 the sysfs PWM interface controls the RP1 PWM block, but the
        GPIO must also be switched to the correct alt-function for the signal
        to actually reach the pin.
        """
        alt = cls._GPIO_TO_ALT_PI5.get(gpio_pin)
        if alt is None:
            return
        try:
            subprocess.check_call(
                ["pinctrl", "set", str(gpio_pin), f"a{alt}"],
                timeout=5,
            )
            logger.debug("pinctrl: GPIO %d -> a%d (PWM)", gpio_pin, alt)
        except FileNotFoundError:
            logger.warning("'pinctrl' not found — cannot set pin mux; "
                           "add 'dtoverlay=pwm' to config.txt instead")
        except subprocess.CalledProcessError as exc:
            logger.warning("pinctrl failed for GPIO %d: %s", gpio_pin, exc)

    # ── public interface (drop-in for gpiozero.PWMOutputDevice) ──────

    @property
    def value(self):
        return self._duty_frac

    @value.setter
    def value(self, v):
        self._duty_frac = max(0.0, min(1.0, float(v)))
        self._write("duty_cycle", int(self._period_ns * self._duty_frac))

    @property
    def frequency(self):
        return self._frequency

    @frequency.setter
    def frequency(self, freq):
        self._frequency = freq
        new_period = int(1_000_000_000 / freq)
        # duty_cycle must be ≤ period, so zero it before shrinking
        self._write("duty_cycle", "0")
        self._period_ns = new_period
        self._write("period", str(self._period_ns))
        self._write("duty_cycle", int(self._period_ns * self._duty_frac))

    def close(self):
        try:
            self._write("duty_cycle", "0")
            self._write("enable", "0")
        except OSError:
            pass


class RaspberryPi:
    def __init__(self,spi=spidev.SpiDev(0,0),spi_freq=40000000,rst = 27,dc = 25,bl = 18,bl_freq=5000,i2c=None,i2c_freq=100000):
        self.np=np
        self.INPUT = False
        self.OUTPUT = True

        self.SPEED  =spi_freq
        self.BL_freq=bl_freq

        self.RST_PIN= self.gpio_mode(rst,self.OUTPUT)
        self.DC_PIN = self.gpio_mode(dc,self.OUTPUT)
        self.BL_PIN = self.gpio_pwm(bl)
        self.bl_DutyCycle(0)
        
        #Initialize SPI
        self.SPI = spi
        if self.SPI!=None :
            self.SPI.max_speed_hz = spi_freq
            self.SPI.mode = 0b00

    def gpio_mode(self,Pin,Mode,pull_up = None,active_state = True):
        if Mode:
            return DigitalOutputDevice(Pin,active_high = True,initial_value =False)
        else:
            return DigitalInputDevice(Pin,pull_up=pull_up,active_state=active_state)

    def digital_write(self, Pin, value):
        if value:
            Pin.on()
        else:
            Pin.off()

    def digital_read(self, Pin):
        return Pin.value

    def delay_ms(self, delaytime):
        time.sleep(delaytime / 1000.0)

    def gpio_pwm(self, Pin):
        # Prefer hardware PWM (flicker-free); fall back to software PWM
        try:
            return HardwarePWM(Pin, frequency=self.BL_freq)
        except (ValueError, OSError, FileNotFoundError) as exc:
            logger.warning(
                "Hardware PWM unavailable on GPIO %d: %s  — "
                "falling back to software PWM at %d Hz. "
                "To eliminate backlight flicker, add 'dtoverlay=pwm' "
                "to /boot/firmware/config.txt and reboot.",
                Pin, exc, self.BL_freq,
            )
            return PWMOutputDevice(Pin, frequency=self.BL_freq)

    def spi_writebyte(self, data):
        if self.SPI!=None :
            self.SPI.writebytes(data)

    def bl_DutyCycle(self, duty):
        self.BL_PIN.value = duty / 100
        
    def bl_Frequency(self,freq):# Hz
        self.BL_PIN.frequency = freq
           
    def module_init(self):
        if self.SPI!=None :
            self.SPI.max_speed_hz = self.SPEED        
            self.SPI.mode = 0b00     
        return 0

    def module_exit(self):
        logging.debug("[SHUTDOWN] spi end")
        if self.SPI!=None :
            self.SPI.close()
        logging.debug("[SHUTDOWN] spi closed")
        logging.debug("[SHUTDOWN] gpio cleanup...")
        self.digital_write(self.RST_PIN, 1)
        logging.debug("[SHUTDOWN] rst written")
        self.digital_write(self.DC_PIN, 0)   
        logging.debug("[SHUTDOWN] dc written")
        self.BL_PIN.close()
        logging.debug("[SHUTDOWN] bl closed")
        time.sleep(0.001)
        logging.debug("[SHUTDOWN] module_exit done")



'''
if os.path.exists('/sys/bus/platform/drivers/gpiomem-bcm2835'):
    implementation = RaspberryPi()

for func in [x for x in dir(implementation) if not x.startswith('_')]:
    setattr(sys.modules[__name__], func, getattr(implementation, func))
'''

### END OF FILE ###
