#!/usr/bin/env python
# -*- coding: utf-8 -*-

import time

import smbus

# INA219 registers
_REG_CONFIG = 0x00
_REG_SHUNTVOLTAGE = 0x01
_REG_BUSVOLTAGE = 0x02
_REG_POWER = 0x03
_REG_CURRENT = 0x04
_REG_CALIBRATION = 0x05


class BusVoltageRange:
    RANGE_16V = 0x00
    RANGE_32V = 0x01


class Gain:
    DIV_1_40MV = 0x00
    DIV_2_80MV = 0x01
    DIV_4_160MV = 0x02
    DIV_8_320MV = 0x03


class ADCResolution:
    ADCRES_12BIT_32S = 0x0D


class Mode:
    SANDBVOLT_CONTINUOUS = 0x07


class INA219:
    def __init__(self, i2c_bus=1, addr=0x41):
        self.bus = smbus.SMBus(i2c_bus)
        self.addr = addr
        self._cal_value = 0
        self._current_lsb = 0.0
        self._power_lsb = 0.0
        self.set_calibration_16V_5A()

    def _read(self, address):
        data = self.bus.read_i2c_block_data(self.addr, address, 2)
        return (data[0] << 8) + data[1]

    def _write(self, address, data):
        payload = [(data >> 8) & 0xFF, data & 0xFF]
        self.bus.write_i2c_block_data(self.addr, address, payload)

    def _to_signed_16(self, value):
        if value >= 0x8000:
            return value - 0x10000
        return value

    def set_calibration_16V_5A(self):
        # Calibration constants from the UPS module INA219 example.
        self._current_lsb = 0.1524
        self._cal_value = 26868
        self._power_lsb = 0.003048

        self._write(_REG_CALIBRATION, self._cal_value)

        bus_voltage_range = BusVoltageRange.RANGE_16V
        gain = Gain.DIV_2_80MV
        bus_adc_resolution = ADCResolution.ADCRES_12BIT_32S
        shunt_adc_resolution = ADCResolution.ADCRES_12BIT_32S
        mode = Mode.SANDBVOLT_CONTINUOUS

        config = (
            (bus_voltage_range << 13)
            | (gain << 11)
            | (bus_adc_resolution << 7)
            | (shunt_adc_resolution << 3)
            | mode
        )
        self._write(_REG_CONFIG, config)

    def get_shunt_voltage_v(self):
        self._write(_REG_CALIBRATION, self._cal_value)
        value = self._read(_REG_SHUNTVOLTAGE)
        value = self._to_signed_16(value)
        return value * 0.00001

    def get_bus_voltage_v(self):
        self._write(_REG_CALIBRATION, self._cal_value)
        value = self._read(_REG_BUSVOLTAGE)
        return (value >> 3) * 0.004

    def get_current_a(self):
        value = self._read(_REG_CURRENT)
        value = self._to_signed_16(value)
        return (value * self._current_lsb) / 1000.0

    def get_power_w(self):
        self._write(_REG_CALIBRATION, self._cal_value)
        value = self._read(_REG_POWER)
        value = self._to_signed_16(value)
        return value * self._power_lsb


class BatteryStatisticsCollector:
    def __init__(
        self,
        i2c_bus=1,
        addr=0x41,
        min_voltage=9.0,
        max_voltage=12.6,
        refresh_seconds=0.5,
    ):
        self.min_voltage = min_voltage
        self.max_voltage = max_voltage
        self.refresh_seconds = refresh_seconds

        # 3S Li-ion open-circuit-voltage approximation (pack voltage -> %).
        # Table is ordered low->high and linearly interpolated between points.
        self._soc_curve = [
            (9.00, 0.0),
            (9.30, 3.0),
            (9.60, 7.0),
            (9.90, 12.0),
            (10.20, 18.0),
            (10.50, 28.0),
            (10.80, 40.0),
            (11.10, 53.0),
            (11.40, 66.0),
            (11.70, 78.0),
            (12.00, 88.0),
            (12.30, 96.0),
            (12.60, 100.0),
        ]

        self._ina219 = INA219(i2c_bus=i2c_bus, addr=addr)
        self._last_read_time = 0.0
        self._last_sample = {
            "voltage": 0.0,
            "current": 0.0,
            "power": 0.0,
            "percentage": 0.0,
        }

    def _percentage_from_voltage(self, voltage):
        curve = self._soc_curve
        if not curve:
            return 0.0

        if voltage <= curve[0][0]:
            return curve[0][1]
        if voltage >= curve[-1][0]:
            return curve[-1][1]

        for idx in range(1, len(curve)):
            low_v, low_pct = curve[idx - 1]
            high_v, high_pct = curve[idx]
            if low_v <= voltage <= high_v:
                if high_v == low_v:
                    return high_pct
                t = (voltage - low_v) / (high_v - low_v)
                return low_pct + (high_pct - low_pct) * t

        return 0.0

    def read(self):
        now = time.monotonic()
        if now - self._last_read_time < self.refresh_seconds:
            return self._last_sample

        bus_voltage = self._ina219.get_bus_voltage_v()
        shunt_voltage = self._ina219.get_shunt_voltage_v()
        voltage = bus_voltage + shunt_voltage
        current = self._ina219.get_current_a()
        # INA219 power register is typically magnitude-only on many modules.
        # Use current direction to provide signed charging/discharging power.
        power = abs(self._ina219.get_power_w())
        if current < -0.01:
            power = -power
        elif current <= 0.01:
            power = 0.0
        percentage = self._percentage_from_voltage(voltage)

        self._last_sample = {
            "voltage": voltage,
            "current": current,
            "power": power,
            "percentage": percentage,
        }
        self._last_read_time = now
        return self._last_sample
