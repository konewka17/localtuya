"""Platform to locally control Tuya-based vacuum devices."""
import base64
import binascii
import json
import logging
import time
from functools import partial

import voluptuous as vol
from homeassistant.components.vacuum import (
    DOMAIN,
    STATE_CLEANING,
    STATE_DOCKED,
    STATE_ERROR,
    STATE_IDLE,
    STATE_PAUSED,
    STATE_RETURNING,
    StateVacuumEntity,
    VacuumEntityFeature,
)

from .common import LocalTuyaEntity, async_setup_entry
from .const import (
    CONF_BATTERY_DP,
    CONF_CLEAN_AREA_DP,
    CONF_CLEAN_RECORD_DP,
    CONF_CLEAN_TIME_DP,
    CONF_DOCKED_STATUS_VALUE,
    CONF_FAN_SPEED_DP,
    CONF_FAN_SPEEDS,
    CONF_FAULT_DP,
    CONF_IDLE_STATUS_VALUE,
    CONF_LOCATE_DP,
    CONF_MODE_DP,
    CONF_MODES,
    CONF_PAUSED_STATE,
    CONF_POWERGO_DP,
    CONF_RETURN_MODE,
    CONF_RETURNING_STATUS_VALUE,
    CONF_STOP_STATUS,
    CONF_POSITION_BASE64_DP,
    CONF_POSITION_RELATIVE_SCALE,
    CONF_POSITION_RELATIVE_ORIGIN,
    CONF_POSITION_AXIS_ROTATION,
)

_LOGGER = logging.getLogger(__name__)

CLEAN_TIME = "clean_time"
CLEAN_AREA = "clean_area"
CLEAN_RECORD = "clean_record"
MODES_LIST = "cleaning_mode_list"
MODE = "cleaning_mode"
FAULT = "fault"
POSITION = "position"
PATH = "path"
RELATIVE_POSITION = "relative_position"

DEFAULT_IDLE_STATUS = "standby,sleep"
DEFAULT_RETURNING_STATUS = "docking"
DEFAULT_DOCKED_STATUS = "charging,chargecompleted"
DEFAULT_MODES = "smart,wall_follow,spiral,single"
DEFAULT_FAN_SPEEDS = "low,normal,high"
DEFAULT_PAUSED_STATE = "paused"
DEFAULT_RETURN_MODE = "chargego"
DEFAULT_STOP_STATUS = "standby"


def flow_schema(dps):
    """Return schema used in config flow."""
    return {
        vol.Required(CONF_IDLE_STATUS_VALUE, default=DEFAULT_IDLE_STATUS): str,
        vol.Required(CONF_POWERGO_DP): vol.In(dps),
        vol.Required(CONF_DOCKED_STATUS_VALUE, default=DEFAULT_DOCKED_STATUS): str,
        vol.Optional(
            CONF_RETURNING_STATUS_VALUE, default=DEFAULT_RETURNING_STATUS
        ): str,
        vol.Optional(CONF_BATTERY_DP): vol.In(dps),
        vol.Optional(CONF_MODE_DP): vol.In(dps),
        vol.Optional(CONF_MODES, default=DEFAULT_MODES): str,
        vol.Optional(CONF_RETURN_MODE, default=DEFAULT_RETURN_MODE): str,
        vol.Optional(CONF_FAN_SPEED_DP): vol.In(dps),
        vol.Optional(CONF_FAN_SPEEDS, default=DEFAULT_FAN_SPEEDS): str,
        vol.Optional(CONF_CLEAN_TIME_DP): vol.In(dps),
        vol.Optional(CONF_CLEAN_AREA_DP): vol.In(dps),
        vol.Optional(CONF_CLEAN_RECORD_DP): vol.In(dps),
        vol.Optional(CONF_LOCATE_DP): vol.In(dps),
        vol.Optional(CONF_FAULT_DP): vol.In(dps),
        vol.Optional(CONF_PAUSED_STATE, default=DEFAULT_PAUSED_STATE): str,
        vol.Optional(CONF_STOP_STATUS, default=DEFAULT_STOP_STATUS): str,
        vol.Optional(CONF_POSITION_BASE64_DP): vol.In(dps),
        vol.Optional(CONF_POSITION_RELATIVE_SCALE): float,
        vol.Optional(CONF_POSITION_RELATIVE_ORIGIN): str,
        vol.Optional(CONF_POSITION_AXIS_ROTATION): int,
    }


