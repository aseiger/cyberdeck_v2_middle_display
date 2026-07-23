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
from PIL import Image,ImageDraw,ImageFont
from gpiozero import PWMOutputDevice, Button
import glob
import systemStats
import batteryStats
import gpsStats
import display_server

def RedGreenColorScale(value : float, invert : bool = False):
    if (value > 100): value = 100
    if (value < 0): value = 0

    highValue = 255 * (value / 100)
    lowValue = 255 - (255 * (value / 100))

    if (invert): return (int(highValue), int(lowValue), 0)
    else: return (int(lowValue), int(highValue), 0)


def SignedValueColor(value: float):
    # Positive and negative values are highlighted to show charge direction.
    if value > 0.01:
        return "GREEN"
    if value < -0.01:
        return "RED"
    return "YELLOW"


class PIDController:
    """A simple PID controller for fan speed control."""
    
    def __init__(self, kp=1.0, ki=0.8, kd=0.1, output_min=0.0, output_max=1.0):
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.output_min = output_min
        self.output_max = output_max
        
        self.previous_error = 0.0
        self.integral = 0.0
        self.previous_time = time.monotonic()
        
    def compute(self, setpoint, measured_value):
        """Compute the PID output given a setpoint and measured value."""
        current_time = time.monotonic()
        dt = current_time - self.previous_time
        
        if dt <= 0:
            return 0.0
            
        error = setpoint - measured_value
        self.integral += error * dt
        # Anti-windup: keep the integral term within the output range.
        if self.ki > 0:
            self.integral = max(self.output_min / self.ki,
                                min(self.output_max / self.ki, self.integral))
        derivative = (error - self.previous_error) / dt
        
        # PID output
        output = (self.kp * error + 
                 self.ki * self.integral + 
                 self.kd * derivative)
        
        # Clamp output to valid range
        output = max(self.output_min, min(self.output_max, output))
        
        self.previous_error = error
        self.previous_time = current_time
        
        return output

    def reset(self):
        """Clear accumulated state (use when the loop is disabled)."""
        self.previous_error = 0.0
        self.integral = 0.0
        self.previous_time = time.monotonic()


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
AUX_FAN_MAX_RPM = 4600.0           # measured at 100% duty via detect_tach.py
AUX_FAN_STOP_RPM = 300.0           # below this target RPM, just stop the fan
CPU_FAN_RPM_GLOB = "/sys/devices/platform/cooling_fan/hwmon/*/fan1_input"
DISPLAY_POLL_INTERVAL_SECONDS = 0.5
BATTERY_CHARGE_FAN_BOOST_A = 0.5  # Enable fan floor above 500 mA charging current.
BATTERY_CHARGE_FAN_MIN_RPM = 0.5 * AUX_FAN_MAX_RPM  # RPM floor while charge boost is active.
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

