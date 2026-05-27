# venus-ess-automatic-grid-setpoint

A small Python controller for **Victron Venus OS** (Cerbo GX, CCGX, Raspberry Pi, …) that
dynamically adjusts the **ESS grid setpoint** and **maximum discharge power** based on
battery state of charge (SoC), time of day and an optional weekly charging schedule.

It is meant for ESS installations where you want finer, SoC-aware control over how the
battery is discharged into your loads than the built-in ESS scheduler provides.

## Features

- **SoC-based discharge control** with hysteresis to prevent rapid toggling around the threshold.
- **Gradual setpoint ramping** — the grid setpoint moves toward its target in small steps
  instead of jumping, for smoother inverter behaviour.
- **Per-SoC power targets** via a configurable `SOC_RANGES` table, so discharge power scales
  with how full the battery is.
- **Day/night discharge limits** — separate maximum discharge power for daytime and
  night-time (21:00–07:00).
- **Scheduled weekly charging** — charge the battery from the grid on a chosen weekday and
  time window (e.g. to top up before a known high-load day).
- **Runtime feed-in override** via a D-Bus setting (`MaxGridFeedInPower`), adjustable without
  restarting the service.
- **hub4 override mode** — writes setpoint and discharge limit to `com.victronenergy.hub4`
  override paths instead of persistent settings. This **reduces flash wear** on the GX device
  and avoids conflicts with other ESS controllers running on the same system.

## How it works

Every 5 seconds the controller:

1. Reads battery **SoC** and **DC power** from `com.victronenergy.system`.
2. Updates the **discharge allowed** state using hysteresis:
   discharging is enabled once SoC reaches `DISCHARGE_SOC_LIMIT` and stays enabled until SoC
   drops below `DISCHARGE_SOC_LIMIT − DISCHARGE_HYSTERESIS_MARGIN`.
3. Reads the **AC load** on all three phases of the inverter output.
4. Determines a **DC power target** for the current SoC from `SOC_RANGES`.
5. Picks the **maximum discharge power** based on time of day (day vs. night), or switches to
   **scheduled charging** if today/now matches the configured charge window.
6. Computes the target grid setpoint (`total_load − power_target`) and **ramps** the actual
   setpoint toward it by at most `SETPOINT_STEP` watts per cycle.
7. Applies an optional **feed-in ceiling** from the `MaxGridFeedInPower` D-Bus setting.
8. Writes the resulting **setpoint** and **discharge limit** either to the hub4 override paths
   (`USE_HUB4_OVERRIDES = True`, recommended) or to the persistent settings paths.

## Requirements

- Victron **Venus OS** device with **ESS** assistant configured and working.
- Root access to the GX device (SSH).
- Python 3 (already present on Venus OS).
- The bundled `velib_python` from `dbus-systemcalc-py` (already present on Venus OS).

> ⚠️ This controller writes the ESS grid setpoint and discharge limit. Make sure your ESS is
> already configured correctly and understand that an incorrect configuration can cause your
> system to charge/discharge in unexpected ways. **Use at your own risk.**

## Installation

SSH into your GX device as `root` and clone the repository into `/data` (the `/data`
partition survives firmware updates):

```sh
cd /data
git clone https://github.com/Mapiiik/venus-ess-automatic-grid-setpoint.git
cd venus-ess-automatic-grid-setpoint
```

Create your configuration from the template and edit it:

```sh
cp config.py.example config.py
vi config.py
```

Register the service so it starts automatically and is supervised by daemontools:

```sh
sh install.sh
```

`install.sh` creates a symlink in `/opt/victronenergy/service/`, which Venus OS picks up and
keeps running. The service will also restart automatically after a reboot.

> **Note:** `config.py` is git-ignored, so your personal settings won't be overwritten when you
> later `git pull` updates.

## Configuration

All user settings live in **`config.py`** (copied from `config.py.example`). Key options:

| Setting | Description |
|---|---|
| `LOCAL_TIMEZONE` | Timezone used for time-of-day rules, e.g. `'Europe/Prague'`. |
| `USE_HUB4_OVERRIDES` | `True` (recommended) writes to hub4 override paths; `False` writes to persistent settings. |
| `MAX_DAY_DISCHARGE` | Max discharge power during the day (W). |
| `MAX_NIGHT_DISCHARGE` | Max discharge power at night, 21:00–07:00 (W). |
| `DISCHARGE_SOC_LIMIT` | SoC (%) at or above which discharging is allowed. |
| `DISCHARGE_HYSTERESIS_MARGIN` | Hysteresis margin (%) before discharging is disabled again. |
| `SCHEDULED_CHARGE_DAY` | Weekday for scheduled charging (0 = Monday … 6 = Sunday). |
| `SCHEDULED_CHARGE_POWER` | Target charge power on the scheduled day (W). |
| `SETPOINT_STEP` | Max change of the grid setpoint per cycle (W). |
| `SOC_RANGES` | Table mapping SoC (%) to minimum inverter power and DC power target. Must cover 0–100 % contiguously. |

### Runtime feed-in override

The controller registers a D-Bus setting:

```
/Settings/AutomaticGridSetpoint/MaxGridFeedInPower
```

Set it (in watts) to cap how much power is fed into the grid without restarting the service.
A value of `-1` disables the override. For example, to limit feed-in to 2000 W:

```sh
dbus -y com.victronenergy.settings /Settings/AutomaticGridSetpoint/MaxGridFeedInPower SetValue 2000
```

## Logs

The service logs to stdout, captured by the supervisor. To follow the live output:

```sh
tail -f /var/log/venus-ess-automatic-grid-setpoint/current
```

(If your logging is not set up, you can also run `python3 /data/venus-ess-automatic-grid-setpoint/main.py`
manually to see the output directly.)

## Uninstall

```sh
rm /opt/victronenergy/service/venus-ess-automatic-grid-setpoint
rm -rf /data/venus-ess-automatic-grid-setpoint
```

## License

Licensed under the **GNU Affero General Public License v3.0** — see [LICENSE.md](LICENSE.md).

Provided "as is", without warranty of any kind. Use at your own risk.