class LocaltuyaVacuum(LocalTuyaEntity, StateVacuumEntity):
    """Tuya vacuum device."""

    def __init__(self, device, config_entry, switchid, **kwargs):
        """Initialize a new LocaltuyaVacuum."""
        super().__init__(device, config_entry, switchid, _LOGGER, **kwargs)
        self._state = None
        self._battery_level = None
        self._attrs = {}

        self._idle_status_list = []
        if self.has_config(CONF_IDLE_STATUS_VALUE):
            self._idle_status_list = self._config[CONF_IDLE_STATUS_VALUE].split(",")

        self._modes_list = []
        if self.has_config(CONF_MODES):
            self._modes_list = self._config[CONF_MODES].split(",")
            self._attrs[MODES_LIST] = self._modes_list

        self._docked_status_list = []
        if self.has_config(CONF_DOCKED_STATUS_VALUE):
            self._docked_status_list = self._config[CONF_DOCKED_STATUS_VALUE].split(",")

        self._fan_speed_list = []
        if self.has_config(CONF_FAN_SPEEDS):
            self._fan_speed_list = self._config[CONF_FAN_SPEEDS].split(",")

        self._attrs[PATH] = []

        self._position_relative_scale = 1
        if self.has_config(CONF_POSITION_RELATIVE_SCALE):
            self._position_relative_scale = self._config[CONF_POSITION_RELATIVE_SCALE]

        self._position_relative_origin = [0, 0]
        if self.has_config(CONF_POSITION_RELATIVE_ORIGIN):
            self._position_relative_origin = json.loads(self._config[CONF_POSITION_RELATIVE_ORIGIN])

        self._position_axis_rotation = 0
        if self.has_config(CONF_POSITION_AXIS_ROTATION):
            self._position_axis_rotation = self._config[CONF_POSITION_AXIS_ROTATION]

        self._fan_speed = ""
        self._cleaning_mode = ""
        _LOGGER.debug("Initialized vacuum [%s]", self.name)

    @property
    def supported_features(self):
        """Flag supported features."""
        supported_features = (
                VacuumEntityFeature.START
                | VacuumEntityFeature.PAUSE
                | VacuumEntityFeature.STOP
                | VacuumEntityFeature.STATUS
                | VacuumEntityFeature.STATE
                | VacuumEntityFeature.SEND_COMMAND
        )

        if self.has_config(CONF_RETURN_MODE):
            supported_features = supported_features | VacuumEntityFeature.RETURN_HOME
        if self.has_config(CONF_FAN_SPEED_DP):
            supported_features = supported_features | VacuumEntityFeature.FAN_SPEED
        if self.has_config(CONF_BATTERY_DP):
            supported_features = supported_features | VacuumEntityFeature.BATTERY
        if self.has_config(CONF_LOCATE_DP):
            supported_features = supported_features | VacuumEntityFeature.LOCATE

        return supported_features

    @property
    def state(self):
        """Return the vacuum state."""
        return self._state

    @property
    def battery_level(self):
        """Return the current battery level."""
        return self._battery_level

    @property
    def extra_state_attributes(self):
        """Return the specific state attributes of this vacuum cleaner."""
        return self._attrs

    @property
    def fan_speed(self):
        """Return the current fan speed."""
        return self._fan_speed

    @property
    def fan_speed_list(self) -> list:
        """Return the list of available fan speeds."""
        return self._fan_speed_list

    async def async_start(self, **kwargs):
        """Turn the vacuum on and start cleaning."""
        await self._device.set_dp(True, self._config[CONF_POWERGO_DP])

    async def async_pause(self, **kwargs):
        """Stop the vacuum cleaner, do not return to base."""
        await self._device.set_dp(False, self._config[CONF_POWERGO_DP])

    async def async_return_to_base(self, **kwargs):
        """Set the vacuum cleaner to return to the dock."""
        if self.has_config(CONF_RETURN_MODE):
            await self._device.set_dp(
                self._config[CONF_RETURN_MODE], self._config[CONF_MODE_DP]
            )
        else:
            _LOGGER.error("Missing command for return home in commands set.")

    async def async_stop(self, **kwargs):
        """Turn the vacuum off stopping the cleaning."""
        # Perform pause action instead of stop, added myself
        await self._device.set_dp(False, self._config[CONF_POWERGO_DP])

    async def async_clean_spot(self, **kwargs):
        """Perform a spot clean-up."""
        return None

    async def async_locate(self, **kwargs):
        """Locate the vacuum cleaner."""
        if self.has_config(CONF_LOCATE_DP):
            await self._device.set_dp("", self._config[CONF_LOCATE_DP])

    async def async_set_fan_speed(self, fan_speed, **kwargs):
        """Set the fan speed."""
        await self._device.set_dp(fan_speed, self._config[CONF_FAN_SPEED_DP])

    def get_command_params_clean(self, vertices, map_id):
        return {'dInfo': {'ts': int(time.time() * 1000), 'userId': '0'}, 'data': {'cmds': [
            {'data': {'cleanId': [-3], 'extraAreas': [
                {"active": "depth", "id": 100, "mode": "point", "name": "aa", "tag": "room",
                 "vertexs": vertices}], 'mapId': map_id, 'segmentId': []}, 'infoType': 21023},
            {'data': {'mode': 'reAppointClean'}, 'infoType': 21005}], 'mainCmds': [21005]}, 'infoType': 30000,
                'message': 'ok'}

    async def async_send_command(self, command, params=None, **kwargs):
        """Send a command to a vacuum cleaner."""
        if params is None:
            params = {}

        if command == "set_mode" and "mode" in params:
            mode = params["mode"]
            await self._device.set_dp(mode, self._config[CONF_MODE_DP])
        elif command == "clean_room":
            room_id = params.get("room", 4)
            map_id = params.get("map_id", 1695662532)
            command_params = {'dInfo': {'ts': int(time.time() * 1000), 'userId': '0'}, 'data': {'cmds': [
                {'data': {'cleanId': [-3], 'extraAreas': [], 'mapId': map_id, 'segmentId': [room_id]},
                 'infoType': 21023}, {'data': {'mode': 'reAppointClean'}, 'infoType': 21005}], 'mainCmds': [21005]},
                              'infoType': 30000, 'message': 'ok'}
            base64_string = base64.b64encode(json.dumps(command_params).encode('utf-8')).decode('utf-8')
            await self._device.set_dp(base64_string, 127)
        elif command == "clean_spot":
            x = params.get("x", .5)
            y = params.get("y", .5)
            size = params.get("size", 300)
            x, y = self.calculate_absolute_position(x, y)
            _LOGGER.info(f"Absolute position: {x, y}")

            map_id = params.get("map_id", 1695662532)
            command_params = self.get_command_params_clean(
                [[x - size / 2, y - size / 2], [x - size / 2, y + size / 2], [x + size / 2, y + size / 2],
                 [x + size / 2, y - size / 2]], map_id)
            base64_string = base64.b64encode(json.dumps(command_params).encode('utf-8')).decode('utf-8')
            await self._device.set_dp(base64_string, 127)
        elif command == "clean_area":
            if 'vertices' in params:
                vertices = params['vertices']
            else:
                relative_vertices = params.get("relative_vertices", [])
                vertices = []
                for x, y in relative_vertices:
                    x, y = self.calculate_absolute_position(x, y)
                    vertices.append([x, y])

            map_id = params.get("map_id", 1695662532)
            command_params = self.get_command_params_clean(vertices, map_id)
            base64_string = base64.b64encode(json.dumps(command_params).encode('utf-8')).decode('utf-8')
            await self._device.set_dp(base64_string, 127)

    def rotate_coordinates(self, px, py):
        if self._position_axis_rotation == 0:
            px, py = px, -py
        elif self._position_axis_rotation == 1:
            px, py = py, px
        elif self._position_axis_rotation == 2:
            px, py = -px, py
        elif self._position_axis_rotation == 3:
            px, py = -px, -py
        return px, py

    def get_relative_position(self):
        position = self._attrs.get(POSITION, None)
        if position is None:
            return None
        px, py = position
        px, py = self.rotate_coordinates(px, py)

        px = px * self._position_relative_scale + self._position_relative_origin[0]
        py = py * self._position_relative_scale + self._position_relative_origin[1]

        return [px, py]

    def calculate_absolute_position(self, x, y):
        px = (x - self._position_relative_origin[0]) / self._position_relative_scale
        py = (y - self._position_relative_origin[1]) / self._position_relative_scale

        px, py = self.rotate_coordinates(px, py)

        return round(px), round(py)

    def status_updated(self, status):
        """Device status was updated."""
        state_value = str(self.dps(self._dp_id))
        previous_state = self._state

        if state_value in self._idle_status_list:
            self._state = STATE_IDLE
        elif state_value in self._docked_status_list:
            self._state = STATE_DOCKED
        elif state_value == self._config[CONF_RETURNING_STATUS_VALUE]:
            self._state = STATE_RETURNING
        elif state_value == self._config[CONF_PAUSED_STATE]:
            self._state = STATE_PAUSED
        else:
            self._state = STATE_CLEANING

        if previous_state == STATE_DOCKED and self._state != STATE_DOCKED:
            self._attrs[PATH] = []
            _LOGGER.info("Resetting PATH")

        if self.has_config(CONF_BATTERY_DP):
            self._battery_level = self.dps_conf(CONF_BATTERY_DP)

        self._cleaning_mode = ""
        if self.has_config(CONF_MODES):
            self._cleaning_mode = self.dps_conf(CONF_MODE_DP)
            self._attrs[MODE] = self._cleaning_mode

        self._fan_speed = ""
        if self.has_config(CONF_FAN_SPEEDS):
            self._fan_speed = self.dps_conf(CONF_FAN_SPEED_DP)

        if self.has_config(CONF_CLEAN_TIME_DP):
            self._attrs[CLEAN_TIME] = self.dps_conf(CONF_CLEAN_TIME_DP)

        if self.has_config(CONF_CLEAN_AREA_DP):
            self._attrs[CLEAN_AREA] = self.dps_conf(CONF_CLEAN_AREA_DP)

        if self.has_config(CONF_CLEAN_RECORD_DP):
            self._attrs[CLEAN_RECORD] = self.dps_conf(CONF_CLEAN_RECORD_DP)

        if self.has_config(CONF_FAULT_DP):
            self._attrs[FAULT] = self.dps_conf(CONF_FAULT_DP)
            if self._attrs[FAULT] != 0:
                self._state = STATE_ERROR

        if self.has_config(CONF_POSITION_BASE64_DP):
            if str(self._config.get(CONF_POSITION_BASE64_DP)) in status:
                position = self.dps_conf(CONF_POSITION_BASE64_DP)
                try:
                    decoded_json = json.loads(base64.b64decode(position))
                    position_array = decoded_json.get('data', {}).get('posArray', [])

                    if position_array is not None and len(position_array) == 1:
                        last_position = self._attrs.get(POSITION, None)
                        new_position = position_array[0]
                        if last_position != new_position:
                            self._attrs[POSITION] = new_position
                            relative_position = self.get_relative_position()
                            if relative_position is not None:
                                self._attrs[RELATIVE_POSITION] = relative_position
                                # self._attrs[PATH].append(relative_position)
                except (json.JSONDecodeError, TypeError, IndexError, binascii.Error):
                    _LOGGER.debug("Couldn't parse position")
                    _LOGGER.debug(f"Raw message: {position}")


async_setup_entry = partial(async_setup_entry, DOMAIN, LocaltuyaVacuum, flow_schema)
