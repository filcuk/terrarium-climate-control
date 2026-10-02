# Terrarium Climate Controller (ESP32-C3)

MicroPython controller that runs a fan on a schedule, with a push button and a small web page for starting and stopping it.

- **Schedule:** the fan runs for a set time at the start of every cycle. By default it runs 5 minutes every hour.
- **Button:** if the fan is on, it stops, and the scheduled run stays off until the next cycle. If the fan is off, it starts an on-demand run (5 minutes by default).
- **Web page:** shows temperature, humidity, fan status, lets you edit the schedule, has a start/stop button that works like the physical one, and shows the last 100 log lines.
- **Sensor:** ASAIR AM2302 / DHT22 for air temperature and relative humidity.



## Pins


| Function    | ESP32-C3 GPIO | Notes                                 |
| ----------- | ------------- | ------------------------------------- |
| Fan drive   | **GPIO5**     | HIGH = fan on (via transistor/MOSFET) |
| Button      | **GPIO4**     | Momentary to GND, internal pull-up    |
| AM2302 data | **GPIO6**     | Temperature / humidity sensor         |


Change `FAN_GPIO` / `BTN_GPIO` / `DHT_GPIO` at the top of `main.py` if you need different pins. Prefer GPIO 3–7; avoid GPIO 8/9 on many C3 boards (boot / USB-JTAG).

## Wiring

![ESP32-C3 wiring diagram](docs/wiring.png)

One USB cable powers both the board and the fan.

### Parts

- ESP32-C3 board
- 5V fan
- 2N2222 transistor
- R1: 1 kΩ resistor (brown-black-red)
- R2: 10 kΩ resistor (brown-black-orange)
- 1N4007 diode. Any 1N4001–1N4007, 1N5819 or 1N4148 also works.
- Momentary push button



### Connections, one at a time

1. **Board 5V** → **fan +** (red wire)
2. **Fan −** → **2N2222 collector (C)**
3. **2N2222 emitter (E)** → **GND**
4. **GPIO5** → **R1 (1 kΩ)** → **2N2222 base (B)**
5. **2N2222 base (B)** → **R2 (10 kΩ)** → **GND**
6. **Diode** across the fan: **striped end → fan +**, plain end → fan −
7. **GPIO4** → **button** → **GND** (on a 4-leg button, use legs that are diagonally opposite)
8. Wire the AM2302 (see below)

Every GND connection goes to the same board GND pin.

### AM2302 / DHT22 temperature & humidity

![AM2302 wiring](docs/am2302-wiring.svg)

This is an **air** temperature and humidity sensor (not soil moisture).

**3-pin breakout module** (labelled VCC / DATA / GND, often already has a pull-up):

```
Board 3V3  ---------- AM2302 VCC
GPIO6      ---------- AM2302 DATA
Board GND  ---------- AM2302 GND
```

**Bare 4-pin AM2302** (plastic grille, four metal pins). With the grille facing you and the pins pointing down, left to right is usually:

```
1 VDD   ---------- Board 3V3
2 DATA  ---------- GPIO6
               \-- 4.7 kΩ or 10 kΩ to 3V3  (pull-up; required on bare sensors)
3 NC    ---------- leave unconnected
4 GND   ---------- Board GND
```

Use **3V3**, not the board 5V pin. Keep the sensor wires reasonably short (under about 1 m). The controller reads the sensor every 10 seconds and writes a climate line into the log every 5 minutes.

**Check the 2N2222's pinout before wiring.** Different makers order the C, B and E legs differently. For example, PN2222A is E-B-C and P2N2222A is C-B-E, read with the flat face toward you and legs pointing down. Look up the datasheet for the part number printed on yours.

If the fan stutters, or the board resets when the fan starts, the fan is drawing more than USB can supply. Power the fan from a separate 5V supply and connect that supply's GND to the board's GND.

## Files on the board


| File           | You create it? | What it is                                                                                                             |
| -------------- | -------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `main.py`      | yes            | The controller. Runs automatically on power-up.                                                                        |
| `wifi.cfg`     | yes            | WiFi credentials. Copy `wifi.cfg.example`, fill it in, and save it on the board as `wifi.cfg`.                         |
| `schedule.cfg` | no             | Created when you save the schedule from the web page. Delete it to go back to the defaults.                            |
| `settings.cfg` | no             | Sensor interval and log size, saved from Advanced on the web page.                                                     |
| `events.log`   | no             | Rolling log (was `fan.log` in earlier versions; renamed automatically). How many lines are kept is set under Advanced. |
| `climate.hist` | no             | Temperature/humidity samples for the chart (1-7 days, set under Advanced).                                             |
| `fan.hist`     | no             | Fan run start/end times, drawn as hatched bands on the chart.                                                          |


`wifi.cfg`:

```
ssid=YourNetworkName
password=YourWifiPassword
hostname=terrarium1
utc_offset_hours=0
```

`hostname` is optional and defaults to `terrarium`. It makes the page available at `http://terrarium1.local/` as well as at the IP address. Use letters, digits and hyphens only, and give each board a different name. `.local` addresses work on Windows 10 and later, macOS, iOS and most Linux systems. Some Android phones don't support them; use the IP address there.

`utc_offset_hours` is also optional. It only shifts the timestamps in the log; the schedule doesn't depend on the clock.

## Flash / run

1. Install [MicroPython for ESP32-C3](https://micropython.org/download/ESP32_GENERIC_C3/).
2. Copy `main.py` and `wifi.cfg` to the board (Thonny, `mpremote`, etc.).
3. Reset the board. Once it's on WiFi, the log shows a line like:
  `WiFi: connected. Device IP: 192.168.1.42  ->  http://192.168.1.42/`
4. Open that address in a browser on the same network.

```bash
mpremote connect auto cp main.py :main.py
mpremote connect auto cp wifi.cfg :wifi.cfg
mpremote connect auto reset
```

If WiFi isn't configured or can't connect, the fan and button still work. The board keeps retrying WiFi in the background, waiting longer between attempts, up to every 10 minutes.

The web page has no password. Anyone on your network can open it.

**Safe mode:** hold the button while the board powers up or resets. The controller and WiFi don't start, so Thonny or mpremote can always connect.

## Behaviour summary


| Event                              | Result                                                       |
| ---------------------------------- | ------------------------------------------------------------ |
| Boot into schedule window          | Fan runs until window ends                                   |
| Button or web button while fan on  | Fan stops; that scheduled run stays off until the next cycle |
| Button or web button while fan off | On-demand run                                                |
| On-demand run ends                 | Fan off until the next scheduled run or button press         |
| Schedule saved on the web page     | Applies immediately and is kept after a reboot               |


