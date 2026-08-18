#!/usr/bin/python
# -*- coding: UTF-8 -*-
#import chardet
import os
import sys 
import time
import datetime
import subprocess
import logging
import math
import signal
import atexit
import spidev as SPI
sys.path.append(".")
from lib import LCD_2inch4
from PIL import Image,ImageChops,ImageDraw,ImageEnhance,ImageFont
from gpiozero import Button
from lib.lcdconfig import HardwarePWM
import glob
import json
import systemStats
import batteryStats
import gpsStats
import display_server
from pixfluid import PixFluid

def RedGreenColorScale(value : float, invert : bool = False):
    if (value > 100): value = 100
    if (value < 0): value = 0

    highValue = 255 * (value / 100)
    lowValue = 255 - (255 * (value / 100))

    if (invert): return (int(highValue), int(lowValue), 0)
    else: return (int(lowValue), int(highValue), 0)


def SOCColor(pct: float):
    if pct >= 25:
        return "GREEN"
    ratio = max(0.0, pct) / 25.0
    return (int(255 * (1.0 - ratio)), int(255 * ratio), 0)


def ChangedTileRegions(previous_image, current_image, tile_size=16):
    difference = ImageChops.difference(previous_image, current_image)
    width, height = current_image.size

    for top in range(0, height, tile_size):
        bottom = min(top + tile_size, height)
        run_left = None

        for left in range(0, width, tile_size):
            right = min(left + tile_size, width)
            changed = difference.crop((left, top, right, bottom)).getbbox() is not None
            if changed and run_left is None:
                run_left = left
            elif not changed and run_left is not None:
                yield (run_left, top, left, bottom)
                run_left = None

        if run_left is not None:
            yield (run_left, top, width, bottom)


def ShowImageRegion(display, image, x_start, y_start):
    width, height = image.size
    if width <= 0 or height <= 0:
        return
    if x_start < 0 or y_start < 0 or x_start + width > display.width or y_start + height > display.height:
        raise ValueError("Image region is outside the display bounds")

    rgb = display.np.asarray(image.convert("RGB"))
    pixels = display.np.zeros((height, width, 2), dtype=display.np.uint8)
    pixels[..., [0]] = display.np.add(
        display.np.bitwise_and(rgb[..., [0]], 0xF8),
        display.np.right_shift(rgb[..., [1]], 5),
    )
    pixels[..., [1]] = display.np.add(
        display.np.bitwise_and(display.np.left_shift(rgb[..., [1]], 3), 0xE0),
        display.np.right_shift(rgb[..., [2]], 3),
    )

    buffer = pixels.flatten().tolist()
    display.command(0x36)
    display.data(0x08)
    display.SetWindows(x_start, y_start, x_start + width, y_start + height)
    display.digital_write(display.DC_PIN, True)
    for offset in range(0, len(buffer), 4096):
        display.spi_writebyte(buffer[offset:offset + 4096])


class FanTachometer:
    """Counts tach pulses on a GPIO pin and reports RPM per polling window."""

    def __init__(self, pin, pulses_per_rev=2):
        self._pulses_per_rev = pulses_per_rev
        self._count = 0
        self._last_time = time.monotonic()
        self._input = Button(pin, pull_up=True)
        self._input.when_pressed = self._on_pulse

    def _on_pulse(self):
        self._count += 1

    def read_rpm(self):
        now = time.monotonic()
        dt = now - self._last_time
        pulses = self._count
        self._count = 0
        self._last_time = now
        if dt <= 0:
            return 0.0
        return pulses / self._pulses_per_rev / dt * 60.0


# Raspberry Pi pin configuration:
RST = 27
DC = 25
BL = 18
bus = 0 
device = 0 
CASE_FAN = 13
FAN_TACH = 17                      # BCM pin wired to the aux fan tach output (found via detect_tach.py)
AUX_FAN_PULSES_PER_REV = 2
CPU_FAN_PWM_GLOB = "/sys/devices/platform/cooling_fan/hwmon/*/pwm1"
CPU_FAN_RPM_GLOB = "/sys/devices/platform/cooling_fan/hwmon/*/fan1_input"
# --- Aux fan control ---
AUX_FAN_CURVE_FILE = os.path.join(os.path.dirname(__file__), "aux_fan_curve.json")
AUX_FAN_TARGET_RPM_RATIO = 1.0     # aux target RPM = CPU fan RPM * this ratio
AUX_FAN_SMOOTHING_TIME = 1.0       # exponential smoothing time constant (seconds)
DISPLAY_POLL_INTERVAL_SECONDS = 0.25
# View 1 pacing: the work (physics + render + SPI push) is the limiter; no
# extra sleep is needed. 24 MHz is the fastest rate this panel tolerates on
# the current wiring (40 MHz corrupts the display).
FLUID_FRAME_INTERVAL_SECONDS = 0.0
FLUID_SPI_HZ = 24000000
logging.basicConfig(level=logging.DEBUG)

