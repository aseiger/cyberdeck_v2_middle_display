#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  untitled.py
#  
#  Copyright 2025  <alex@cyberdeck2>
#  
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 2 of the License, or
#  (at your option) any later version.
#  
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#  
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
#  MA 02110-1301, USA.
#  
#  

import threading
import time
import subprocess

class SystemStatisticsCollector :     
    
    def __init__(self):
        self._IPAddr = ""
        self._WIFI_SSID = ""
        self._WIFI_RSSI = ""
        self._WIFI_QUALITY = ""
        self._Hostname = ""
        self._CPUUsage = ""
        self._CPUTemp = ""
        self._CPUFan = ""
        self._MemUsage = ""
        self._DiskUsage = ""
        self._Uptime = ""
        
        
        self.get_ip()
        self.get_wifi_ssid()
        self.get_wifi_rssi()
        self.get_wifi_quality()
        self.get_hostname()
        self.get_cpu_usage()
        self.get_cpu_temp()
        self.get_cpu_fan()
        self.get_mem_usage()
        self.get_disk_usage()
        self.get_uptime()
    
    @property
    def IPAddr(self):
        return self._IPAddr
        
    @property
    def WIFI_SSID(self):
        return self._WIFI_SSID
    
    @property
    def WIFI_RSSI(self):
        return self._WIFI_RSSI
    
    @property
    def WIFI_QUALITY(self):
        return self._WIFI_QUALITY
    
    @property
    def Hostname(self):
        return self._Hostname
        
    @property
    def CPUUsage(self):
        return self._CPUUsage
        
    @property
    def CPUTemp(self):
        return self._CPUTemp
        
    @property
    def CPUFan(self):
        return self._CPUFan
        
    @property
    def MemUsage(self):
        return self._MemUsage
        
    @property
    def DiskUsage(self):
        return self._DiskUsage
        
    @property
    def Uptime(self):
        return self._Uptime
        

    def get_ip(self):
        cmd = "hostname -I | cut -d' ' -f1"
        self._IPAddr = subprocess.check_output(cmd, shell=True).decode("utf-8")
        threading.Timer(5, self.get_ip).start()
        
    def get_wifi_ssid(self):
        cmd = "iwconfig 2>/dev/null | grep 'ESSID' | awk -F: '{gsub(/\"/, \"\"); print $2}'"
        self._WIFI_SSID = subprocess.check_output(cmd, shell=True).decode("utf-8")
        threading.Timer(5, self.get_wifi_ssid).start()

    def get_wifi_rssi(self):
        cmd = "iwconfig 2>/dev/null | grep 'Signal level' | awk -F= '{print $3}'"
        self._WIFI_RSSI = subprocess.check_output(cmd, shell=True).decode("utf-8").strip()
        threading.Timer(5, self.get_wifi_rssi).start()

    def get_wifi_quality(self):
        cmd = "iwconfig 2>/dev/null | grep 'Signal level' | awk -F'=' '{print $2}' | awk '{print $1}' | awk -F'/' '{print 100*($1/$2) }'"
        self._WIFI_QUALITY = subprocess.check_output(cmd, shell=True).decode("utf-8").strip()
        threading.Timer(5, self.get_wifi_quality).start()

    def get_hostname(self):
        cmd = "hostname"
        self._Hostname = subprocess.check_output(cmd, shell=True).decode("utf-8")
        threading.Timer(5, self.get_hostname).start()
        
    def get_cpu_usage(self):
        cmd =  "mpstat 1 1 | grep \"Average\" | awk '{print 100 - $12 \"%\"}'"
        self._CPUUsage = subprocess.check_output(cmd, shell=True).decode("utf-8").strip()
        threading.Timer(1, self.get_cpu_usage).start()
        
    def get_cpu_temp(self):
        cmd = "vcgencmd measure_temp | cut -d'=' -f 2"
        self._CPUTemp = subprocess.check_output(cmd, shell=True).decode("utf-8").strip()
        threading.Timer(1, self.get_cpu_temp).start()
        
    def get_cpu_fan(self):
        cmd = "cat /sys/devices/platform/cooling_fan/hwmon/*/fan1_input"
        self._CPUFan = subprocess.check_output(cmd, shell=True).decode("utf-8").strip()
        threading.Timer(1, self.get_cpu_fan).start()
        
    def get_mem_usage(self):
        cmd = "free -m | awk 'NR==2{printf \"%s/%s MB  %.2f%%\", $3,$2,$3*100/$2 }'"
        self._MemUsage = subprocess.check_output(cmd, shell=True).decode("utf-8")
        threading.Timer(1, self.get_mem_usage).start()
        
    def get_disk_usage(self):
        cmd = "df -h | awk '$NF==\"/\"{printf \"%d/%d GB  %s\", $3,$2,$5}'"
        self._DiskUsage = subprocess.check_output(cmd, shell=True).decode("utf-8")
        threading.Timer(5, self.get_disk_usage).start()
        
    def get_uptime(self):
        cmd = 'uptime -p'
        self._Uptime = subprocess.check_output(cmd, shell=True).decode("utf-8")
        threading.Timer(1, self.get_uptime).start()

def main(args):
    
    collector = SystemStatisticsCollector()
    while True:
        print(collector.IPAddr)
        time.sleep(1)
    return 0

if __name__ == '__main__':
    import sys
    sys.exit(main(sys.argv))