try:
    # display with hardware SPI:
    ''' Warning!!!Don't  creation of multiple displayer objects!!! '''
    disp = LCD_2inch4.LCD_2inch4(spi=SPI.SpiDev(bus, device),spi_freq=5000000,rst=RST,dc=DC,bl=BL)
    # disp = LCD_2inch4.LCD_2inch4()
    # Initialize library.
    disp.Init()
    # Clear display.
    disp.clear()
    #Set the backlight to 100
    disp.bl_DutyCycle(100)
    last_ipc_server_brightness = -1.0

    # Start IPC server for GTK applet communication
    ipc_server = display_server.DisplayControlServer(
        socket_path="/tmp/lcdstats.sock",
    )
    ipc_server.start()
    
    case_fan = PWMOutputDevice(CASE_FAN, initial_value=0.0)
    case_fan.value = 0.0

    i = 0
    
    collector = systemStats.SystemStatisticsCollector()
    gps_collector = gpsStats.GPSStatisticsCollector()
    battery_collector = None
    next_battery_init_attempt = 0.0
    fan_tach = FanTachometer(FAN_TACH, pulses_per_rev=AUX_FAN_PULSES_PER_REV)
    # PID trims around the feedforward duty; error and output are normalized
    # to the fan's full range, so gains are dimensionless. Gains are kept low
    # because the 0.5s tach window is noisy (~40 pulses/sample) and the
    # feedforward term already does most of the work.
    fan_pid = PIDController(kp=0.05, ki=0.05, kd=0.0, output_min=-0.25, output_max=0.25)
    aux_fan_rpm_smooth = 0.0
    AUX_FAN_RPM_SMOOTHING = 0.3  # EMA weight per 0.5s sample (~1.4s time constant)

    while True:
        now = time.monotonic()
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

        battery_current_for_fan = battery_sample["current"] if battery_sample is not None else 0.0

        # Setpoint: match the Pi's built-in fan RPM (fan1_input reports RPM directly).
        cpu_fan_rpm = 0.0
        for rpm_path in glob.glob(CPU_FAN_RPM_GLOB):
            try:
                with open(rpm_path) as f:
                    cpu_fan_rpm = float(f.read())
                break
            except (OSError, ValueError):
                pass

        target_rpm = cpu_fan_rpm
        if battery_current_for_fan > BATTERY_CHARGE_FAN_BOOST_A:
            target_rpm = max(target_rpm, BATTERY_CHARGE_FAN_MIN_RPM)
        target_rpm = min(target_rpm, AUX_FAN_MAX_RPM)

        aux_fan_rpm = fan_tach.read_rpm()
        # Smooth the noisy per-window tach reading before it reaches the PID.
        aux_fan_rpm_smooth += (aux_fan_rpm - aux_fan_rpm_smooth) * AUX_FAN_RPM_SMOOTHING

        if target_rpm < AUX_FAN_STOP_RPM:
            # Below the aux fan's usable range: stop it and reset the loop.
            fan_pid.reset()
            aux_fan_rpm_smooth = 0.0
            case_fan.value = 0.0
        else:
            # Feedforward gets close to the right duty; PID trims out the
            # remaining RPM error (both normalized to the fan's full range).
            feedforward = target_rpm / AUX_FAN_MAX_RPM
            trim = fan_pid.compute(target_rpm / AUX_FAN_MAX_RPM,
                                   aux_fan_rpm_smooth / AUX_FAN_MAX_RPM)
            case_fan.value = max(0.0, min(1.0, feedforward + trim))


        # Sync LCD backlight with main display brightness
        if ipc_server.has_brightness and ipc_server.brightness != last_ipc_server_brightness:
            disp.bl_DutyCycle(ipc_server.brightness)
        last_ipc_server_brightness = ipc_server.brightness

        # Create blank image for drawing.
        image1 = Image.new("RGB", (disp.width, disp.height ), "BLACK")
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
            if gps_collector.PPSActive:
                gps_pps_text = "OK"
                gps_pps_color = "GREEN"
            else:
                gps_pps_text = "--"
                gps_pps_color = (120, 120, 120)
        else:
            gps_fix_text = "No GPS"
            gps_color = "RED"
            gps_signal_text = "--/--"
            gps_pps_text = "--"
            gps_pps_color = (120, 120, 120)

        draw.text((gps_x, gps_y), "GPS", fill="CYAN", font=GPSFont)

        # NMEA activity blinker: small circle to the right of "GPS"
        nmea_blink_x = gps_x + 32
        nmea_blink_y = gps_y + 7
        if gps_collector.Connected:
            # Solid green when connected but no NMEA data yet
            nmea_color = "GREEN"
        else:
            nmea_color = "RED"
        draw.ellipse([nmea_blink_x, nmea_blink_y, nmea_blink_x + 6, nmea_blink_y + 6], fill=nmea_color)
        draw.text((gps_x, gps_y + gps_line), f"Fix {gps_fix_text}", fill=gps_color, font=GPSFont)
        draw.text((gps_x, gps_y + gps_line * 2), f"Sig {gps_signal_text}", fill=gps_color, font=GPSFont)
        draw.text((gps_x, gps_y + gps_line * 3), f"PPS {gps_pps_text}", fill=gps_pps_color, font=GPSFont)


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

            text = collector._WIFI_QUALITY + "%  " + collector._WIFI_RSSI
            draw.text((LPad, drawpos), text, fill = RedGreenColorScale(float(collector._WIFI_QUALITY)),font=SmallFont)
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
        if target_rpm < AUX_FAN_STOP_RPM:
            fan_text = "off"
            fan_color = (120, 120, 120)
        else:
            fan_text = f"{aux_fan_rpm_smooth:4.0f} / {target_rpm:4.0f} rpm"
            # Green when tracking within 10% of target, yellow otherwise.
            rpm_error = abs(aux_fan_rpm_smooth - target_rpm) / target_rpm
            fan_color = "GREEN" if rpm_error <= 0.10 else "YELLOW"
        draw.text((LPad + 40, drawpos), fan_text, fill=fan_color, font=SmallFont)
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
            current_color = SignedValueColor(battery_current)

            draw.text((LPad, drawpos), f"V: {battery_voltage:5.2f}V", fill="CYAN", font=SmallFont)
            draw.text((LPad + 116, drawpos), f"I: {battery_current:5.2f}A", fill=current_color, font=SmallFont)
            drawpos = drawpos + SmallFontSize + TextPadding

            draw.text((LPad, drawpos), f"P: {battery_power:5.2f}W", fill="CYAN", font=SmallFont)
            draw.text((LPad + 116, drawpos), f"SOC: {battery_pct:5.1f}%", fill=RedGreenColorScale(battery_pct), font=SmallFont)
            drawpos = drawpos + SmallFontSize + TextPadding

        bar_left = LPad
        bar_top = disp.height - 10
        bar_height = 6
        bar_width = disp.width - (LPad * 2)
        bar_fill_w = int(bar_width * max(0.0, min(100.0, battery_pct)) / 100.0)

        draw.rectangle(
            [(bar_left, bar_top), (bar_left + bar_width, bar_top + bar_height)],
            fill=(22, 22, 22),
            outline=(140, 140, 140),
            width=2,
        )
        if bar_fill_w > 0:
            draw.rectangle(
                [(bar_left + 1, bar_top + 1), (bar_left + bar_fill_w - 1, bar_top + bar_height - 1)],
                fill=RedGreenColorScale(battery_pct),
            )

        image1=image1.rotate(0)
        disp.ShowImage(image1)
        
        time.sleep(DISPLAY_POLL_INTERVAL_SECONDS)

except IOError as e:
    logging.info(e)    
except KeyboardInterrupt:
    logging.info("quit:")
finally:
    cleanup_and_exit()