case_fan = None
fan_tach = None
ipc_server = None
gps_collector = None
disp = None


def cleanup_and_exit(signum=None, frame=None):
    """Force peripherals to a safe state during service stop/shutdown."""
    global case_fan, fan_tach, ipc_server, gps_collector, disp

    if case_fan is not None:
        try:
            case_fan.value = 0.0
            case_fan.close()
        except Exception:
            pass
        finally:
            case_fan = None

    if fan_tach is not None:
        try:
            fan_tach._input.close()
        except Exception:
            pass
        finally:
            fan_tach = None

    if ipc_server is not None:
        try:
            ipc_server.stop()
        except Exception:
            pass
        finally:
            ipc_server = None

    if gps_collector is not None:
        try:
            gps_collector.stop()
        except Exception:
            pass
        finally:
            gps_collector = None

    if disp is not None:
        try:
            disp.module_exit()
        except Exception:
            pass
        finally:
            disp = None

    if signum is not None:
        raise SystemExit(0)


atexit.register(cleanup_and_exit)
signal.signal(signal.SIGTERM, cleanup_and_exit)
signal.signal(signal.SIGINT, cleanup_and_exit)


def _rpm_to_duty(target_rpm, curve):
    """Reverse-lookup: given a target RPM, return the PWM duty from the curve.

    curve is a list of {"pwm": float, "rpm": float} dicts sorted by PWM.
    Uses linear interpolation between the two nearest entries.
    """
    if not curve:
        return 0.0

    # Clamp to curve bounds
    min_rpm = curve[0]["rpm"]
    max_rpm = curve[-1]["rpm"]

    if target_rpm <= 0:
        return 0.0
    if target_rpm <= min_rpm:
        return 0.0
    if target_rpm >= max_rpm:
        return 1.0

    # Find bracketing entries
    for i in range(len(curve) - 1):
        lo = curve[i]
        hi = curve[i + 1]
        if lo["rpm"] <= target_rpm <= hi["rpm"]:
            if hi["rpm"] == lo["rpm"]:
                return lo["pwm"]
            frac = (target_rpm - lo["rpm"]) / (hi["rpm"] - lo["rpm"])
            return lo["pwm"] + frac * (hi["pwm"] - lo["pwm"])
        if hi["rpm"] <= target_rpm <= lo["rpm"]:
            if lo["rpm"] == hi["rpm"]:
                return lo["pwm"]
            frac = (target_rpm - hi["rpm"]) / (lo["rpm"] - hi["rpm"])
            return hi["pwm"] + frac * (lo["pwm"] - hi["pwm"])

    return 1.0


