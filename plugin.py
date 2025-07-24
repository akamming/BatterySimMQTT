"""
<plugin key="BatterySimMQTT" name="Battery Simulation via MQTT P1" author="your_name" version="1.0.4">
    <description>
        <h2>Battery Simulation Plugin</h2><br/>
        <p>Simuleert een thuisbatterijsysteem op basis van real-time P1 meterdata via MQTT.</p>
        <h3>Features</h3>
        <ul style="list-style-type:square">
            <li>Simuleert batterij opladen/ontladen om export te vermijden</li>
            <li>Berekening van kostenbesparingen voor vaste en dynamische energietarieven</li>
            <li>Gebruikt svalue1-4 van Domoticz MQTT P1 meter output (cumulatieve Wh)</li>
        </ul>
        <h3>Aangemaakte Apparaten</h3>
        <ul style="list-style-type:square">
            <li>Gesimuleerde P1 meter (met batterij)</li>
            <li>Batterij Laadtoestand (%)</li>
            <li>Batterij Opgeslagen Energie (Wh)</li>
            <li>Batterij Stroom (W)</li>
            <li>Kosten (vast tarief)</li>
            <li>Kosten (dynamisch tarief)</li>
        </ul>
    </description>
    <params>
        <param field="Address" label="MQTT Server IP" width="200px" required="true" default="127.0.0.1"/>
        <param field="Port" label="MQTT Port" width="60px" required="true" default="1883"/>
        <param field="Username" label="MQTT Username" width="150px" required="false"/>
        <param field="Password" label="MQTT Password" width="150px" required="false" password="true"/>
        <param field="Mode1" label="Device IDXs (P1, Tariff)" width="150px" required="true"/>
        <param field="Mode2" label="Battery Capacity (kWh)" width="100px" required="true" default="5.0"/>
        <param field="Mode3" label="Charge Rate (kW)" width="100px" required="true" default="2.5"/>
        <param field="Mode4" label="Discharge Rate (kW)" width="100px" required="true" default="2.5"/>
        <param field="Mode5" label="Loss Factor (%)" width="100px" required="true" default="7"/>
        <param field="Mode6" label="Tariffs (usage,feed-in) €/kWh" width="150px" required="true" default="0.22,0.08"/>
    </params>
</plugin>
"""

import Domoticz
import json
import paho.mqtt.client as mqtt
from datetime import datetime
import os

MAXUPDATEINTERVAL = 60  # Maximum update interval in seconds

# Device indices
SIMULATED_P1_METER = 1
BATTERY_SOC = 2
BATTERY_ENERGY = 3
COST_FIXED = 5
COST_DYNAMIC = 6

def log(msg):
    Domoticz.Log(msg)

def debug(msg):
    if Domoticz._debug_enabled:
        Domoticz.Log("DEBUG: "+str(msg))

def error(msg):
    Domoticz.Error("ERROR: "+str(msg))

