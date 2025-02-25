"""Adds support for Danfoss Ally Gateway."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import voluptuous as vol
from homeassistant.components.climate.const import PRESET_AWAY, PRESET_HOME
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.dispatcher import dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import Throttle
from pydanfossally import DanfossAlly, exceptions

from .const import (
    CONF_KEY,
    CONF_SECRET,
    DATA,
    DOMAIN,
    SIGNAL_ALLY_UPDATE_RECEIVED,
    UPDATE_LISTENER,
    UPDATE_TRACK,
)

_LOGGER = logging.getLogger(__name__)

ALLY_COMPONENTS = ["binary_sensor", "climate", "sensor", "switch", "select"]

MIN_TIME_BETWEEN_UPDATES = timedelta(seconds=30)
SCAN_INTERVAL = 45

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.All(
            cv.ensure_list,
            [
                {
                    vol.Required(CONF_KEY): cv.string,
                    vol.Required(CONF_SECRET): cv.string,
                }
            ],
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the Danfoss Ally component."""

    hass.data.setdefault(DOMAIN, {})

    if DOMAIN not in config:
        return True

    for conf in config[DOMAIN]:
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_IMPORT},
                data=conf,
            )
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Danfoss Ally from a config entry."""

    key = entry.data[CONF_KEY]
    secret = entry.data[CONF_SECRET]

    allyconnector = AllyConnector(hass, key, secret)
    try:
        await hass.async_add_executor_job(allyconnector.setup)
    except TimeoutError:
        _LOGGER.error("Timeout connecting to Danfoss Ally")
        raise ConfigEntryNotReady  # pylint: disable=raise-missing-from
    except:  # pylint: disable=bare-except
        _LOGGER.error(
            "Something went horrible wrong when communicating with Danfoss Ally"
        )
        return False

    if not allyconnector.authorized:
        _LOGGER.error("Error authorizing")
        return False

    async def _update(now):
        """Periodic update."""
        try:
            await allyconnector.async_update()
            if _update.error_reported:
                _update.error_reported = False
                _LOGGER.info("Connection reestablished")
        except TimeoutError:
            if not _update.error_reported or _LOGGER.isEnabledFor(logging.DEBUG):
                _update.error_reported = True
                _LOGGER.error("Timeout connecting to Danfoss Ally")
        except exceptions.HTTPException as err:
            if not _update.error_reported or _LOGGER.isEnabledFor(logging.DEBUG):
                _update.error_reported = True
                _LOGGER.error("HTTP error: %s", str(err.__cause__))
        except ConnectionError as err:
            if not _update.error_reported or _LOGGER.isEnabledFor(logging.DEBUG):
                _update.error_reported = True
                _LOGGER.error(
                    "Connection to Danfoss Ally failed: %s", str(err.__cause__)
                )
        except Exception as err:  # pylint: disable=broad-except
            if not _update.error_reported or _LOGGER.isEnabledFor(logging.DEBUG):
                _update.error_reported = True
                _LOGGER.error(
                    "Other error communicating with Danfoss Ally: %s",
                    str(err.__context__),
                )

    _update.error_reported = False

    await _update(None)

    # Remove old devices
    if allyconnector.ally.devices is not None and len(allyconnector.ally.devices) > 0:
        # Build list of devices to keep
        devices = []
        for device in allyconnector.ally.devices:
            devices.append((DOMAIN, device))

        # Remove devices no longr reported by the API
        device_registry = dr.async_get(hass)
        for device_entry in dr.async_entries_for_config_entry(
            device_registry, entry.entry_id
        ):
            for identifier in device_entry.identifiers:
                if identifier not in devices:
                    _LOGGER.warning("Removing device: %s", identifier)
                    device_registry.async_remove_device(device_entry.id)

    update_track = async_track_time_interval(
        hass,
        _update,
        timedelta(seconds=SCAN_INTERVAL),
    )

    update_listener = entry.add_update_listener(_async_update_listener)

    hass.data[DOMAIN][entry.entry_id] = {
        DATA: allyconnector,
        UPDATE_TRACK: update_track,
        UPDATE_LISTENER: update_listener,
    }

    await hass.config_entries.async_forward_entry_setups(entry, ALLY_COMPONENTS)

    # for component in ALLY_COMPONENTS:
    #     hass.async_create_task(
    #         hass.config_entries.async_forward_entry_setups(entry, component)
    #     )

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a config entry."""
    unload_ok = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, component)
                for component in ALLY_COMPONENTS
            ]
        )
    )

    hass.data[DOMAIN][entry.entry_id][UPDATE_TRACK]()
    hass.data[DOMAIN][entry.entry_id][UPDATE_LISTENER]()

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


