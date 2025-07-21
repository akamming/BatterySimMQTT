"""
<plugin key="BatterySimMQTT" name="Battery Simulation via MQTT P1" author="your_name" version="1.1.0">
    <description>
        <h2>Battery Simulation Plugin</h2><br/>
        <p>Simulates a home battery system based on real P1 meter data via MQTT.</p>
        <h3>Features</h3>
        <ul style="list-style-type:square">
            <li>Simulates battery charge/discharge to avoid export</li>
            <li>Calculates cost savings for fixed and dynamic energy tariffs</li>
            <li>Uses svalue1-4 from Domoticz MQTT P1 meter output (cumulative Wh)</li>
        </ul>
        <h3>Devices Created</h3>
        <ul style="list-style-type:square">
            <li>Simulated P1 meter (original, no battery)</li>
            <li>Battery State of Charge (%)</li>
            <li>Battery Stored Energy (kWh)</li>
            <li>Battery Flow (kW)</li>
            <li>Virtual P1 meter (with battery)</li>
            <li>Fixed Cost (Real P1)</li>
            <li>Dynamic Cost (Real P1)</li>
            <li>Fixed Cost (Virtual P1)</li>
            <li>Dynamic Cost (Virtual P1)</li>
            <li>Net Saving (Fixed Tariff)</li>
            <li>Net Saving (Dynamic Tariff)</li>
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

def log(msg):
    Domoticz.Log(msg)

def debug(msg):
    if Domoticz._debug_enabled:
        Domoticz.Log("DEBUG: " + str(msg))

class BasePlugin:
    def __init__(self):
        self.mqtt_client = None
        self.config = {}
        self.soc = 0.0
        self.last_time = None
        self.last_totals = None
        self.simulated_meter = 0.0
        self.cost_fixed = 0.0
        self.cost_dynamic = 0.0
        self.cost_fixed_real = 0.0
        self.cost_dynamic_real = 0.0
        self.net_saving_fixed = 0.0
        self.net_saving_dynamic = 0.0
        self.current_dynamic_tariff = 0.0
        self.debug_enabled = False

    def onStart(self):
        log("Starting BatterySimMQTT plugin")

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
            "capacity": float(Parameters["Mode2"]),
            "charge_rate": float(Parameters["Mode3"]),
            "discharge_rate": float(Parameters["Mode4"]),
            "loss": float(Parameters["Mode5"]) / 100.0
        }

        try:
            usage_tariff, feedin_tariff = Parameters["Mode6"].split(",")
            self.config["usage_tariff"] = float(usage_tariff)
            self.config["feedin_tariff"] = float(feedin_tariff)
        except Exception as e:
            Domoticz.Error("Mode6 must be in format 'usage,feedin' €/kWh: " + str(e))
            return

        self.soc = 0.5 * self.config["capacity"]

        # Create devices
        devices = {
            1: ("Simulated P1 Meter", "Electric"),
            2: ("Battery SoC", "Percentage"),
            3: ("Battery Energy", 243, 29),
            4: ("Battery Flow", 243, 8),
            5: ("Virtual P1 Meter with Battery", "Electric"),
            6: ("Fixed Cost (Real P1)", 113, 0, 3),    # Type 113, subtype 0, switchtype 3
            7: ("Dynamic Cost (Real P1)", 113, 0, 3),
            8: ("Fixed Cost (Virtual P1)", 113, 0, 3),
            9: ("Dynamic Cost (Virtual P1)", 113, 0, 3),
            10: ("Net Saving (Fixed Tariff)", 113, 0, 3),
            11: ("Net Saving (Dynamic Tariff)", 113, 0, 3),
        }

        for unit, props in devices.items():
            if unit not in Devices:
                if len(props) == 2:
                    # Naam + TypeName
                    Domoticz.Device(Name=props[0], Unit=unit, TypeName=props[1]).Create()
                elif len(props) == 3:
                    # Naam + Type + Subtype
                    Domoticz.Device(Name=props[0], Unit=unit, Type=props[1], Subtype=props[2]).Create()
                elif len(props) == 4:
                    # Naam + Type + Subtype + Switchtype
                    Domoticz.Device(Name=props[0], Unit=unit, Type=props[1], Subtype=props[2], Switchtype=props[3]).Create()
                else:
                    Domoticz.Error(f"Device tuple onjuist formaat voor unit {unit}: {props}")


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
        now = datetime.now()
        delta_t = (now - self.last_time).total_seconds() if self.last_time else 10.0
        self.last_time = now

        try:
            payload = json.loads(msg.payload.decode())
        except Exception as e:
            Domoticz.Error("Invalid JSON payload: " + str(e))
            return

        if msg.topic == self.config["dyn_topic"]:
            try:
                self.current_dynamic_tariff = float(payload["svalue1"])
                debug(f"Updated dynamic tariff: {self.current_dynamic_tariff}")
            except:
                Domoticz.Error("Invalid dynamic tariff value in svalue1")

        elif msg.topic == self.config["p1_topic"]:
            try:
                current = {
                    "use_high": int(payload["svalue1"]),
                    "use_low": int(payload["svalue2"]),
                    "ret_high": int(payload["svalue3"]),
                    "ret_low": int(payload["svalue4"])
                }

                debug(f"P1 data: {current}")

                total_wh = current["use_high"] + current["use_low"] - current["ret_high"] - current["ret_low"]

                if self.last_totals:
                    last_total_wh = (
                        self.last_totals["use_high"] +
                        self.last_totals["use_low"] -
                        self.last_totals["ret_high"] -
                        self.last_totals["ret_low"]
                    )
                    delta_wh = total_wh - last_total_wh

                    # Kosten berekenen voor real P1
                    real_kwh = delta_wh / 1000.0
                    self.cost_fixed_real += real_kwh * (self.config["usage_tariff"] if delta_wh > 0 else self.config["feedin_tariff"])
                    self.cost_dynamic_real += real_kwh * self.current_dynamic_tariff

                    cap_wh = self.config["capacity"] * 1000
                    loss_factor = self.config["loss"]
                    max_charge_wh = self.config["charge_rate"] * (delta_t / 3600.0) * 1000
                    max_discharge_wh = self.config["discharge_rate"] * (delta_t / 3600.0) * 1000

                    if delta_wh > 0:
                        wh_to_charge = delta_wh * (1 - loss_factor)
                        wh_to_charge = min(wh_to_charge, max_charge_wh)
                        wh_available_space = cap_wh - self.soc * 1000
                        wh_charged = min(wh_to_charge, wh_available_space)

                        new_soc_wh = self.soc * 1000 + wh_charged
                        battery_flow_wh = wh_charged
                    else:
                        wh_to_discharge = -delta_wh
                        wh_to_discharge = min(wh_to_discharge, max_discharge_wh)
                        wh_available = self.soc * 1000
                        wh_discharged = min(wh_to_discharge, wh_available)

                        new_soc_wh = self.soc * 1000 - wh_discharged
                        battery_flow_wh = -wh_discharged

                    delta_soc_kwh = (new_soc_wh - self.soc * 1000) / 1000.0
                    self.soc = new_soc_wh / 1000.0

                    self.simulated_meter += delta_wh / 1000.0

                    # Kosten op simulatie
                    self.cost_fixed += (delta_wh / 1000.0) * (self.config["usage_tariff"] if delta_wh > 0 else self.config["feedin_tariff"])
                    self.cost_dynamic += (delta_wh / 1000.0) * self.current_dynamic_tariff

                    power_kw = battery_flow_wh / 1000.0 / (delta_t / 3600.0)

                    # Netto besparing
                    self.net_saving_fixed = self.cost_fixed_real - self.cost_fixed
                    self.net_saving_dynamic = self.cost_dynamic_real - self.cost_dynamic

                    # Update devices
                    Devices[1].Update(0, str(round(total_wh / 1000.0, 3)))
                    Devices[2].Update(0, str(int(100 * self.soc / self.config["capacity"])))
                    Devices[3].Update(0, str(round(self.soc, 3)))
                    Devices[4].Update(0, str(round(power_kw, 2)))
                    Devices[5].Update(0, str(round(self.simulated_meter, 3)))
                    Devices[6].Update(0, str(int(round(self.cost_fixed_real * 100))))      # euro naar centen
                    Devices[7].Update(0, str(int(round(self.cost_dynamic_real * 100))))
                    Devices[8].Update(0, str(int(round(self.cost_fixed * 100))))
                    Devices[9].Update(0, str(int(round(self.cost_dynamic * 100))))
                    Devices[10].Update(0, str(int(round(self.net_saving_fixed * 100))))
                    Devices[11].Update(0, str(int(round(self.net_saving_dynamic * 100))))

                    log(f"P1 update: ΔSoC={delta_soc_kwh:.3f} kWh, Battery flow={power_kw:.3f} kW")

                self.last_totals = current

            except Exception as e:
                Domoticz.Error("Error processing P1 data: " + str(e))

    def onHeartbeat(self):
        pass

global _plugin
_plugin = BasePlugin()

def onStart():
    _plugin.onStart()

def onStop():
    pass

def onConnect(status, description):
    pass

def onMessage(Unit, Topic, Payload, QoS):
    pass

def onNotification(Name, Subject, Text, Status, Priority, Sound, ImageFile):
    pass

def onCommand(Unit, Command, Level, Color):
    pass

def onHeartbeat():
    _plugin.onHeartbeat()