def TimeElapsedSinceLastUpdate(last_update):
    from datetime import datetime
    try:
        return datetime.now() - datetime.strptime(last_update, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now() - datetime.now()  # 0 seconden

def UpdateElectricCounterSensor(idx, name, power, kwh):
    debug(f"Updating ElectricCounterSensor: idx={idx}, name={name}, power={power}, kwh={kwh}")
    value = f"{power:.6f};{kwh:.6f}"
    if idx not in Devices:
        Domoticz.Device(Name=name, Unit=idx, Type=243, Subtype=29, Used=1).Create()
    try:
        device = Devices[idx]
        if (device.sValue != str(value) or 
            TimeElapsedSinceLastUpdate(device.LastUpdate).total_seconds() > MAXUPDATEINTERVAL):
            device.Update(nValue=0, sValue=str(value))
            Domoticz.Log(f"ElectricCounterSensor ({device.Name}) updated: {value}")
        else:
            debug(f"Not updating ElectricCounterSensor ({device.Name})")
    except KeyError:
        Domoticz.Error(f"Unable to update ElectricCounterSensor ({name}), is 'accept new devices' aan?")

def UpdateBatterySocSensor(idx, name, value):
    # Type=25, Subtype=0 (Percentage)
    if idx not in Devices:
        Domoticz.Device(Name=name, Unit=idx, Type=25, Subtype=0, Used=1).Create()
    try:
        device = Devices[idx]
        if (device.sValue != str(value) or 
            TimeElapsedSinceLastUpdate(device.LastUpdate).total_seconds() > MAXUPDATEINTERVAL):
            device.Update(nValue=0, sValue=str(value))
            Domoticz.Log(f"BatterySocSensor ({device.Name}) updated: {value}")
        else:
            debug(f"Not updating BatterySocSensor ({device.Name})")
    except KeyError:
        Domoticz.Error(f"Unable to update BatterySocSensor ({name}), is 'accept new devices' aan?")

def UpdateCostSensor(idx, name, value):
    # Type=113, Subtype=0, Switchtype=3 (General kWh)
    if idx not in Devices:
        Domoticz.Device(Name=name, Unit=idx, Type=113, Subtype=0, Switchtype=3, Used=1).Create()
    try:
        device = Devices[idx]
        if (device.sValue != str(value) or 
            TimeElapsedSinceLastUpdate(device.LastUpdate).total_seconds() > MAXUPDATEINTERVAL):
            device.Update(nValue=0, sValue=str(value))
            Domoticz.Log(f"CostSensor ({device.Name}) updated: {value}")
        else:
            debug(f"Not updating CostSensor ({device.Name})")
    except KeyError:
        Domoticz.Error(f"Unable to update CostSensor ({name}), is 'accept new devices' aan?")

def GetEnergyDeviceValues(idx):
    """
    Get the current power and energy values from an electric counter sensor.
    Returns a tuple (power, energy).
    """
    if idx not in Devices:
        Domoticz.Error(f"Device with Unit {idx} not found")
        return 0, 0

    device = Devices[idx]
    try:
        power, energy = map(float, device.sValue.split(";"))
        debug(f"Retrieved values from device {device.Name}: power={power}, energy={energy}")
        return power, energy
    except ValueError:
        Domoticz.Error(f"Invalid sValue format for device {device.Name}: {device.sValue}")
        return 0, 0

class BasePlugin:
    def __init__(self):
        self.mqtt_client = None
        self.config = {}
        self.last_totals = None
        self.soc = 0.0
        self.last_time = None
        self.simulated_meter_energy = 0.0
        self.simulated_meter_power = 0

        self.cost_fixed_real = 0.0
        self.cost_dynamic_real = 0.0

        self.current_dynamic_tariff = 0.0
        self.debug_enabled = False
        self.devices_def = {}

    def dev_remove_all_devices(self):
        log("Verwijderen van alle apparaten...")
        for unit in list(Devices.keys()):
            try:
                name = Devices[unit].Name
                Devices[unit].Delete()
                log(f"Apparaat met Unit {unit} en naam {name} verwijderd")
            except Exception as e:
                Domoticz.Error(f"Fout bij verwijderen apparaat met Unit {unit}: {e}")

    def onStart(self):
        log("Starting BatterySimMQTT plugin")

        self.dev_remove_all_devices()
        plugin_dir = os.path.dirname(__file__)
        debug_file = os.path.join(plugin_dir, "DEBUG")
        if os.path.isfile(debug_file):
            Domoticz._debug_enabled = True
            self.debug_enabled = True
            log("DEBUG mode enabled via DEBUG file")
        else:
            Domoticz._debug_enabled = False

        try:
            p1_idx, tariff_idx = Parameters["Mode1"].split(",")
            p1_topic = f"domoticz/out/{p1_idx.strip()}"
            dyn_topic = f"domoticz/out/{tariff_idx.strip()}"
        except Exception as e:
            Domoticz.Error("Mode1 must be in format 'p1_idx,tariff_idx': " + str(e))
            return

        self.config = {
            "mqtt_host": Parameters["Address"],
            "mqtt_port": int(Parameters["Port"]),
            "mqtt_user": Parameters["Username"],
            "mqtt_pass": Parameters["Password"],
            "p1_topic": p1_topic,
            "dyn_topic": dyn_topic,
            "capacity": float(Parameters["Mode2"]) * 1000,  # omzetten naar Wh
            "charge_rate": float(Parameters["Mode3"]) * 1000,  # omzetten naar W
            "discharge_rate": float(Parameters["Mode4"]) * 1000,  # omzetten naar W
            "loss": float(Parameters["Mode5"]) / 100.0
        }

        try:
            usage_tariff, feedin_tariff = Parameters["Mode6"].split(",")
            self.config["usage_tariff"] = float(usage_tariff) / 1000.0  # omzetten naar €/Wh
            self.config["feedin_tariff"] = float(feedin_tariff) / 1000.0  # omzetten naar €/Wh
        except Exception as e:
            Domoticz.Error("Mode6 must be in format 'usage,feedin' €/kWh: " + str(e))
            return

        self.connect_mqtt()

    def connect_mqtt(self):
        try:
            debug("Preparing MQTT client...")

            self.mqtt_client = mqtt.Client()

            if self.config["mqtt_user"] or self.config["mqtt_pass"]:
                self.mqtt_client.username_pw_set(self.config["mqtt_user"], self.config["mqtt_pass"])
                debug(f"MQTT credentials set: username='{self.config['mqtt_user']}'")

            self.mqtt_client.on_connect = self.on_mqtt_connect
            self.mqtt_client.on_message = self.on_mqtt_message

            debug(f"Attempting MQTT connection to {self.config['mqtt_host']}:{self.config['mqtt_port']}")
            self.mqtt_client.connect(
                self.config["mqtt_host"],
                self.config["mqtt_port"],
                keepalive=60
            )

            self.mqtt_client.loop_start()
            log("MQTT client connection initiated")

        except Exception as e:
            Domoticz.Error("MQTT connection failed: " + str(e))
            debug(f"MQTT parameters used: host={self.config['mqtt_host']}, port={self.config['mqtt_port']}, user={self.config['mqtt_user']}")

    def on_mqtt_connect(self, client, userdata, flags, rc):
        log("Connected to MQTT broker")
        client.subscribe(self.config["p1_topic"])
        client.subscribe(self.config["dyn_topic"])
        debug(f"Subscribed to topics: {self.config['p1_topic']}, {self.config['dyn_topic']}")

    def on_mqtt_message(self, client, userdata, msg):
        #calculate time delta
        now = datetime.now()
        delta_t = int((now - self.last_time).total_seconds()) if self.last_time else 10
        self.last_time = now

        # Log and process the received MQTT message
        debug(f"Received MQTT message on topic: {msg.topic}, payload: {msg.payload.decode()}")
        try:
            payload = json.loads(msg.payload.decode())
        except Exception as e:
            Domoticz.Error("Invalid JSON payload: " + str(e))
            return

        if msg.topic == self.config["dyn_topic"]:
            try:
                self.current_dynamic_tariff = float(payload["svalue1"]) / 1000.0  # omzetten naar €/Wh
                debug(f"Updated dynamic tariff: {self.current_dynamic_tariff:.6f} €/Wh")
            except Exception:
                Domoticz.Error("Invalid dynamic tariff value in svalue1")

        elif msg.topic == self.config["p1_topic"]:
            try:
                current_p1 = {
                    "use_high": int(payload["svalue1"]),
                    "use_low": int(payload["svalue2"]),
                    "ret_high": int(payload["svalue3"]),
                    "ret_low": int(payload["svalue4"]),
                }
            except Exception as e:
                Domoticz.Error("Invalid P1 values: " + str(e))
                return

            # Get current device values
            current_simulated_p1_power, current_simulated_p1_energy = GetEnergyDeviceValues(SIMULATED_P1_METER)

            total_usage_wh = current_p1["use_high"] + current_p1["use_low"]
            total_return_wh = current_p1["ret_high"] + current_p1["ret_low"]
            debug(f"P1 totals: usage_wh={total_usage_wh}, return_wh={total_return_wh}")

            if self.last_totals is None:
                self.last_totals = current_p1
                debug("First P1 message, initializing totals")
                return

            delta_usage = total_usage_wh - (self.last_totals["use_high"] + self.last_totals["use_low"])
            delta_return = total_return_wh - (self.last_totals["ret_high"] + self.last_totals["ret_low"])
            self.last_totals = current_p1
            debug(f"Delta usage Wh={delta_usage}, Delta return Wh={delta_return}")

            net_power = (delta_usage - delta_return) / delta_t
            debug(f"Net power (W) = {net_power:.6f}")

            max_charge = self.config["charge_rate"] * (delta_t / 3600.0)
            max_discharge = self.config["discharge_rate"] * (delta_t / 3600.0)

            debug(f"Max charge (Wh) = {max_charge:.6f}, Max discharge (Wh) = {max_discharge:.6f}")

            charge = 0.0
            discharge = 0.0
            net_charge = 0.0
            net_discharge = 0.0

            if net_power > 0:
                charge = min(net_power, max_charge, self.config["capacity"] - self.soc)
                net_charge *= (1 - self.config["loss"])
                self.soc += charge
            else:
                discharge = min(-net_power, max_discharge, self.soc)
                net_discharge /= (1 - self.config["loss"])
                self.soc -= discharge

            battery_power = charge - discharge
            debug(f"Charge/Discharge (W) = {charge - discharge:.6f} W")
            debug(f"Net Charge/Discharge (Wh) = {charge - discharge:.6f}, SoC = {self.soc:.6f} W")

            self.soc = max(0.0, min(self.soc, self.config["capacity"]))
            new_simulated_p1_energy = current_simulated_p1_energy + (charge - discharge)
            current_simulated_p1_power = (new_simulated_p1_energy - current_simulated_p1_energy) / delta_t * 3600.0
            current_simulated_p1_energy = new_simulated_p1_energy

            # Kostenberekening (alle op basis van delta’s)
            cost_fixed = delta_usage * self.config["usage_tariff"] - delta_return * self.config["feedin_tariff"]
            cost_dynamic = (delta_usage - delta_return) * self.current_dynamic_tariff

            self.cost_fixed_real += cost_fixed
            self.cost_dynamic_real += cost_dynamic

            # Debug: Kosten
            debug(f"Fixed Cost Calculation: ({delta_usage} * {self.config['usage_tariff']} - {delta_return} * {self.config['feedin_tariff']}) = {cost_fixed:.6f}")
            debug(f"Dynamic Cost Calculation: ({delta_usage} - {delta_return}) * {self.current_dynamic_tariff} = {cost_dynamic:.6f}")

            UpdateElectricCounterSensor(SIMULATED_P1_METER, "Simulated P1 Meter", current_simulated_p1_power, current_simulated_p1_energy)
            UpdateBatterySocSensor(BATTERY_SOC, "Battery SoC", f"{(self.soc / self.config['capacity']) * 100:.6f}")
            UpdateElectricCounterSensor(BATTERY_ENERGY, "Battery Energy", battery_power, self.soc)
            UpdateCostSensor(COST_FIXED, "Cost Fixed Tariff", f"{self.cost_fixed_real * 100:.6f}")
            UpdateCostSensor(COST_DYNAMIC, "Cost Dynamic Tariff", f"{self.cost_dynamic_real * 100:.6f}")

    def onHeartbeat(self):
        pass

    def onStop(self):
        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
            log("MQTT client disconnected")

global _plugin
_plugin = BasePlugin()

def onStart():
    _plugin.onStart()

def onStop():
    _plugin.onStop()

def onHeartbeat():
    _plugin.onHeartbeat()

def onMQTTMessage(topic, payload, qos, retained):
    pass

def onCommand(Unit, Command, Level, Hue):
    pass
