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
    
    case_fan = PWMOutputDevice(CASE_FAN)
    case_fan.value = 0.0

    i = 0
    
    collector = systemStats.SystemStatisticsCollector()

    while True:
        
        cmd = 'cat /sys/devices/platform/cooling_fan/hwmon/*/pwm1'
        CPUFan_PWM = float(subprocess.check_output(cmd, shell=True).decode("utf-8"))
        fanValue = 0.35 + (CPUFan_PWM / 255)
        if (fanValue > 1.0): fanValue = 1.0
        case_fan.value = fanValue


        # Create blank image for drawing.
        image1 = Image.new("RGB", (disp.width, disp.height ), "BLACK")
        draw = ImageDraw.Draw(image1)

        FontBig = ImageFont.truetype("./Font/Font02.ttf",40)
        Font = ImageFont.truetype("./Font/Font02.ttf",25)
        SmallFont = ImageFont.truetype("./Font/Font02.ttf",18)
        
        text = str(datetime.datetime.now().strftime('%m-%d-%Y %H:%M:%S'))
        draw.text((5, 5), text, fill = "WHITE",font=Font)

        text = collector.IPAddr
        draw.text((5, 35), text, fill = "YELLOW",font=Font)

        if collector._WIFI_QUALITY != "":
            text = collector.WIFI_SSID
            draw.text((5, 70), text, fill = "GREEN",font=Font)

            text = collector._WIFI_QUALITY + "%  " + collector._WIFI_RSSI
            draw.text((5, 95), text, fill = "RED",font=SmallFont)

        text = "CPU:"
        draw.text((5, 120), text, fill = "YELLOW",font=SmallFont)
        text = collector.CPUUsage + "   " + collector.CPUTemp
        draw.text((40, 120), text, fill = "GREEN",font=SmallFont)

        text = "Mem:"
        draw.text((5, 140), text, fill = "YELLOW",font=SmallFont)
        text = collector.MemUsage
        draw.text((40, 140), text, fill = "GREEN",font=SmallFont)
        
        text = "Disk:"
        draw.text((5, 160), text, fill= "YELLOW", font=SmallFont)
        text = collector.DiskUsage
        draw.text((40, 160), text, fill= "GREEN", font=SmallFont)

        text = collector.Uptime
        draw.text((5, 190), text, fill = "GREEN",font=SmallFont)
        
        draw.text((30, 255), "Fan", fill = "RED", font=SmallFont)
        draw.text((105, 255), "CPU", fill = "RED", font=SmallFont)
        draw.text((175, 255), "Mem", fill = "RED", font=SmallFont)
        
        gaugeWidget.drawGauge(draw, 5, 275, 70, 100*(CPUFan_PWM / 255))
        
        gaugeWidget.drawGauge(draw, 80, 275, 70, float(collector.CPUUsage[:-1]))
        
        gaugeWidget.drawGauge(draw, 155, 275, 70, float(collector.MemUsage[-7:-1]))

        image1=image1.rotate(0)
        disp.ShowImage(image1)
        
        time.sleep(0.2)

except IOError as e:
    logging.info(e)    
except KeyboardInterrupt:
    disp.module_exit()
    logging.info("quit:")
    exit()