try:
    # display with hardware SPI:
    ''' Warning!!!Don't  creation of multiple displayer objects!!! '''
    disp = LCD_2inch4.LCD_2inch4(spi=SPI.SpiDev(bus, device),spi_freq=15000000,rst=RST,dc=DC,bl=BL)
    # disp = LCD_2inch4.LCD_2inch4()
    # Initialize library.
    disp.Init()
    # Clear display.
    disp.clear()
    #Set the backlight to 100
    disp.bl_DutyCycle(100)
    last_lcd_brightness = -1.0

    background_path = os.path.join(os.path.dirname(__file__), "pic", "cyberpunk_bg.png")
    background_image = Image.open(background_path).convert("RGB")
    background_image = ImageEnhance.Brightness(background_image).enhance(0.25)
    if background_image.size != (disp.width, disp.height):
        raise ValueError(
            f"Background image must be {disp.width}x{disp.height}, got {background_image.size}"
        )

    # Start IPC server for GTK applet communication
    ipc_server = display_server.DisplayControlServer(
        socket_path="/tmp/lcdstats.sock",
    )
    # Seed the server with the daemon's initial backlight so clients
    # (e.g. the GTK applet) report the real hardware value on connect.
    ipc_server.set_lcd_brightness(100)
    ipc_server.start()

    # View 1: live fluid simulation (Stam stable-fluids, see fluidsim.py).
    # Stepped in real time while view 1 is active; state persists across
    # view switches, and applet events (perturb/reset) are applied as they
    # arrive via the IPC server.
    fluid = PixFluid(target_size=(disp.width, disp.height))
    fluid.reset()

    case_fan = HardwarePWM(CASE_FAN, frequency=1000)
    case_fan.value = 0.0

    i = 0
    
    collector = systemStats.SystemStatisticsCollector()
    gps_collector = gpsStats.GPSStatisticsCollector()
    battery_collector = None
    next_battery_init_attempt = 0.0
    fan_tach = FanTachometer(FAN_TACH, pulses_per_rev=AUX_FAN_PULSES_PER_REV)

    # Load PWM→RPM lookup table
    aux_fan_curve = None
    try:
        with open(AUX_FAN_CURVE_FILE) as f:
            aux_fan_curve = json.load(f)["curve"]
        logging.info("Loaded aux fan curve with %d entries", len(aux_fan_curve))
    except Exception as e:
        logging.warning("Could not load aux fan curve (%s), using fallback", e)

    displayed_image = None
    smoothed_aux_duty = 0.0          # smoothed PWM duty for aux fan
    last_aux_duty = -1.0             # last written duty, avoid unnecessary writes
    last_loop_time = time.monotonic() # track actual iteration delta for smoothing
    current_view = 0                 # active screen view index

    while True:
        now = time.monotonic()
        dt = now - last_loop_time       # actual time since last iteration
        last_loop_time = now
        battery_sample = None

        if battery_collector is None and now >= next_battery_init_attempt:
            try:
                battery_collector = batteryStats.BatteryStatisticsCollector(addr=0x41)
            except Exception as e:
                logging.debug("INA219 init failed: %s", e)
                next_battery_init_attempt = now + 5.0

        if battery_collector is not None:
            try:
                battery_sample = battery_collector.read()
            except Exception as e:
                logging.debug("INA219 read failed: %s", e)
                battery_collector = None
                next_battery_init_attempt = now + 5.0

        cpu_fan_pwm = 255
        for pwm_path in glob.glob(CPU_FAN_PWM_GLOB):
            try:
                with open(pwm_path) as f:
                    cpu_fan_pwm = int(f.read())
                break
            except (OSError, ValueError):
                pass

        cpu_fan_rpm = 0.0
        for rpm_path in glob.glob(CPU_FAN_RPM_GLOB):
            try:
                with open(rpm_path) as f:
                    cpu_fan_rpm = float(f.read())
                break
            except (OSError, ValueError):
                pass

        # --- Aux fan: lookup-table control ---
        aux_fan_rpm = fan_tach.read_rpm()
        target_rpm = cpu_fan_rpm * AUX_FAN_TARGET_RPM_RATIO

        if aux_fan_curve:
            # Reverse-lookup: find the PWM duty that produces target_rpm
            # by interpolating the stored PWM→RPM curve.
            target_duty = _rpm_to_duty(target_rpm, aux_fan_curve)
        else:
            # Fallback: linear approximation (0% PWM → 0 RPM, 100% → max RPM)
            target_duty = min(1.0, target_rpm / 5000.0)

        alpha = min(dt / AUX_FAN_SMOOTHING_TIME, 1.0)
        smoothed_aux_duty += alpha * (target_duty - smoothed_aux_duty)
        new_duty = max(0.0, min(1.0, smoothed_aux_duty))
        if abs(new_duty - last_aux_duty) > 0.01:
            case_fan.value = new_duty
            last_aux_duty = new_duty


        # Sync SPI LCD backlight with the dedicated lcd_brightness value
        if ipc_server.has_lcd_brightness and ipc_server.lcd_brightness != last_lcd_brightness:
            disp.bl_DutyCycle(ipc_server.lcd_brightness)
        last_lcd_brightness = ipc_server.lcd_brightness

        # Sync active view
        current_view = ipc_server.current_view

        # Apply fluid events queued by applet clients (perturb / reset)
        for ev in ipc_server.drain_fluid_events():
            action = ev.get("action")
            if action == "reset":
                fluid.reset()
            elif action == "perturb":
                fluid.perturb(
                    x=float(ev.get("x", 0.5)),
                    y=float(ev.get("y", 0.5)),
                    strength=float(ev.get("strength", 500.0)),
                    radius=float(ev.get("radius", 40.0)),
                    fx=float(ev.get("fx", 0.0)),
                    fy=float(ev.get("fy", 0.0)),
                    dye=float(ev.get("dye", 0.0)),
                )

        # Full-frame pushes are bandwidth-bound: run the SPI faster in the
        # fluid view, restore the conservative rate for the dashboard.
        if disp.SPI is not None:
            want_speed = FLUID_SPI_HZ if current_view == 1 else disp.SPEED
            if disp.SPI.max_speed_hz != want_speed:
                disp.SPI.max_speed_hz = want_speed

        image1 = background_image.copy()
        draw = ImageDraw.Draw(image1)

        FontBigSize = 46
        FontSize = 25
        SmallFontSize = 18
        TinyFontSize = 14
        GPSFontSize = 15
        TextPadding = 1
        DividerHeight = 5
        FontBig = ImageFont.truetype("./Font/Font02.ttf",FontBigSize)
        Font = ImageFont.truetype("./Font/Font02.ttf",FontSize)
        SmallFont = ImageFont.truetype("./Font/Font02.ttf",SmallFontSize)
        TinyFont = ImageFont.truetype("./Font/Font02.ttf",TinyFontSize)
        GPSFont = ImageFont.truetype("./Font/Font02.ttf",GPSFontSize)

        drawpos = 5
        LPad = 8

        DividerColor = (50, 50, 50)

        draw.rectangle([(0, 0), (disp.width, disp.height)], outline=DividerColor, width=5)

        # ── View 0: Full Dashboard (default) ──────────────────────
        if current_view == 0:
            text = str(datetime.datetime.now().strftime('%H:%M'))
            draw.text((LPad, drawpos), text, fill = "WHITE",font=FontBig)

            # GPS status indicator occupies the space freed by dropping seconds.
            gps_x = LPad + 155
            gps_y = drawpos
            gps_line = GPSFontSize + 2

            if gps_collector.Connected:
                gps_fix_text = gps_collector.FixText
                gps_has_fix = gps_collector.FixMode >= 2
                gps_color = "GREEN" if gps_has_fix else "YELLOW"
                gps_signal_text = f"{gps_collector.SatsUsed}/{gps_collector.SatsVisible}"
                gps_clock_governed = gps_collector.ClockGoverned
            else:
                gps_fix_text = "No GPS"
                gps_color = "RED"
                gps_signal_text = "--/--"
                gps_clock_governed = False

            draw.text((gps_x, gps_y), "GPS", fill="CYAN", font=GPSFont)

            # Clock-governed indicator: small circle to the right of "GPS"
            # Green = clock is GPS-disciplined, red = not
            clock_blink_x = gps_x + 32
            clock_blink_y = gps_y + 7
            clock_color = "GREEN" if gps_clock_governed else "RED"
            draw.ellipse([clock_blink_x, clock_blink_y, clock_blink_x + 6, clock_blink_y + 6], fill=clock_color)
            draw.text((gps_x, gps_y + gps_line), f"Fix {gps_fix_text}", fill=gps_color, font=GPSFont)
            draw.text((gps_x, gps_y + gps_line * 2), f"Sig {gps_signal_text}", fill=gps_color, font=GPSFont)
            clock_text = "YES" if gps_clock_governed else "NO"
            clock_text_color = "GREEN" if gps_clock_governed else "RED"
            draw.text((gps_x, gps_y + gps_line * 3), f"Clock {clock_text}", fill=clock_text_color, font=GPSFont)

            drawpos = drawpos + FontBigSize + TextPadding

            text = str(datetime.datetime.now().strftime('%m-%d-%Y'))
            draw.text((LPad, drawpos), text, fill = "YELLOW",font=SmallFont)
            drawpos = drawpos + SmallFontSize + TextPadding

            drawpos = drawpos + DividerHeight
            draw.line([(0, drawpos), (240, drawpos)], fill = DividerColor, width = DividerHeight)

            text = collector.IPAddr
            draw.text((LPad, drawpos), text, fill = "YELLOW",font=Font)
            drawpos = drawpos + FontSize + TextPadding

            if collector._WIFI_QUALITY != "":
                text = collector.WIFI_SSID
                draw.text((LPad, drawpos), text, fill = "GREEN",font=SmallFont)
                drawpos = drawpos + SmallFontSize + TextPadding

                wifi_line = collector._WIFI_QUALITY + "%  " + collector._WIFI_RSSI
                if collector._WIFI_FREQ:
                    wifi_line += "  " + collector._WIFI_FREQ
                draw.text((LPad, drawpos), wifi_line, fill = RedGreenColorScale(float(collector._WIFI_QUALITY)),font=SmallFont)
                drawpos = drawpos + SmallFontSize + TextPadding

            drawpos = drawpos + DividerHeight
            draw.line([(0, drawpos), (240, drawpos)], fill = DividerColor, width = DividerHeight)

            drawpos = drawpos + TextPadding
            text = "CPU:"
            draw.text((LPad, drawpos), text, fill = "YELLOW",font=SmallFont)
            text = collector.CPUUsage + "   " + collector.CPUTemp
            draw.text((LPad + 40, drawpos), text, fill = "GREEN",font=SmallFont)
            drawpos = drawpos + SmallFontSize + TextPadding

            text = "Mem:"
            draw.text((LPad, drawpos), text, fill = "YELLOW",font=SmallFont)
            text = collector.MemUsage
            draw.text((LPad + 40, drawpos), text, fill = "GREEN",font=SmallFont)
            drawpos = drawpos + SmallFontSize + TextPadding

            text = "Disk:"
            draw.text((LPad, drawpos), text, fill= "YELLOW", font=SmallFont)
            text = collector.DiskUsage
            draw.text((LPad + 40, drawpos), text, fill= "GREEN", font=SmallFont)
            drawpos = drawpos + SmallFontSize + TextPadding

            text = "Fan:"
            draw.text((LPad, drawpos), text, fill="YELLOW", font=SmallFont)
            fan_text = f"CPU {cpu_fan_rpm:4.0f}  AUX {aux_fan_rpm:4.0f}"
            draw.text((LPad + 40, drawpos), fan_text, fill="GREEN", font=SmallFont)
            drawpos = drawpos + SmallFontSize + TextPadding

            text = collector.Uptime
            draw.text((LPad, drawpos), text, fill = "GREEN",font=SmallFont)
            drawpos = drawpos + SmallFontSize + TextPadding

            drawpos = drawpos + DividerHeight
            draw.line([(0, drawpos), (240, drawpos)], fill = DividerColor, width = DividerHeight)

            drawpos = drawpos + TextPadding
            draw.text((LPad, drawpos), "Battery", fill="YELLOW", font=SmallFont)
            drawpos = drawpos + SmallFontSize + TextPadding

            if battery_sample is None:
                draw.text((LPad, drawpos), "INA219 not detected", fill="RED", font=SmallFont)
                battery_voltage = 0.0
                battery_current = 0.0
                battery_power = 0.0
                battery_pct = 0.0
                drawpos = drawpos + SmallFontSize + TextPadding
            else:
                battery_voltage = battery_sample["voltage"]
                battery_current = battery_sample["current"]
                battery_power = battery_sample["power"]
                battery_pct = battery_sample["percentage"]
                current_color = RedGreenColorScale(
                    abs(battery_current) / 3.0 * 100,
                    invert=True,
                )

                draw.text((LPad, drawpos), f"V: {battery_voltage:5.2f}V", fill="CYAN", font=SmallFont)
                draw.text((LPad + 116, drawpos), f"I: {battery_current:5.2f}A", fill=current_color, font=SmallFont)
                drawpos = drawpos + SmallFontSize + TextPadding

                draw.text((LPad, drawpos), f"P: {battery_power:5.2f}W", fill="CYAN", font=SmallFont)
                draw.text((LPad + 116, drawpos), f"SOC: {battery_pct:5.1f}%", fill=SOCColor(battery_pct), font=SmallFont)
                drawpos = drawpos + SmallFontSize + TextPadding

            drawpos = drawpos + DividerHeight
            drawpos = drawpos + DividerHeight
            draw.line([(0, drawpos), (240, drawpos)], fill=DividerColor, width=DividerHeight)

        elif current_view == 1:
            # View 1: live 2-D pixel fluid (particle sim, Clavet
            # double-density relaxation).  step() advances by the real
            # elapsed time in fixed substeps (clamped), so the pour runs
            # at real speed at any loop rate; the dimmed dashboard
            # background shows through behind the water.
            fluid.step(min(dt, 0.1))
            image1 = fluid.render(background_image)
        else:
            # Unknown view — draw a placeholder
            draw.text((LPad, drawpos), f"View {current_view}", fill="WHITE", font=FontBig)

        image1=image1.rotate(0)
        if displayed_image is None:
            disp.ShowImage(image1)
        else:
            tile = 32 if current_view == 1 else 16
            for region in ChangedTileRegions(displayed_image, image1, tile):
                left, top, right, bottom = region
                ShowImageRegion(disp, image1.crop(region), left, top)
        displayed_image = image1
        
        if current_view == 1:
            time.sleep(FLUID_FRAME_INTERVAL_SECONDS)
        else:
            time.sleep(DISPLAY_POLL_INTERVAL_SECONDS)

except IOError as e:
    logging.info(e)    
except KeyboardInterrupt:
    logging.info("quit:")
finally:
    cleanup_and_exit()
