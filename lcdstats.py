#!/usr/bin/python
# -*- coding: UTF-8 -*-
#import chardet
import os
import sys 
import time
import datetime
import subprocess
import logging
import spidev as SPI
sys.path.append(".")
from lib import LCD_2inch4
from PIL import Image,ImageDraw,ImageFont
from gpiozero import PWMOutputDevice
import systemStats
import gaugeWidget
import display_server

def RedGreenColorScale(value : float, invert : bool = False):
    if (value > 100): value = 100
    if (value < 0): value = 0

    highValue = 255 * (value / 100)
    lowValue = 255 - (255 * (value / 100))

    if (invert): return (int(highValue), int(lowValue), 0)
    else: return (int(lowValue), int(highValue), 0)

# Raspberry Pi pin configuration:
RST = 27
DC = 25
BL = 18
bus = 0 
device = 0 
CASE_FAN = 13
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

    while True:
        
        cmd = 'cat /sys/devices/platform/cooling_fan/hwmon/*/pwm1'
        CPUFan_PWM = float(subprocess.check_output(cmd, shell=True).decode("utf-8"))
        fanValue = 0.1 + (CPUFan_PWM / 255)
        if (fanValue > 1.0): fanValue = 1.0
        case_fan.value = fanValue


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

        draw.rectangle([(0, 0), (240, 360)], outline=DividerColor, width=5)

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
        
        draw.text((30, 255), "Vol", fill = "RED", font=SmallFont)
        draw.text((105, 255), "CPU", fill = "RED", font=SmallFont)
        draw.text((185, 255), "Mem", fill = "RED", font=SmallFont)
        
        vol_value = ipc_server.volume if ipc_server.has_volume else 0.0
        gaugeWidget.drawGauge(draw, 8, 275, 70, vol_value)
        
        gaugeWidget.drawGauge(draw, 85, 275, 70, float(collector.CPUUsage[:-1]))
        
        gaugeWidget.drawGauge(draw, 162, 275, 70, float(collector.MemUsage[-7:-1]))

        # Brightness / Volume bar section (only when connected)
        bar_y = 350
        bar_h = 10
        bar_left = LPad
        bar_max_w = 240 - LPad * 2

        if ipc_server.has_brightness:
            bri = ipc_server.brightness
            label = f"BRI {bri:.0f}%"
            draw.text((bar_left, bar_y - SmallFontSize - 1), label, fill="WHITE", font=SmallFont)
            # background
            draw.rectangle([(bar_left, bar_y), (bar_left + bar_max_w, bar_y + bar_h)],
                           outline="YELLOW", fill="BLACK")
            # filled portion
            fill_w = int(bar_max_w * bri / 100)
            if fill_w > 0:
                draw.rectangle([(bar_left, bar_y), (bar_left + fill_w, bar_y + bar_h)],
                               fill="YELLOW")
            bar_y += bar_h + SmallFontSize + 4

        if ipc_server.has_volume:
            vol = ipc_server.volume
            label = f"VOL {vol:.0f}%"
            draw.text((bar_left, bar_y - SmallFontSize - 1), label, fill="WHITE", font=SmallFont)
            draw.rectangle([(bar_left, bar_y), (bar_left + bar_max_w, bar_y + bar_h)],
                           outline="CYAN", fill="BLACK")
            fill_w = int(bar_max_w * vol / 100)
            if fill_w > 0:
                draw.rectangle([(bar_left, bar_y), (bar_left + fill_w, bar_y + bar_h)],
                               fill="CYAN")

        image1=image1.rotate(0)
        disp.ShowImage(image1)
        
        time.sleep(0.2)

except IOError as e:
    logging.info(e)    
except KeyboardInterrupt:
    ipc_server.stop()
    disp.module_exit()
    logging.info("quit:")
    exit()
