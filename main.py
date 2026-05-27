#!/usr/bin/python3 -u
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# venus-ess-automatic-grid-setpoint
# Dynamically adjusts the Victron ESS grid setpoint and maximum discharge power
# based on battery SoC, time of day and an optional weekly charging schedule.
#
# Copyright (C) 2025 Martin Patočka <mapik@mapik.net>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import os
import sys
import time
from datetime import datetime

from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib
import dbus

# Victron packages
sys.path.insert(1, '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python')
from settingsdevice import SettingsDevice

# D-Bus paths for reading from com.victronenergy.system
SOC_PATH = '/Dc/Battery/Soc'
DC_POWER_PATH = '/Dc/Battery/Power'
ACOUT1_L1_POWER_PATH = '/Ac/ConsumptionOnOutput/L1/Power'
ACOUT1_L2_POWER_PATH = '/Ac/ConsumptionOnOutput/L2/Power'
ACOUT1_L3_POWER_PATH = '/Ac/ConsumptionOnOutput/L3/Power'

# D-Bus paths for writing to com.victronenergy.settings
GRID_SETPOINT_PATH = '/Settings/CGwacs/AcPowerSetPoint'
# Maximal discharge power path
MAX_DISCHARGE_PATH = '/Settings/CGwacs/MaxDischargePower'

# D-Bus path for writing to com.victronenergy.hub4 (Overrides)
GRID_SETPOINT_PATH_OVERRIDE = '/Overrides/Setpoint'

# User configuration – copy config.py.example to config.py and adjust to your needs
from config import (
    LOCAL_TIMEZONE,
    USE_HUB4_OVERRIDES,
    MAX_DAY_DISCHARGE,
    MAX_NIGHT_DISCHARGE,
    SCHEDULED_CHARGE_DAY,
    SCHEDULED_CHARGE_POWER,
    DISCHARGE_SOC_LIMIT,
    DISCHARGE_HYSTERESIS_MARGIN,
    SETPOINT_STEP,
    SOC_RANGES,
)

def get_limits_for_soc(soc_value):
    soc_int = int(soc_value)
    for min_soc, max_soc, min_power_limit, dc_power_target in SOC_RANGES:
        if min_soc <= soc_int <= max_soc:
            return min_power_limit, dc_power_target
    return 0.0, 0.0

def validate_soc_ranges():
    prev_max = -1
    for lo, hi, *_ in SOC_RANGES:
        if lo != prev_max + 1:
            raise ValueError("Gap or overlap in SOC_RANGES")
        prev_max = hi
    if prev_max != 100:
        raise ValueError("Last max_soc must be 100 in SOC_RANGES")

# Validation will take place during module import.
validate_soc_ranges()

