#!/usr/bin/python3 -u
# -*- coding: utf-8 -*-

from datetime import datetime
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib
import dbus

# D-Bus paths for reading from com.victronenergy.system
SOC_PATH = '/Dc/Battery/Soc'
DC_POWER_PATH = '/Dc/Battery/Power'
ACOUT1_L1_POWER_PATH = '/Ac/ConsumptionOnOutput/L1/Power'
ACOUT1_L2_POWER_PATH = '/Ac/ConsumptionOnOutput/L2/Power'
ACOUT1_L3_POWER_PATH = '/Ac/ConsumptionOnOutput/L3/Power'

# D-Bus path for writing to com.victronenergy.settings
GRID_SETPOINT_PATH = '/Settings/CGwacs/AcPowerSetPoint'

# Maximal discharge power path
MAX_DISCHARGE_PATH = '/Settings/CGwacs/MaxDischargePower'
# Max discharge power allowed during day (W)
MAX_DAY_DISCHARGE = 4500
# Max discharge power allowed during night (W)
MAX_NIGHT_DISCHARGE = 1600
# Discharging is allowed when SoC is equal to or above this threshold
DISCHARGE_SOC_LIMIT = 85
# Margin for hysteresis to prevent frequent toggling
DISCHARGE_HYSTERESIS_MARGIN = 1

# Mapping: SoC (%) -> offset in watts (min_soc, max_soc, offset)
SOC_RANGES = [
    ( 0,  85,  500),  # 0–85 %
    (86,  86, 1500),  # 86 %
    (87,  87, 2500),  # 87 %
    (88,  88, 3500),  # 88 %
    (89,  89, 4000),  # 89 %
    (90, 100, 4500)   # 90-100 %
]

def get_offset_for_soc(soc_value):
    soc_int = int(soc_value)
    for min_soc, max_soc, offset in SOC_RANGES:
        if min_soc <= soc_int <= max_soc:
            return offset
    return 0

class GridPointController:
    def __init__(self):
        # Connect to the system D-Bus
        self.bus = dbus.SystemBus()

        # Initial state for discharge hysteresis
        self.discharge_allowed = None

        # Setpoint buffer
        self.last_setpoint = None  # Stores previous grid setpoint for gradual adjustment
        self.setpoint_step = 100   # Max change per cycle in watts

        # Power target
        self.power_target = 0

        # Schedule update every 5 seconds (5000 ms)
        GLib.timeout_add(5000, self.update_setpoint)

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

    def write_setting(self, path, value):
        """
        Write a value to a settings path using com.victronenergy.settings API.
        """
        try:
            obj = self.bus.get_object('com.victronenergy.settings', path)
            obj.SetValue(dbus.Double(value), dbus_interface='com.victronenergy.BusItem')
        except Exception as e:
            print(f"Error writing {path}: {e}")

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
                # by self.setpoint_step W per cycle. Otherwise, it will jump straight to soc_offset.
                self.power_target = 0
                # Force recalculation of last_setpoint in this cycle based on current load and offsets
                self.last_setpoint = None

        if write_change:
            # -1 means unlimited discharge
            value = -1 if self.discharge_allowed else 0
            self.write_setting(MAX_DISCHARGE_PATH, value)
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
        soc_offset = get_offset_for_soc(soc)

        now = datetime.now().time()

        # Apply daytime maximal discharge limit
        night_start = datetime.strptime("19:00", "%H:%M").time()
        night_end = datetime.strptime("05:00", "%H:%M").time()

        if now >= night_start or now <= night_end:
            max_power_limit = MAX_NIGHT_DISCHARGE
        else:
            max_power_limit = MAX_DAY_DISCHARGE

        # Apply power boost during surplus from PV
        if dc_power > 0:
            self.power_target = min(max_power_limit, self.power_target + min(dc_power, self.setpoint_step))
        else:
            self.power_target = max(soc_offset, self.power_target + max(dc_power, -self.setpoint_step))

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

        # Update stored value
        self.last_setpoint = new_setpoint

        print(f"SoC: {soc:.1f} %, L1: {load_L1:.1f} W, L2: {load_L2:.1f} W, "
            f"L3: {load_L3:.1f} W, Total: {total_load:.1f} W, DC Power: {dc_power:.1f} W, "
            f"SoC Offset: {soc_offset} W, Power Target: {self.power_target:.1f} W, "
            f"Max Power Limit: {max_power_limit:.1f} W, Target: {target_setpoint:.1f} W, "
            f"Setpoint: {new_setpoint:.1f} W")

        self.write_setting(GRID_SETPOINT_PATH, new_setpoint)
        return True  # Keep the timer running

if __name__ == '__main__':
    # Set GLib as the default main loop for D-Bus
    DBusGMainLoop(set_as_default=True)
    controller = GridPointController()
    # Start the GLib main loop
    mainloop = GLib.MainLoop()
    mainloop.run()
