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
import spidev as SPI
sys.path.append(".")
from lib import LCD_2inch4
from PIL import Image,ImageDraw,ImageFont
from gpiozero import PWMOutputDevice
import systemStats
import batteryStats
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

# Raspberry Pi pin configuration:
RST = 27
DC = 25
BL = 18
bus = 0 
device = 0 
CASE_FAN = 13
DISPLAY_POLL_INTERVAL_SECONDS = 0.5
FAN_RAMP_UP_SECONDS = 0.7
FAN_RAMP_DOWN_SECONDS = 2.5
BATTERY_CHARGE_FAN_BOOST_A = 0.5  # Enable fan floor above 500 mA charging current.
BATTERY_CHARGE_FAN_MIN_DUTY = 0.5  # While boost is active, run at least 50% unless CPU fan target is higher.
logging.basicConfig(level=logging.DEBUG)
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
    
    case_fan = PWMOutputDevice(CASE_FAN)
    case_fan.value = 0.0

    i = 0
    
    collector = systemStats.SystemStatisticsCollector()
    battery_collector = None
    next_battery_init_attempt = 0.0
    mirrored_fan_value = 0.0
    last_fan_update = time.monotonic()

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

        cmd = 'cat /sys/devices/platform/cooling_fan/hwmon/*/pwm1'
        CPUFan_PWM = float(subprocess.check_output(cmd, shell=True).decode("utf-8"))
        cpu_fan_target = CPUFan_PWM / 255.0
        if cpu_fan_target < 0.0:
            cpu_fan_target = 0.0
        if cpu_fan_target > 1.0:
            cpu_fan_target = 1.0

        fan_target = cpu_fan_target
        if battery_current_for_fan > BATTERY_CHARGE_FAN_BOOST_A:
            fan_target = max(cpu_fan_target, BATTERY_CHARGE_FAN_MIN_DUTY)

        dt = max(0.0, now - last_fan_update)
        ramp_seconds = FAN_RAMP_UP_SECONDS if fan_target > mirrored_fan_value else FAN_RAMP_DOWN_SECONDS
        alpha = 1.0 if ramp_seconds <= 0 else 1.0 - math.exp(-dt / ramp_seconds)
        mirrored_fan_value += (fan_target - mirrored_fan_value) * alpha

        # Allow full stop when the source fan is off and no boost is active.
        if fan_target <= 0.0 and mirrored_fan_value < 0.01:
            mirrored_fan_value = 0.0

        case_fan.value = max(0.0, min(1.0, mirrored_fan_value))
        last_fan_update = now


        # Sync LCD backlight with main display brightness
        if ipc_server.has_brightness and ipc_server.brightness != last_ipc_server_brightness:
            disp.bl_DutyCycle(ipc_server.brightness)
        last_ipc_server_brightness = ipc_server.brightness

        # Create blank image for drawing.
        image1 = Image.new("RGB", (disp.width, disp.height ), "BLACK")
        draw = ImageDraw.Draw(image1)

        FontBigSize = 60
        FontSize = 25
        SmallFontSize = 18
        TextPadding = 1
        DividerHeight = 5
        FontBig = ImageFont.truetype("./Font/Font02.ttf",FontBigSize)
        Font = ImageFont.truetype("./Font/Font02.ttf",FontSize)
        SmallFont = ImageFont.truetype("./Font/Font02.ttf",SmallFontSize)

        drawpos = 5
        LPad = 8

        DividerColor = (50, 50, 50)

        draw.rectangle([(0, 0), (disp.width, disp.height)], outline=DividerColor, width=5)

        text = str(datetime.datetime.now().strftime('%H:%M:%S'))
        draw.text((LPad, drawpos), text, fill = "WHITE",font=FontBig)
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
    ipc_server.stop()
    disp.module_exit()
    logging.info("quit:")
    exit()
