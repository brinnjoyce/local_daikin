import logging
import requests
from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature
from homeassistant.components.climate.const import (
    HVACMode,
    SWING_OFF,
    SWING_BOTH,
    SWING_VERTICAL,
    SWING_HORIZONTAL
)
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers.device_registry import format_mac
from typing import Any, List
from dataclasses import dataclass, field
from enum import StrEnum

from homeassistant.helpers.device_registry import DeviceInfo

from datetime import timedelta

SCAN_INTERVAL = timedelta(seconds=60)

# TODO make outside temp another sensor or something? Makes sense right?
# https://github.com/home-assistant/example-custom-config/blob/master/custom_components/example_sensor/sensor.py

_LOGGER = logging.getLogger(__name__)


@dataclass
class DaikinAttribute:
    name: str
    value: float
    path: list[str]
    to: str

    def format(self) -> str:
        return {"pn": self.name, "pv": self.value}

class HAFanMode(StrEnum):
    FAN_QUIET = "Quiet"
    FAN_AUTO = "Auto"
    FAN_LEVEL1 = "Level 1"
    FAN_LEVEL2 = "Level 2"
    FAN_LEVEL3 = "Level 3"
    FAN_LEVEL4 = "Level 4"
    FAN_LEVEL5 = "Level 5"



TURN_OFF_SWING_AXIS = "000000"
TURN_ON_SWING_AXIS = "0F0000"



FAN_MODE_MAP = {
    HAFanMode.FAN_AUTO : "0A00",
    HAFanMode.FAN_QUIET : "0B00",
    HAFanMode.FAN_LEVEL1 : "0300",
    HAFanMode.FAN_LEVEL2 : "0400",
    HAFanMode.FAN_LEVEL3 : "0500",
    HAFanMode.FAN_LEVEL4 : "0600",
    HAFanMode.FAN_LEVEL5 : "0700"
}

# Vertical, horizontal
HVAC_MODE_TO_SWING_ATTR_NAMES = {
    HVACMode.AUTO : ("p_20", "p_21"),
    HVACMode.COOL : ("p_05", "p_06"),
    HVACMode.HEAT : ("p_07", "p_08"),
    HVACMode.FAN_ONLY : ("p_24", "p_25"),
    HVACMode.DRY : ("p_22", "p_23")
}

HVAC_MODE_TO_FAN_SPEED_ATTR_NAME = {
    HVACMode.AUTO : "p_26",
    HVACMode.COOL : "p_09",
    HVACMode.HEAT : "p_0A",
    HVACMode.FAN_ONLY : "p_28",
    # HVACMode.DRY : "dummy" There is no fan mode for dry. It's always automatic.
}


MODE_MAP = {
    "0300" : HVACMode.AUTO,
    "0200" : HVACMode.COOL,
    "0100" : HVACMode.HEAT,
    "0000" : HVACMode.FAN_ONLY,
    "0500" : HVACMode.DRY
}


HVAC_TO_TEMP_HEX = {
    HVACMode.COOL : "p_02",
    HVACMode.HEAT : "p_03",
    HVACMode.AUTO : "p_1D"
}



REVERSE_MODE_MAP = {v: k for k, v in MODE_MAP.items()}
REVERSE_FAN_MODE_MAP = {v: k for k, v in FAN_MODE_MAP.items()}

@dataclass
class DaikinRequest:
    attributes: list[DaikinAttribute] = field(default_factory=list)


    def serialize(self, payload=None) -> dict:
        if payload is None:
            payload = {
                'requests' : []
            }

        def get_existing_index(name: str, children: list[dict]) -> int:
            for index, child in enumerate(children):
                if child.get("pn") == name:
                    return index
            return -1
        
        def get_existing_to(to: str, requests: list[dict]) -> bool:
            for request in requests:
                this_to = request.get("to")
                if this_to == to:
                    return request
            return None

        for attribute in self.attributes:
            to = get_existing_to(attribute.to, payload['requests'])
            if to is None:
                payload['requests'].append({
                    'op': 3,
                    'pc' : {
                        "pn" : "dgc_status",
                        "pch" : []
                    },
                    "to": attribute.to
                })
                to = payload['requests'][-1]
            entry = to['pc']['pch']
            for pn in attribute.path:
                index = get_existing_index(pn, entry)
                if index == -1:
                    entry.append({"pn": pn, "pch": []})
                entry = entry[-1]['pch']
            entry.append(attribute.format())
        return payload

        



