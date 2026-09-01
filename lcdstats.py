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
import repeaterStats
import display_server
from pixfluid import PixFluid
import sdr_waterfall

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


def FormatUptime(seconds: float):
    """Compact d/h/m uptime string for the repeater view."""
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"

def PacketAge(seconds: float):
    """Compact 'how long ago' string for the repeater packet feed."""
    seconds = int(max(0, seconds))
    if seconds < 5:
        return "now"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


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
# extra sleep is needed.
FLUID_FRAME_INTERVAL_SECONDS = 0.0
FLUID_SPI_HZ = 28000000  # fluid-view ceiling: 28 MHz renders, 30 MHz corrupts (white).
# 28 MHz = fastest confirmed-rendering speed = brightest (shorter SPI bursts
# = less sustained 5V backlight sag = less dimming/flicker). If you ever see a
# white flash at this rate, that's the ~30 MHz signal cliff — drop to 24.
# NOTE: the per-update brightness pulse is NOT a rate problem — it's the 5V
# backlight sagging under SPI load (proven: frozen gray flickers, frozen black
# stays solid). Higher rate only shortens the dip; decouple backlight power to
# kill it. 28 MHz is the highest rate that doesn't corrupt the panel.
logging.basicConfig(level=logging.DEBUG)

case_fan = None
fan_tach = None
ipc_server = None
gps_collector = None
repeater_collector = None
sdr_wf = None
disp = None