class AllyConnector:
    """An object to store the Danfoss Ally data."""

    def __init__(self, hass, key, secret):
        """Initialize Danfoss Ally Connector."""
        self.hass = hass
        self._key = key
        self._secret = secret
        self.ally = DanfossAlly()
        self._authorized = False
        self._latest_write_time = datetime.min
        self._latest_poll_time = datetime.min

    def setup(self) -> None:
        """Setup API connection."""
        auth = self.ally.initialize(self._key, self._secret)

        self._authorized = auth

    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    async def async_update(self) -> None:
        """Update API data."""
        _LOGGER.debug("Updating Danfoss Ally devices")

        # Postpone poll if a recent change were made - Attempt to avoid UI glitches
        seconds_since_write = (
            datetime.utcnow() - self._latest_write_time
        ).total_seconds()
        if seconds_since_write < 1:
            _LOGGER.debug(
                "Seconds since last write %f. Postponing update for 1 sec.",
                seconds_since_write,
            )
            await asyncio.sleep(1)

        # Poll API
        await self.hass.async_add_executor_job(
            self.ally.getDeviceList
        )  # self.ally.getDeviceList()
        self._latest_poll_time = datetime.utcnow()

        for device in self.ally.devices:  # pylint: disable=consider-using-dict-items
            _LOGGER.debug("%s: %s", device, self.ally.devices[device])
        dispatcher_send(self.hass, SIGNAL_ALLY_UPDATE_RECEIVED)

    @property
    def devices(self):
        """Return device list from API."""
        return self.ally.devices

    def set_temperature(
        self, device_id: str, temperature: float, code="manual_mode_fast"
    ) -> None:
        """Set temperature for device_id."""
        self._latest_write_time = datetime.utcnow()
        self.ally.setTemperature(device_id, temperature, code)

        # Debug info - log if update was done approximately as the same time as write
        seconds_since_poll = (
            datetime.utcnow() - self._latest_poll_time
        ).total_seconds()
        if seconds_since_poll < 0.5:
            _LOGGER.debug(
                "set_temperature: Time since last poll %f sec.", seconds_since_poll
            )

    def set_mode(self, device_id: str, mode: str) -> None:
        """Set operating mode for device_id."""
        self._latest_write_time = datetime.utcnow()
        self.ally.setMode(device_id, mode)

        # Debug info - log if update was done approximately as the same time as write
        seconds_since_poll = (
            datetime.utcnow() - self._latest_poll_time
        ).total_seconds()
        if seconds_since_poll < 0.5:
            _LOGGER.debug("set_mode: Time since last poll %f sec.", seconds_since_poll)

    def send_commands(
        self,
        device_id: str,
        listofcommands: list[tuple[str, str]],
        postponeupdate: bool,
    ) -> None:
        """Send list of commands for given device."""
        if postponeupdate:
            self._latest_write_time = datetime.utcnow()
        try:
            self.ally.sendCommand(device_id, listofcommands)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.error(
                "Failed to send command to device: %s. Error: %s",
                device_id,
                str(err.__cause__),
            )

    @property
    def authorized(self) -> bool:
        """Return authorized state."""
        return self._authorized
