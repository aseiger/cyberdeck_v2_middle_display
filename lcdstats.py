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

        text = collector.Hostname
        draw.text((5, 5), text, fill = "WHITE", font=FontBig)
        
        text = str(datetime.datetime.now().strftime('%m-%d-%Y %H:%M:%S'))
        draw.text((5, 44), text, fill = "YELLOW",font=Font)

        text = collector.WIFI_SSID
        draw.text((5, 75), text, fill = "GREEN",font=Font)

        text = collector.IPAddr
        draw.text((5, 100), text, fill = "GREEN",font=Font)

        text = "CPU: " + collector.CPUUsage + "   " + collector.CPUTemp
        draw.text((5, 150), text, fill = "GREEN",font=SmallFont)

        text = "Mem: " + collector.MemUsage
        draw.text((5, 175), text, fill = "GREEN",font=SmallFont)
        
        text = "Disk: " + collector.DiskUsage
        draw.text((5, 200), text, fill= "GREEN", font=SmallFont)

        text = collector.Uptime
        draw.text((5, 225), text, fill = "GREEN",font=SmallFont)
        
        # text = collector.CPUFan + "RPM   " + "{:.0f}%".format(100*(CPUFan_PWM / 255))
        # draw.text((5, 275), text, fill = "GREEN", font=SmallFont)
        
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