def cleanup_and_exit(signum=None, frame=None):
    """Force peripherals to a safe state during service stop/shutdown."""
    global case_fan, fan_tach, ipc_server, gps_collector, repeater_collector, sdr_wf, disp

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

    if repeater_collector is not None:
        try:
            repeater_collector.stop()
        except Exception:
            pass
        finally:
            repeater_collector = None

    if sdr_wf is not None:
        # Release the RTL-SDR dongle on shutdown so other programs can use it.
        try:
            sdr_wf.stop()
        except Exception:
            pass
        finally:
            sdr_wf = None

    if disp is not None:
        try:
            disp.module_exit()
        except Exception:
            pass
        finally:
            disp = None

    if signum is not None:
        # Terminate decisively with os._exit() rather than raising SystemExit.
        # Raising SystemExit can hang the shutdown: the main loop runs on the main
        # thread and periodically blocks in subprocess.check_output ("mpstat 1 1",
        # "vcgencmd measure_temp") -> os.waitpid. When SIGTERM lands while the main
        # thread is parked in that C-level wait, the SystemExit cannot unwind out of
        # it, so the process sits alive past systemd's stop timeout and gets SIGKILLed.
        # Hardware cleanup (module_exit, servers, collectors) is already complete
        # above, so it is safe to end the process immediately.
        logging.info("[SHUTDOWN] cleanup complete, exiting")
        os._exit(0)


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
    # bl_freq=5000: run the backlight on 5 kHz hardware PWM — well above the
    # perceptible-flicker range (1 kHz was the previous default).
    # 28 MHz base: fastest confirmed-rendering speed = brightest (shorter SPI
    # bursts = less sustained 5V backlight sag). Stays under the ~30 MHz signal
    # cliff. The permanent fix for dimming is still power decoupling on the LCD
    # 5V rail, but 28 MHz is the brightest the software side can get.
    disp = LCD_2inch4.LCD_2inch4(spi=SPI.SpiDev(bus, device),spi_freq=28000000,rst=RST,dc=DC,bl=BL,bl_freq=5000)
    # disp = LCD_2inch4.LCD_2inch4()
    # Brief settle before we drive the reset line (safety net; the real
    # blank-on-restart fix is the corrected ST7789 reset settle time in
    # LCD_2inch4.reset()).
    time.sleep(1.5)
    # Initialize library.
    disp.Init()
    # Baseline the panel to BLACK (not white) so there's no white content in
    # GRAM before the first frame. Backlight is still OFF here so it's
    # invisible — but if any tearing/frame-delay shows old content when the
    # backlight first lights, it'll be black (matches the dark theme) instead
    # of a white patch.
    disp.clear_color(0x0000)
    # Backlight is deliberately left OFF here. It's only switched on once the
    # first real frame has been drawn in the main loop below, so the panel
    # stays fully black through init and the user never sees the white flash.
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

    # View 3: RTL-SDR spectrum waterfall (sdr_waterfall.py).  The dongle is
    # opened ONLY while view 3 is active and closed on exit/shutdown, so it
    # stays free for other programs all the rest of the time.
    WF_TOP = 60                    # header strip: title + frequency text
    WF_BOTTOM_MARGIN = 8           # gap above the panel border line
    sdr_wf = sdr_waterfall.SdrWaterfall(
        width=disp.width,
        height=disp.height - WF_TOP - WF_BOTTOM_MARGIN,
    )
    case_fan = HardwarePWM(CASE_FAN, frequency=1000)
    case_fan.value = 0.0

    i = 0
    
    collector = systemStats.SystemStatisticsCollector()
    gps_collector = gpsStats.GPSStatisticsCollector()
    # View 2: openhop-repeater status (polls http://127.0.0.1:8000/api/stats).
    repeater_collector = repeaterStats.RepeaterStatisticsCollector()
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
    sdr_last_open_try = 0.0          # throttle dongle-open retries to 1/s

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


        # Sync SPI LCD backlight with the dedicated lcd_brightness value.
        # Gated on "at least one frame drawn" (displayed_image is not None) so
        # the panel is never lit before it has real content. From the second
        # iteration on this is the live path for applet brightness changes.
        if displayed_image is not None and ipc_server.has_lcd_brightness and ipc_server.lcd_brightness != last_lcd_brightness:
            disp.bl_DutyCycle(ipc_server.lcd_brightness)
            last_lcd_brightness = ipc_server.lcd_brightness

        # Sync active view
        current_view = ipc_server.current_view

        # View 3 owns the RTL-SDR dongle exclusively while active: open on
        # entry (retrying at most once a second if another program holds it),
        # close on exit so other programs get it back immediately.
        sdr_wanted = (current_view == 3)
        if sdr_wanted and not sdr_wf.active:
            if now - sdr_last_open_try > 1.0:
                sdr_last_open_try = now
                sdr_wf.start()
        elif not sdr_wanted and sdr_wf.active:
            sdr_wf.stop()

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
            elif action == "burst":
                fluid.burst(
                    side=ev.get("side", "left"),
                    duration=float(ev.get("duration", 0.6)),
                )
            elif action == "spout":
                fluid.spout(
                    side=ev.get("side", "left"),
                    on=bool(ev.get("on", True)),
                    row=ev.get("row"),
                )

        # Full-frame pushes are bandwidth-bound: run the SPI faster in the
        # fluid and SDR views (both push full frames), restore the
        # conservative rate for the dashboard.
        if disp.SPI is not None:
            want_speed = FLUID_SPI_HZ if current_view in (1, 3) else disp.SPEED
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

        elif current_view == 2:
            # View 2: openhop-repeater live status (polls /api/stats and
            # /api/recent_packets on a background thread — see repeaterStats.py).
            # Green dot = service up, yellow = radio degraded, red = down/disabled.
            draw.text((LPad, drawpos), "MC Repeater", fill="WHITE", font=FontBig)

            if repeater_collector.Connected:
                _radio_status = repeater_collector.RadioStatus
                if _radio_status == "degraded":
                    status_color = "YELLOW"
                elif _radio_status == "disabled":
                    status_color = "RED"
                else:
                    status_color = "GREEN"
            else:
                status_color = "RED"
            dot_x = disp.width - LPad - 10
            dot_y = drawpos + (FontBigSize - 8) // 2
            draw.ellipse([dot_x, dot_y, dot_x + 8, dot_y + 8], fill=status_color)

            drawpos = drawpos + FontBigSize + TextPadding
            drawpos = drawpos + DividerHeight
            draw.line([(0, drawpos), (240, drawpos)], fill=DividerColor, width=DividerHeight)

            if not repeater_collector.Connected:
                # Service down — the collector keeps retrying in the background.
                if repeater_collector.AuthFailed:
                    draw.text((LPad, drawpos + 80), "AUTH FAILED", fill="RED", font=Font)
                    draw.text((LPad, drawpos + 115), "no valid API token",
                              fill="YELLOW", font=TinyFont)
                    draw.text((LPad, drawpos + 132), "set REPEATER_API_KEY env or file",
                              fill="CYAN", font=TinyFont)
                else:
                    draw.text((LPad, drawpos + 80), "SERVICE DOWN", fill="RED", font=Font)
                    draw.text((LPad, drawpos + 115),
                              f"polling http://127.0.0.1:{repeaterStats.REPEATER_API_PORT}",
                              fill="YELLOW", font=TinyFont)
            else:
                # Node name + local hash (one line; the LCD clips past x=240)
                node_name = repeater_collector.NodeName[:18] or "?"
                local_hash = repeater_collector.LocalHash
                if local_hash:
                    draw.text((LPad, drawpos), f"{node_name}  #{local_hash[:16]}",
                              fill="GREEN", font=SmallFont)
                else:
                    draw.text((LPad, drawpos), node_name, fill="GREEN", font=SmallFont)
                drawpos = drawpos + SmallFontSize + TextPadding

                # RX / TX totals with rolling per-hour rates (one line)
                rx_text = f"RX {repeater_collector.RxCount} (+{int(repeater_collector.RxPerHour)}/h)"
                tx_text = f"TX {repeater_collector.TxCount}"
                draw.text((LPad, drawpos), rx_text[:20], fill="GREEN", font=SmallFont)
                draw.text((LPad + 130, drawpos), tx_text[:14], fill="CYAN", font=SmallFont)
                drawpos = drawpos + SmallFontSize + TextPadding

                # Mode / noise floor / airtime (one line)
                mode = repeater_collector.Mode
                mode_color = {"forward": "GREEN", "monitor": "YELLOW"}.get(mode, "RED")
                noise_floor = repeater_collector.NoiseFloorDbm
                nf_text = "--" if noise_floor is None else f"{noise_floor:.0f}dBm"
                airtime_pct = repeater_collector.AirtimeUtilizationPct
                draw.text((LPad, drawpos), mode or "?", fill=mode_color, font=SmallFont)
                draw.text((LPad + 78, drawpos), nf_text, fill="CYAN", font=SmallFont)
                draw.text((LPad + 150, drawpos), f"{airtime_pct:.0f}%",
                          fill=RedGreenColorScale(airtime_pct), font=SmallFont)
                drawpos = drawpos + SmallFontSize + TextPadding

                # --- Live packet feed (the main event) -------------------
                drawpos = drawpos + DividerHeight
                draw.line([(0, drawpos), (240, drawpos)], fill=DividerColor, width=DividerHeight)

                packets = repeater_collector.RecentPackets
                now_ts = time.time()

                # Recent adverts: count in the last 10 min + newest one's src.
                recent_adverts = [p for p in packets
                                  if p["type_name"] == "ADVERT"
                                  and (now_ts - p["timestamp"]) <= 600]
                if recent_adverts:
                    newest_adv = recent_adverts[0]
                    adv_text = f"{len(recent_adverts)} last 10m, {newest_adv['src_hash']}→{newest_adv['dst_hash']}"
                else:
                    adv_text = "none in last 10m"
                draw.text((LPad, drawpos), "Advert:", fill="YELLOW", font=SmallFont)
                draw.text((LPad + 56, drawpos), adv_text[:24], fill="GREEN", font=SmallFont)
                drawpos = drawpos + SmallFontSize + TextPadding

                # Newest-first feed: src→dst TYPE ROUTE rssi age. Dropped
                # packets show DROP in red; duplicates are dimmed grey.
                for p in packets[:8]:
                    if p["drop_reason"]:
                        tail = "DROP"
                    elif p["rssi"] is not None:
                        tail = f"{p['rssi']}dBm"
                    else:
                        tail = ""
                    line = f"{p['src_hash']}→{p['dst_hash']} {p['type_name']} {p['route_name']}"
                    if tail:
                        line += f" {tail}"
                    line += f" {PacketAge(now_ts - p['timestamp'])}"
                    if p["drop_reason"]:
                        color = "RED"
                    elif p["is_duplicate"]:
                        color = (120, 120, 120)
                    else:
                        color = "GREEN"
                    draw.text((LPad, drawpos), line[:34], fill=color, font=TinyFont)
                    drawpos = drawpos + TinyFontSize + TextPadding

                if not packets:
                    draw.text((LPad, drawpos), "no packets yet", fill="CYAN", font=TinyFont)

        elif current_view == 3:
            # View 3: RTL-SDR spectrum waterfall.  The reader thread owns all
            # USB/FFT work; here we only blit the latest frame + a header.
            draw.text((LPad, drawpos), "SDR", fill="WHITE", font=FontBig)

            freq_text = f"{sdr_wf.center_hz / 1e6:g} MHz"   # :g keeps 910.525 intact
            draw.text((LPad + 72, drawpos + 4), freq_text[:13], fill="CYAN", font=SmallFont)

            half_khz = sdr_wf.sample_rate // 2000
            span_str = (f"+/-{half_khz} kHz" if half_khz < 1000
                        else f"+/-{half_khz / 1000:g} MHz")
            span_text = f"{span_str}   gain {sdr_wf.gain_db:.0f} dB"
            draw.text((LPad + 72, drawpos + SmallFontSize + 4), span_text,
                      fill=(150, 150, 150), font=TinyFont)

            # Color waterfall: palette mapping happens in sdr_waterfall.
            wf_img = Image.fromarray(sdr_wf.snapshot_rgb(), "RGB")
            image1.paste(wf_img, (0, WF_TOP))

            if not sdr_wf.active:
                # Dongle busy / missing / dropped — explain over the frame.
                msg = sdr_wf.error or "acquiring..."
                draw.text((LPad + 30, disp.height // 2), msg[:16], fill="RED", font=SmallFont)

        else:
            # Unknown view — draw a placeholder
            draw.text((LPad, drawpos), f"View {current_view}", fill="WHITE", font=FontBig)

        image1=image1.rotate(0)
        if displayed_image is None:
            # First full frame. (An earlier version re-pushed this same frame
            # once more ~1s later as a band-aid for the blank-on-restart bug.
            # The real fix is the ST7789 reset settle time in
            # LCD_2inch4.reset(), so the first frame now lands correctly on its
            # own — and dropping the redundant full-screen rewrite also removes
            # the one-frame flicker it used to cause.)
            disp.ShowImage(image1)
            # Give the panel a beat to finish displaying the first frame before
            # we light the backlight, so no partial/old content shows through.
            time.sleep(0.25)
            # First real frame is on the panel — safe to light the backlight.
            # (It was held off during init so the user never sees the flash.)
            if ipc_server.has_lcd_brightness:
                disp.bl_DutyCycle(ipc_server.lcd_brightness)
                last_lcd_brightness = ipc_server.lcd_brightness
        else:
            tile = 32 if current_view == 1 else 16
            for region in ChangedTileRegions(displayed_image, image1, tile):
                left, top, right, bottom = region
                ShowImageRegion(disp, image1.crop(region), left, top)
        displayed_image = image1
        
        if current_view in (1, 3):
            time.sleep(FLUID_FRAME_INTERVAL_SECONDS)
        else:
            time.sleep(DISPLAY_POLL_INTERVAL_SECONDS)

except IOError as e:
    logging.info(e)    
except KeyboardInterrupt:
    logging.info("quit:")
finally:
    cleanup_and_exit()
