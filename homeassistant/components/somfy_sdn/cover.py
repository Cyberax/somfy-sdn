from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import typing
from typing import Any

from somfy.connector import (
    MASTER_ADDRESS,
    NodeType,
    SomfyAddress,
    SomfyExchanger,
    SomfyMessage,
    SomfyMessageId,
    try_to_exchange_one,
)
from somfy.payloads import (
    CtrlMoveRelativePayload,
    CtrlMoveToFunction,
    CtrlMoveToPayload,
    CtrlStopPayload,
    MotorStatus,
    PostMotorPositionPayload,
    PostMotorStatusPayload,
    RelativeMoveFunction,
    SomfyNackReason,
)
from somfy.utils import SomfyNackException, send_with_ack

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import CONNECTOR, DEVICES

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=20)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    if not discovery_info:
        return
    devices = discovery_info[DEVICES]
    connector = discovery_info[CONNECTOR]

    entities = []
    for d in devices:
        entity = SdnCover(
            hass, d["somfy_id"], d["name"], d.get("move_by_ips", False), connector
        )
        entities.append(entity)

    async_add_entities(entities)
    for e in entities:
        e.schedule_update_ha_state(force_refresh=True)


class SdnCover(CoverEntity):
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(
        self,
        hass: HomeAssistant,
        somfy_id,
        name,
        move_by_stops: bool,
        connector: SomfyExchanger,
    ):
        self.somfy_id = somfy_id
        self.somfy_addr = SomfyAddress.make(somfy_id)
        self.connector = connector
        self._name = name
        self.move_by_stops = move_by_stops
        self._position = 0
        self._attr_should_poll = True
        self._attr_is_closed = False

        self.entity_id = "cover." + somfy_id.lower()
        self._attr_unique_id = "somfy_sdn_" + somfy_id.lower()

        self.fast_poll_task: asyncio.Task | None = None
        self.last_good_update = asyncio.get_event_loop().time()

        _LOGGER.debug("Setting up the shade: %s(%s)", name, somfy_id)
        super().__init__()

    @property
    def name(self):
        return self._name

    async def _shutdown(self):
        if self.fast_poll_task and not self.fast_poll_task.done():
            self.fast_poll_task.cancel()
        await self.fast_poll_task

    async def async_update(self):
        reply = await try_to_exchange_one(
            self.connector,
            self.somfy_addr,
            SomfyMessageId.GET_MOTOR_POSITION,
            SomfyMessageId.POST_MOTOR_POSITION,
        )
        if not reply:
            raise Exception("Failed to get motor position")
        pos = typing.cast(PostMotorPositionPayload, reply.payload)

        reply = await try_to_exchange_one(
            self.connector,
            self.somfy_addr,
            SomfyMessageId.GET_MOTOR_STATUS,
            SomfyMessageId.POST_MOTOR_STATUS,
        )
        if not reply:
            raise Exception("Failed to get motor position")
        stat = typing.cast(PostMotorStatusPayload, reply.payload)

        self._position = pos.get_position_percent()
        self._attr_is_closed = pos.get_position_percent() > 96
        if stat.get_status() != MotorStatus.RUNNING:
            self._attr_is_opening = False
            self._attr_is_closing = False

        self.last_good_update = asyncio.get_event_loop().time()

    @property
    def current_cover_position(self) -> int | None:
        # HomeAssistant has a consistent convention, making shades behave like light sources:
        # fully open is 100 and fully closed is 0.
        # Somfy has a different convention: 0 is open, 100 is closed.
        return 100 - self._position

    async def async_close_cover(self, **kwargs: Any) -> None:
        if self.move_by_stops:
            if await self._move_to_next_ip(True):
                return

        await self._do_set_position(somfy_pos=100)

    async def async_open_cover(self, **kwargs: Any) -> None:
        if self.move_by_stops:
            if await self._move_to_next_ip(False):
                return

        await self._do_set_position(somfy_pos=0)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        sent = SomfyMessage(
            msgid=SomfyMessageId.CTRL_STOP,
            need_ack=True,
            from_node_type=NodeType.TYPE_ALL,
            from_addr=MASTER_ADDRESS,
            to_node_type=NodeType.TYPE_ALL,
            to_addr=self.somfy_addr,
            payload=CtrlStopPayload.make(),
        )
        # TODO: timeout handling
        await send_with_ack(self.somfy_addr, self.connector, sent)

    async def _start_fast_poll(self):
        # Poll the status to quickly update the UI
        if self.fast_poll_task is None or self.fast_poll_task.done():
            self.fast_poll_task = self.hass.async_create_task(self._async_fast_update())

    # Quickly update the UI during opening/closing
    async def _async_fast_update(self):
        # Make sure we don't stop the updates immediately
        self.last_good_update = asyncio.get_event_loop().time()
        _LOGGER.info("Fast update started")
        while (
            self._attr_is_opening or self._attr_is_closing
        ) and not self.hass.is_stopping:
            if asyncio.get_event_loop().time() - self.last_good_update > 5:
                _LOGGER.warning("Updates are failing, stopping fast update")
                break
            self.schedule_update_ha_state(force_refresh=True)
            await asyncio.sleep(0.5)
        _LOGGER.info("Fast update stopped")

    async def _move_to_next_ip(self, move_down: bool) -> bool:
        payload = CtrlMoveRelativePayload.make(
            func=RelativeMoveFunction.MOVE_NEXT_IP_DOWN
            if move_down
            else RelativeMoveFunction.MOVE_NEXT_IP_UP,
            parameter=0,
        )
        sent = SomfyMessage(
            msgid=SomfyMessageId.CTRL_MOVE_RELATIVE,
            need_ack=True,
            from_node_type=NodeType.TYPE_ALL,
            from_addr=MASTER_ADDRESS,
            to_node_type=NodeType.TYPE_ALL,
            to_addr=self.somfy_addr,
            payload=payload,
        )
        try:
            # TODO: timeout handling
            await send_with_ack(self.somfy_addr, self.connector, sent)
        except SomfyNackException as e:
            if e.nack().get_nack_code() == SomfyNackReason.NACK_LAST_IP_REACHED:
                return False

        self._attr_is_opening = not move_down
        self._attr_is_closing = move_down
        await self._start_fast_poll()
        return True

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        target_pos = kwargs[ATTR_POSITION]
        # HomeAssistant has a consistent convention, making shades behave like light sources:
        # fully open is 100 and fully closed is 0.
        # Somfy has a different convention: 0 is open, 100 is closed.
        await self._do_set_position(100 - target_pos)

    async def _do_set_position(self, somfy_pos):
        if somfy_pos < self._position:
            self._attr_is_opening = True
            self._attr_is_closing = False
        if somfy_pos > self._position:
            self._attr_is_closing = True
            self._attr_is_opening = False
        payload = CtrlMoveToPayload.make(
            func=CtrlMoveToFunction.POSITION_PERCENT, position=int(somfy_pos)
        )
        sent = SomfyMessage(
            msgid=SomfyMessageId.CTRL_MOVETO,
            need_ack=True,
            from_node_type=NodeType.TYPE_ALL,
            from_addr=MASTER_ADDRESS,
            to_node_type=NodeType.TYPE_ALL,
            to_addr=self.somfy_addr,
            payload=payload,
        )
        # TODO: timeout handling
        await send_with_ack(self.somfy_addr, self.connector, sent)
        await self._start_fast_poll()