# -------------------------------
# Grid Setpoint Controller class
# -------------------------------
class GridSetpointController:
    def __init__(self):
        # Connect to the system D-Bus
        self.bus = dbus.SystemBus()

        # Register settings device to allow user override of max feed-in power via D-Bus
        self.settings = SettingsDevice(
            bus=self.bus,
            supportedSettings={
                'maxfeedin': ['/Settings/AutomaticGridSetpoint/MaxGridFeedInPower', -1, -10000, 10000],
            },
            eventCallback=self.handle_setting_change
        )

        # Initial state for discharge hysteresis
        self.discharge_allowed = None

        # Setpoint buffer
        self.last_setpoint = None  # Stores previous grid setpoint for gradual adjustment
        self.setpoint_step = SETPOINT_STEP   # Max change per cycle in watts

        # Power target
        self.power_target = 0.0

        # Schedule update every 5 seconds (5000 ms)
        GLib.timeout_add(5000, self.update_setpoint)

    def handle_setting_change(self, setting, old, new):
        print(f"[Settings] {setting} changed from {old} to {new}")

    def read_value(self, path):
        """
        Read a float value from com.victronenergy.system at the given path.
        Returns 0.0 if not available.
        """
        try:
            obj = self.bus.get_object('com.victronenergy.system', path)
            return float(obj.GetValue(dbus_interface='com.victronenergy.BusItem'))
        except Exception:
            return 0.0

    def write_settings(self, path, value):
        """
        Write a value to a settings path using com.victronenergy.settings API.
        """
        try:
            obj = self.bus.get_object('com.victronenergy.settings', path)
            obj.SetValue(dbus.Double(value), dbus_interface='com.victronenergy.BusItem')
        except Exception as e:
            print(f"Error writing {path}: {e}")

    def write_hub4(self, path, value):
        """
        Write a value to a hub4 path using com.victronenergy.hub4 API.
        """
        try:
            obj = self.bus.get_object('com.victronenergy.hub4', path)
            obj.SetValue(dbus.Double(value), dbus_interface='com.victronenergy.BusItem')
        except Exception as e:
            print(f"Error writing {path}: {e}")

    def in_time_range(self, start_str, end_str, weekday=None):
        """
        Returns True if the current time (in LOCAL_TIMEZONE) is within the given interval.
        If the optional 'weekday' parameter is provided (0=Monday, ..., 6=Sunday),
        it will also check that today matches that weekday.
        """
        # Get the current date and time in the local timezone
        now_dt = datetime.now()
        now_time = now_dt.time()

        # If a weekday is specified, check if today matches it
        if weekday is not None and now_dt.weekday() != weekday:
            return False

        # Convert start and end strings ("HH:MM") to time objects
        start = datetime.strptime(start_str, "%H:%M").time()
        end = datetime.strptime(end_str, "%H:%M").time()

        if start < end:
            # Simple case: interval does not cross midnight
            return start <= now_time < end
        else:
            # Interval crosses midnight (e.g., 22:00–06:00)
            return now_time >= start or now_time < end

    def get_new_power_target(
        self,
        dc_power: float,
        dc_power_target: float,
        min_power_limit: float,
        max_power_limit: float,
    ) -> float:
        """
        Adjust the current power_target one step toward the difference
        between dc_power and dc_power_target, clamped to [min_power_limit, max_power_limit].

        dc_power           - current DC power to the battery
        dc_power_target    - desired DC power into(+)/from(-) battery
        min_power_limit    - minimal inverter power limit
        max_power_limit    - maximum inverter power limit
        """
        # If there is surplus DC power from PV, increase the target but not over max_power_limit
        if dc_power > dc_power_target:
            return min(
                max_power_limit,
                self.power_target + min(dc_power - dc_power_target, self.setpoint_step)
            )
        # If there is a deficit, decrease the target but not below min_power_limit
        else:
            return max(
                min_power_limit,
                self.power_target + max(dc_power - dc_power_target, -self.setpoint_step)
            )

    def update_discharge_limit(self, soc):
        """
        Applies hysteresis logic to control MaxDischargePower based on SoC.
        Discharging is allowed when SoC is above the threshold.
        Once allowed, it remains allowed until SoC drops below (threshold - margin).
        """
        write_change = False
        if self.discharge_allowed is None:
            # Initialize discharge state on first run
            self.discharge_allowed = soc >= DISCHARGE_SOC_LIMIT
            write_change = True

        if self.discharge_allowed:
            # Discharging is currently allowed; disable it only if SoC drops below the lower bound
            if soc < DISCHARGE_SOC_LIMIT - DISCHARGE_HYSTERESIS_MARGIN:
                self.discharge_allowed = False
                write_change = True
        else:
            # Discharging is currently disabled; enable it only if SoC reaches the threshold
            if soc >= DISCHARGE_SOC_LIMIT:
                self.discharge_allowed = True
                write_change = True

                # Reset grid setpoint and power target (for slow ramp-up of power)
                # If DC power is positive (which it usually will be here), power_target will ramp up
                # by self.setpoint_step W per cycle. Otherwise, it will jump straight to min_power_limit.
                #self.power_target = 0.0
                # After the code changes, this value should be already zero or equal to the negated value of the planned charging power.

                # Force recalculation of last_setpoint in this cycle based on current load and offsets
                #self.last_setpoint = None
                # After the code changes, it should be fine, no need to force it.

        if write_change:
            # -1 means unlimited discharge
            value = -1 if self.discharge_allowed else 0
            # Always write to the persistent setting (not the hub4 override).
            # Only the setting triggers the ESS "#7" reason code (discharge disabled
            # by configured limit). The value changes rarely (SoC hysteresis crossing),
            # so flash wear is negligible – unlike the grid setpoint below.
            self.write_settings(MAX_DISCHARGE_PATH, value)

            print(f"[DischargeControl] SoC: {soc:.1f} % → DischargeAllowed: {self.discharge_allowed} → MaxDischargePower: {value}")

    def update_setpoint(self):
        """
        Read SoC and total load, then gradually adjust grid setpoint toward target.
        If DC power is positive (incoming energy), increase the target accordingly.
        """
        # Read current battery SoC
        soc = self.read_value(SOC_PATH)
        # Read current DC power (positive means charging from PV or other source)
        dc_power = self.read_value(DC_POWER_PATH)

        # Update discharge limit based on SoC
        self.update_discharge_limit(soc)

        # Read current load
        load_L1 = self.read_value(ACOUT1_L1_POWER_PATH)
        load_L2 = self.read_value(ACOUT1_L2_POWER_PATH)
        load_L3 = self.read_value(ACOUT1_L3_POWER_PATH)

        total_load = load_L1 + load_L2 + load_L3
        min_power_limit, dc_power_target = get_limits_for_soc(soc)

        # Determine max power limit based on time of day
        # Night mode: from 21:00 to 07:00 → use MAX_NIGHT_DISCHARGE
        # Day mode: otherwise → use MAX_DAY_DISCHARGE
        if self.in_time_range("21:00", "07:00"):
            max_power_limit = MAX_NIGHT_DISCHARGE
        else:
            max_power_limit = MAX_DAY_DISCHARGE

        # Set power target to keep DC power at SCHEDULED_CHARGE_POWER if today is the scheduled charge day
        # and the current time is between 10:00 and 22:00
        if self.in_time_range("10:00", "22:00", weekday=SCHEDULED_CHARGE_DAY):
            mode = "Scheduled charge"
            self.power_target = self.get_new_power_target(
                dc_power = dc_power,
                dc_power_target = SCHEDULED_CHARGE_POWER,
                min_power_limit = - SCHEDULED_CHARGE_POWER * 1.20, # allow 20 % more
                max_power_limit = max_power_limit
            )

        # If discharging is allowed, calculate the power target dynamically
        elif self.discharge_allowed is True:
            mode = "Normal"
            self.power_target = self.get_new_power_target(
                dc_power = dc_power,
                dc_power_target = dc_power_target,
                min_power_limit = min_power_limit,
                max_power_limit = max_power_limit
            )

        # If discharging is not allowed, set power target to zero
        else:
            mode = "No discharge"
            self.power_target = 0.0
        
        # Base target setpoint
        target_setpoint = total_load - min(self.power_target, max_power_limit)

        # Initialize last_setpoint if needed
        if self.last_setpoint is None:
            self.last_setpoint = target_setpoint

        # Gradually move toward target
        delta = target_setpoint - self.last_setpoint
        if abs(delta) <= self.setpoint_step:
            new_setpoint = target_setpoint
        else:
            step = self.setpoint_step if delta > 0 else -self.setpoint_step
            new_setpoint = self.last_setpoint + step

        # Override from D-Bus if available
        override = self.settings['maxfeedin']
        if override != -1:
            new_setpoint = max(-override, new_setpoint)  # Override acts as a ceiling for feed-in (negative value)
            mode += " (MaxGridFeedIn Override - {:.1f} W)".format(override)

        # Update stored value
        self.last_setpoint = new_setpoint

        print(
            f"[{mode}] "
            f"SoC: {soc:5.1f} %, "
            f"L1: {load_L1:7.1f} W, L2: {load_L2:7.1f} W, L3: {load_L3:7.1f} W, "
            f"Total: {total_load:7.1f} W, DC Power: {dc_power:7.1f} W, "
            f"Min Power Limit: {min_power_limit:7.1f} W, Max Power Limit: {max_power_limit:7.1f} W, "
            f"Power Target: {self.power_target:7.1f} W, "
            f"Target: {target_setpoint:7.1f} W, Setpoint: {new_setpoint:7.1f} W"
        )

        if USE_HUB4_OVERRIDES:
            self.write_hub4(GRID_SETPOINT_PATH_OVERRIDE, new_setpoint)
        else:
            self.write_settings(GRID_SETPOINT_PATH, new_setpoint)

        return True  # Keep the timer running

if __name__ == '__main__':
    # Set the local time zone for the entire process
    os.environ['TZ'] = LOCAL_TIMEZONE
    time.tzset()
    # Set GLib as the default main loop for D-Bus
    DBusGMainLoop(set_as_default=True)
    controller = GridSetpointController()
    # Start the GLib main loop
    mainloop = GLib.MainLoop()
    mainloop.run()