async def async_setup_entry(hass, entry, async_add_entities):
    ip = entry.data["ip_address"]
    entity = LocalDaikin(ip)
    await hass.async_add_executor_job(entity.update)
    await entity.initialize_unique_id(hass)

    # 👇 Register entity for use by switch.py
    hass.data["local_daikin"][entry.entry_id]["climate_entity"] = entity

    async_add_entities([entity])

class LocalDaikin(ClimateEntity):
    def __init__(self, ip_address: str):
        self._name = "Local Daikin"
        self.url = f"http://{ip_address}/dsiot/multireq"
        self._hvac_mode = HVACMode.OFF
        self._fan_mode = HAFanMode.FAN_QUIET
        self._swing_mode = SWING_OFF
        self._temperature = None
        self._outside_temperature = None
        self._target_temperature = None
        self._current_temperature = None
        self._current_humidity = None
        self._runtime_today = None
        self._energy_today = None
        self._mac = None
        self._max_temp = 30 # may need some logic to set this based on the device ID
        self._min_temp = 10

        self._ip = ip_address
        self._name = f"Local Daikin ({ip_address})"
        self._attr_unique_id = f"daikin_climate_{ip_address}"
            

        self._attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.AUTO, HVACMode.DRY, HVACMode.FAN_ONLY]
        self._attr_fan_modes = [
            HAFanMode.FAN_QUIET, 
            HAFanMode.FAN_AUTO, 
            HAFanMode.FAN_LEVEL1, 
            HAFanMode.FAN_LEVEL2, 
            HAFanMode.FAN_LEVEL3, 
            HAFanMode.FAN_LEVEL4, 
            HAFanMode.FAN_LEVEL5
        ]
        self._attr_swing_modes = [
            SWING_OFF,
            SWING_BOTH,
            SWING_VERTICAL,
            SWING_HORIZONTAL
        ]
        self._attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.TURN_OFF | ClimateEntityFeature.TURN_ON | ClimateEntityFeature.FAN_MODE | ClimateEntityFeature.SWING_MODE
        self._enable_turn_on_off_backwards_compatibility = False
        

    def set_hvac_mode(self, hvac_mode):
        _LOGGER.info("Set Hvac mode to " + str(hvac_mode))

        if hvac_mode == HVACMode.OFF:
            self.turn_off()
        else:
            new_mode = REVERSE_MODE_MAP.get(hvac_mode)
            if new_mode is None:
                raise Exception(f"Unknown hvac mode {hvac_mode}")
            attribute = DaikinAttribute("p_01", new_mode, ["e_1002", "e_3001"], "/dsiot/edge/adr_0100.dgc_status")

            # Potentially add the turn on attribute here, unsure.
            self.turn_on()
            self.update_attribute(DaikinRequest([attribute]).serialize())


    async def initialize_unique_id(self, hass):
        payload = {
            "requests": [
                {"op": 2, "to": "/dsiot/edge.adp_i"}
            ]
        }
        response = await hass.async_add_executor_job(lambda: requests.post(self.url, json=payload))
        response.raise_for_status()
        data = response.json()
        self._mac = format_mac(self.find_value_by_pn(data, "/dsiot/edge.adp_i", "adp_i", "mac"))

    @property
    def device_info(self):
        return DeviceInfo(
            identifiers={("local_daikin", self._ip)},
            name="Local Daikin AC",
            manufacturer="Daikin",
            model="LAN Adapter",
            sw_version="1.0"
        )

    @property
    def name(self):
        """Return the name of the entity."""
        return self._name

    @property
    def hvac_mode(self):
        """Return current operation."""
        return self._hvac_mode.value

    @property
    def fan_mode(self):
        """Return current operation."""
        return self._fan_mode.value

    @property
    def swing_mode(self):
        return self._swing_mode

    @property
    def fan_modes(self):
        return [mode.value for mode in self._attr_fan_modes]

    @property
    def swing_modes(self):
        return self._attr_swing_modes

    @property
    def hvac_modes(self):
        return [mode.value for mode in self._attr_hvac_modes]

    @property
    def should_poll(self):
        return True

    def set_fan_mode(self, fan_mode: str):
        mode = FAN_MODE_MAP[fan_mode]
        name = HVAC_MODE_TO_FAN_SPEED_ATTR_NAME.get(self.hvac_mode)

        # If in dry mode for example, you cannot set the fan speed. So we ignore anything we cant find in this map.
        if name is not None:
            mode_attr = DaikinAttribute(name, mode, ["e_1002", "e_3001"], "/dsiot/edge/adr_0100.dgc_status")
            self.update_attribute(DaikinRequest([mode_attr]).serialize())
        else:
            self._fan_mode = HAFanMode.FAN_AUTO

    def set_swing_mode(self, swing_mode: str):
        if self.hvac_mode == HVACMode.OFF:
            return
        vertical_axis_command = TURN_OFF_SWING_AXIS if swing_mode in (SWING_OFF, SWING_HORIZONTAL) else TURN_ON_SWING_AXIS
        horizontal_axis_command = TURN_OFF_SWING_AXIS if swing_mode in (SWING_OFF, SWING_VERTICAL) else TURN_ON_SWING_AXIS
        vertical_attr_name, horizontal_attr_name = HVAC_MODE_TO_SWING_ATTR_NAMES[self.hvac_mode]
        self.update_attribute(
            DaikinRequest(
                [
                    DaikinAttribute(horizontal_attr_name, horizontal_axis_command, ["e_1002", "e_3001"], "/dsiot/edge/adr_0100.dgc_status"),
                    DaikinAttribute(vertical_attr_name, vertical_axis_command, ["e_1002", "e_3001"], "/dsiot/edge/adr_0100.dgc_status")
                ]
            ).serialize()
        )


    @property
    def temperature_unit(self):
        """Return the unit of measurement."""
        return UnitOfTemperature.CELSIUS

    # The max and min temps set the upper and lower bounds of the homeassistant climate control.
    # When not set, API errors arise if you request temperatures that are out of bounds
    @property
    def max_temp(self):
        """Return the maximum temperature."""
        return self._max_temp

    @property
    def min_temp(self):
        """Return the minimum temperature."""
        return self._min_temp

    @property
    def target_temperature(self):
        """Return the temperature we try to reach or None in non-settable modes."""
        if self._hvac_mode in (HVACMode.COOL, HVACMode.HEAT, HVACMode.AUTO):
            return self._target_temperature if self._target_temperature is not None else 22.0
        else:
            return None

    @property
    def current_temperature(self):
        return self._current_temperature

    @property
    def extra_state_attributes(self):
        return {
            "outside_temperature": self._outside_temperature,
            "runtime_today": self._runtime_today,
            "energy_today": self._energy_today
        }

    @property
    def unique_id(self):
        return self._mac

    @staticmethod
    def find_value_by_pn(data:dict, fr: str, *keys):
        data = [ x['pc'] for x in data['responses'] if x['fr'] == fr ]

        while keys:
            current_key = keys[0]
            keys = keys[1:]
            found = False
            for pcs in data:
                if pcs['pn'] == current_key:
                    if not keys:
                        return pcs['pv']
                    data = pcs['pch']
                    found = True
                    break
            if not found:
                raise Exception(f'Key {current_key} not found')


    @staticmethod
    def hex_to_temp(value: str, divisor=2) -> float:
        temp = int(value[:2], 16)
        if temp >= 128:
            temp -= 256
        return temp / divisor


    def set_temperature(self, temperature: float, **kwargs):
        _LOGGER.info("Temp change to " + str(temperature) + " requested.")
        attr_name = HVAC_TO_TEMP_HEX.get(self.hvac_mode)
        if attr_name is None:
            _LOGGER.error(f"Cannot set temperature in {self.hvac} mode.")
            return

        temperature_hex = format(int(temperature * 2), '02x') 
        temp_attr = DaikinAttribute(attr_name, temperature_hex, ["e_1002", "e_3001"], "/dsiot/edge/adr_0100.dgc_status")
        self.update_attribute(DaikinRequest([temp_attr]).serialize())

    def update_attribute(self, request: dict, *keys) -> None:
        _LOGGER.info(request)
        response = requests.put(self.url, json=request).json()
        _LOGGER.info(response)
        if response['responses'][0]['rsc'] != 2004:
            raise Exception(f"An exception occured:\n{response}")

        self.update()

    def _update_state(self, state: bool):
        attribute = DaikinAttribute("p_01", "00" if not state else "01", ["e_1002", "e_A002"], "/dsiot/edge/adr_0100.dgc_status")
        self.update_attribute(DaikinRequest([attribute]).serialize())

    def turn_off(self):
        _LOGGER.info("Turned off")
        self._update_state(False)

    def turn_on(self):
        _LOGGER.info("Turned on")
        self._update_state(True)

    def get_swing_state(self, data: dict) -> str:


        # The number of zeros in the response seems strange. Don't have time to work out, so this should work
        vertical_attr_name, horizontal_attr_name = HVAC_MODE_TO_SWING_ATTR_NAMES[self.hvac_mode]
        vertical = "F" in self.find_value_by_pn(data, "/dsiot/edge/adr_0100.dgc_status", "dgc_status", "e_1002", "e_3001", vertical_attr_name)
        horizontal = "F" in self.find_value_by_pn(data, "/dsiot/edge/adr_0100.dgc_status", "dgc_status", "e_1002", "e_3001", horizontal_attr_name)

        if horizontal and vertical:
            return SWING_BOTH
        if horizontal:
            return SWING_HORIZONTAL
        if vertical:
            return SWING_VERTICAL
        
        return SWING_OFF

    @property
    def current_humidity(self) -> int:
        return self._current_humidity


    def update(self):
        """Fetch new state data for the entity."""
        payload = {
            "requests": [
                {"op": 2, "to": "/dsiot/edge/adr_0100.dgc_status?filter=pv,pt,md"},
                {"op": 2, "to": "/dsiot/edge/adr_0200.dgc_status?filter=pv,pt,md"},
                {"op": 2, "to": "/dsiot/edge/adr_0100.i_power.week_power?filter=pv,pt,md"}
            ]
        }

        response = requests.post(self.url, json=payload)
        response.raise_for_status()
        data = response.json()
        _LOGGER.info(data)

        # Set the HVAC mode.
        is_off = self.find_value_by_pn(data, "/dsiot/edge/adr_0100.dgc_status", "dgc_status", "e_1002", "e_A002", "p_01") == "00"
        self._hvac_mode = HVACMode.OFF if is_off else MODE_MAP[self.find_value_by_pn(data, '/dsiot/edge/adr_0100.dgc_status', 'dgc_status', 'e_1002', 'e_3001', 'p_01')]

        self._outside_temperature = self.hex_to_temp(self.find_value_by_pn(data, '/dsiot/edge/adr_0200.dgc_status', 'dgc_status', 'e_1003', 'e_A00D', 'p_01'))

        # Only set the target temperature if this mode allows it. Otherwise, it should be set to none.
        name = HVAC_TO_TEMP_HEX.get(self._hvac_mode)
        if name is not None:
            try:
                self._target_temperature = self.hex_to_temp(self.find_value_by_pn(data, '/dsiot/edge/adr_0100.dgc_status', 'dgc_status', 'e_1002', 'e_3001', name))
            except Exception:
                _LOGGER.warning("No target temperature found, setting fallback.")
                self._target_temperature = 22.0  # default
        else:
            self._target_temperature = None        

        # For some reason, this hex value does not get the 'divide by 2' treatment. My only assumption as to why this might be is because the level of granularity
        # for this temperature is limited to integers. So the passed divisor is 1.
        self._current_temperature = self.hex_to_temp(self.find_value_by_pn(data, '/dsiot/edge/adr_0100.dgc_status', 'dgc_status', 'e_1002', 'e_A00B', 'p_01'), divisor=1)

        # If we cannot find a name for this hvac_mode's fan speed, it is automatic. This is the case for dry.
        fan_mode_key_name = HVAC_MODE_TO_FAN_SPEED_ATTR_NAME.get(self._hvac_mode)
        if fan_mode_key_name is not None:
            hex_value = self.find_value_by_pn(data, "/dsiot/edge/adr_0100.dgc_status", "dgc_status", "e_1002", "e_3001", fan_mode_key_name)
            self._fan_mode = REVERSE_FAN_MODE_MAP[hex_value]
        else:
            self._fan_mode = HAFanMode.FAN_AUTO

        self._current_humidity = int(self.find_value_by_pn(data, '/dsiot/edge/adr_0100.dgc_status', 'dgc_status', 'e_1002', 'e_A00B', 'p_02'), 16)

        if not self.hvac_mode == HVACMode.OFF:
            self._swing_mode = self.get_swing_state(data)
        
        self._energy_today = self.find_value_by_pn(data, '/dsiot/edge/adr_0100.i_power.week_power', 'week_power', 'datas')[-1]
        self._runtime_today = self.find_value_by_pn(data, '/dsiot/edge/adr_0100.i_power.week_power', 'week_power', 'today_runtime')
