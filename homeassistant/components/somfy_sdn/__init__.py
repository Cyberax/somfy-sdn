"""The somfy_sdn integration."""

from __future__ import annotations

import logging

from somfy.connector import ReconnectingSomfyConnector, SocketConnectionFactory
from somfy.serial import SerialConnectionFactory
import voluptuous as vol

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONNECTION,
    CONNECTOR,
    DATA_SOMFY,
    DEVICES,
    DOMAIN,
    ENDPOINT_SERIAL,
    ENDPOINT_TCP,
    ENDPOINT_TCP_HOST,
    ENDPOINT_TCP_PORT,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.COVER]

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: [
            vol.Schema(
                {
                    vol.Required(CONNECTION): vol.Schema(
                        {
                            vol.Exclusive(ENDPOINT_TCP, "connectivity"): {
                                vol.Required(ENDPOINT_TCP_HOST): cv.string,
                                vol.Required(ENDPOINT_TCP_PORT): cv.positive_int,
                            },
                            vol.Exclusive(ENDPOINT_SERIAL, "connectivity"): cv.string,
                            vol.Required(DEVICES): [
                                {
                                    vol.Required("somfy_id"): cv.string,
                                    vol.Required("name"): cv.string,
                                    vol.Optional("move_by_ips"): cv.boolean,
                                }
                            ],
                        }
                    )
                }
            )
        ]
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the somfy_sdn component."""

    for c in config[DOMAIN]:
        conn_conf = c[CONNECTION]

        tcp = conn_conf.get(ENDPOINT_TCP)
        if tcp:
            ch = SocketConnectionFactory(tcp[ENDPOINT_TCP_HOST], tcp[ENDPOINT_TCP_PORT])
        else:
            serial = conn_conf.get(ENDPOINT_SERIAL)
            ch = SerialConnectionFactory(serial)

        conn = ReconnectingSomfyConnector(ch)
        await conn.start()

        devices = conn_conf[DEVICES]
        hass.async_create_task(
            async_load_platform(
                hass,
                Platform.COVER,
                DOMAIN,
                {DEVICES: devices, CONNECTOR: conn},
                config,
            )
        )

    return True
