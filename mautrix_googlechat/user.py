# mautrix-googlechat - A Matrix-Google Chat puppeting bridge
# Copyright (C) 2021 Tulir Asokan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
from typing import (Any, Dict, Optional, List, Awaitable, Union, Callable, AsyncIterable, cast,
                    NamedTuple, TYPE_CHECKING)
import datetime
import asyncio
import time

import maugclib.parsers
from maugclib import (googlechat_pb2 as googlechat, Client, RefreshTokenCache, TokenManager,
                      GoogleAuthError)

from mautrix.types import UserID, RoomID, MessageType
from mautrix.bridge import BaseUser, async_getter_lock
from mautrix.util.bridge_state import BridgeState, BridgeStateEvent
from mautrix.util.opt_prometheus import Gauge, Summary, async_time

from .config import Config
from .db import User as DBUser
from . import puppet as pu, portal as po

if TYPE_CHECKING:
    from .__main__ import GoogleChatBridge

METRIC_SYNC = Summary('bridge_sync', 'calls to sync')
METRIC_TYPING = Summary('bridge_on_typing', 'calls to on_typing')
METRIC_EVENT = Summary('bridge_on_event', 'calls to on_event')
METRIC_RECEIPT = Summary('bridge_on_receipt', 'calls to on_receipt')
METRIC_LOGGED_IN = Gauge('bridge_logged_in', 'Number of users logged into the bridge')
METRIC_CONNECTED = Gauge('bridge_connected', 'Number of users connected to Google Chat')


class User(DBUser, BaseUser):
    by_mxid: Dict[UserID, 'User'] = {}
    by_gcid: Dict[str, 'User'] = {}
    config: Config

    client: Optional[Client]
    is_admin: bool
    _db_instance: Optional[DBUser]

    _notice_room_lock: asyncio.Lock
    _intentional_disconnect: bool
    name: Optional[str]
    name_future: asyncio.Future
    connected: bool

    groups: Dict[str, googlechat.GetGroupResponse]
    groups_lock: asyncio.Lock
    users: Dict[str, googlechat.User]
    users_lock: asyncio.Lock

    def __init__(self, mxid: UserID, gcid: Optional[str] = None,
                 refresh_token: Optional[str] = None, notice_room: Optional[RoomID] = None
                 ) -> None:
        super().__init__(mxid=mxid, gcid=gcid, refresh_token=refresh_token,
                         notice_room=notice_room)
        BaseUser.__init__(self)
        self._notice_room_lock = asyncio.Lock()
        self.is_whitelisted, self.is_admin, self.level = self.config.get_permissions(mxid)
        self.client = None
        self.name = None
        self.name_future = self.loop.create_future()
        self.connected = False
        self.groups = {}
        self.groups_lock = asyncio.Lock()
        self.users = {}
        self.users_lock = asyncio.Lock()
        self._intentional_disconnect = False

    # region Sessions

    def _add_to_cache(self) -> None:
        self.by_mxid[self.mxid] = self
        if self.gcid:
            self.by_gcid[self.gcid] = self

    @classmethod
    async def all_logged_in(cls) -> AsyncIterable['User']:
        users = await super().all_logged_in()
        user: cls
        for user in users:
            try:
                yield cls.by_mxid[user.mxid]
            except KeyError:
                user._add_to_cache()
                yield user

    @classmethod
    @async_getter_lock
    async def get_by_mxid(cls, mxid: UserID, *, create: bool = True) -> Optional['User']:
        if pu.Puppet.get_id_from_mxid(mxid) or mxid == cls.az.bot_mxid:
            return None
        try:
            return cls.by_mxid[mxid]
        except KeyError:
            pass

        user = cast(cls, await super().get_by_mxid(mxid))
        if user is not None:
            user._add_to_cache()
            return user

        if create:
            cls.log.debug(f"Creating user instance for {mxid}")
            user = cls(mxid)
            await user.insert()
            user._add_to_cache()
            return user

        return None

    @classmethod
    @async_getter_lock
    async def get_by_gcid(cls, gcid: str) -> Optional['User']:
        try:
            return cls.by_gcid[gcid]
        except KeyError:
            pass

        user = cast(cls, await super().get_by_gcid(gcid))
        if user is not None:
            user._add_to_cache()
            return user

        return None

    # endregion

    async def fill_bridge_state(self, state: BridgeState) -> None:
        await super().fill_bridge_state(state)
        state.remote_id = str(self.gcid)
        state.remote_name = ""
        if self.gcid:
            puppet = await pu.Puppet.get_by_gcid(self.gcid)
            state.remote_name = puppet.name

    async def get_notice_room(self) -> RoomID:
        if not self.notice_room:
            async with self._notice_room_lock:
                # If someone already created the room while this call was waiting,
                # don't make a new room
                if self.notice_room:
                    return self.notice_room
                creation_content = {}
                if not self.config["bridge.federate_rooms"]:
                    creation_content["m.federate"] = False
                self.notice_room = await self.az.intent.create_room(
                    is_direct=True,
                    invitees=[self.mxid],
                    topic="Google Chat bridge notices",
                    creation_content=creation_content,
                )
                await self.save()
        return self.notice_room

    async def send_bridge_notice(self, text: str, important: bool = False,
                                 state_event: Optional[BridgeStateEvent] = None) -> None:
        if state_event:
            await self.push_bridge_state(state_event, message=text)
        if self.config["bridge.disable_bridge_notices"]:
            return
        elif not important and not self.config["bridge.unimportant_bridge_notices"]:
            return
        msgtype = MessageType.TEXT if important else MessageType.NOTICE
        try:
            await self.az.intent.send_text(await self.get_notice_room(), text, msgtype=msgtype)
        except Exception:
            self.log.warning("Failed to send bridge notice '%s'", text, exc_info=True)

    async def is_logged_in(self) -> bool:
        return self.client and self.connected

    @classmethod
    def init_cls(cls, bridge: 'GoogleChatBridge') -> AsyncIterable[Awaitable[None]]:
        cls.bridge = bridge
        cls.az = bridge.az
        cls.config = bridge.config
        cls.loop = bridge.loop
        return (user._try_init() async for user in cls.all_logged_in())

    async def _try_init(self) -> None:
        try:
            token_mgr = await TokenManager.from_refresh_token(UserRefreshTokenCache(self))
        except GoogleAuthError as e:
            await self.send_bridge_notice(
                f"Failed to resume session with stored refresh token: {e}",
                state_event=BridgeStateEvent.BAD_CREDENTIALS,
                important=True,
            )
            self.log.exception("Failed to resume session with stored refresh token")
        else:
            self.login_complete(token_mgr)

    def login_complete(self, token_manager: TokenManager) -> None:
        self.client = Client(token_manager, max_retries=3, retry_backoff_base=2)
        asyncio.create_task(self.start())
        self.client.on_stream_event.add_observer(self._in_background(self.on_stream_event))
        self.client.on_connect.add_observer(self.on_connect)
        self.client.on_reconnect.add_observer(self.on_reconnect)
        self.client.on_disconnect.add_observer(self.on_disconnect)

    def _in_background(self, method: Callable[[Any], Awaitable[None]]
                       ) -> Callable[[Any], Awaitable[None]]:
        async def try_proxy(*args, **kwargs) -> None:
            try:
                await method(*args, **kwargs)
            except Exception:
                self.log.exception("Exception in event handler")

        async def proxy(*args, **kwargs) -> None:
            asyncio.create_task(try_proxy(*args, **kwargs))

        return proxy

    async def start(self) -> None:
        last_disconnection = 0
        backoff = 4
        backoff_reset_in_seconds = 60
        state_event = BridgeStateEvent.TRANSIENT_DISCONNECT
        self._intentional_disconnect = False
        while True:
            try:
                await self.client.connect()
                self._track_metric(METRIC_CONNECTED, False)
                if self._intentional_disconnect:
                    self.log.info("Client connection finished")
                    return
                else:
                    self.log.warning("Client connection finished unexpectedly")
                    error_msg = "Client connection finished unexpectedly"
            except Exception as e:
                self._track_metric(METRIC_CONNECTED, False)
                self.log.exception("Exception in connection")
                error_msg = f"Exception in Google Chat connection: {e}"

            if last_disconnection + backoff_reset_in_seconds < time.time():
                backoff = 4
                state_event = BridgeStateEvent.TRANSIENT_DISCONNECT
            else:
                backoff = int(backoff * 1.5)
                if backoff > 60:
                    state_event = BridgeStateEvent.UNKNOWN_ERROR
            await self.send_bridge_notice(error_msg, state_event=state_event,
                                          important=state_event == BridgeStateEvent.UNKNOWN_ERROR)
            last_disconnection = time.time()
            self.log.debug(f"Reconnecting in {backoff} seconds")
            await asyncio.sleep(backoff)

    async def stop(self) -> None:
        if self.client:
            self._intentional_disconnect = True
            await self.client.disconnect()

    async def logout(self) -> None:
        self._track_metric(METRIC_LOGGED_IN, False)
        await self.stop()
        self.client = None
        self.by_gcid.pop(self.gcid, None)
        self.gcid = None
        self.refresh_token = None
        self.connected = False

        self.users = {}
        self.groups = {}

        self.name = None
        if not self.name_future.done():
            self.name_future.set_exception(Exception("logged out"))
        self.name_future = self.loop.create_future()

    async def on_connect(self) -> None:
        self.connected = True
        asyncio.create_task(self.on_connect_later())
        await self.send_bridge_notice("Connected to Google Chat")

    async def get_self(self) -> googlechat.User:
        if not self.gcid:
            info = await self.client.proto_get_self_user_status(
                googlechat.GetSelfUserStatusRequest(
                    request_header=self.client.get_gc_request_header()
                )
            )
            self.gcid = info.user_status.user_id.id
            self.by_gcid[self.gcid] = self

        resp = await self.client.proto_get_members(googlechat.GetMembersRequest(
            request_header=self.client.get_gc_request_header(),
            member_ids=[
                googlechat.MemberId(user_id=googlechat.UserId(id=self.gcid)),
            ]
        ))
        return resp.members[0].user

    async def get_users(self, ids: List[str]) -> List[googlechat.User]:
        async with self.users_lock:
            req_ids = [googlechat.MemberId(user_id=googlechat.UserId(id=user_id))
                       for user_id in ids if user_id not in self.users]
            if req_ids:
                self.log.debug(f"Fetching info of users {[user.user_id.id for user in req_ids]}")
                resp = await self.client.proto_get_members(googlechat.GetMembersRequest(
                    request_header=self.client.get_gc_request_header(),
                    member_ids=req_ids,
                ))
                member: googlechat.Member
                for member in resp.members:
                    self.users[member.user.user_id.id] = member.user
        return [self.users[user_id] for user_id in ids]

    async def get_group(self, id: Union[googlechat.GroupId, str]) -> googlechat.GetGroupResponse:
        if isinstance(id, str):
            group_id = maugclib.parsers.group_id_from_id(id)
            conv_id = id
        else:
            group_id = id
            conv_id = maugclib.parsers.id_from_group_id(id)
        try:
            return self.groups[conv_id]
        except KeyError:
            pass

        async with self.groups_lock:
            # Try again in case the fetch succeeded while waiting for the lock
            try:
                return self.groups[conv_id]
            except KeyError:
                pass
            self.log.debug(f"Fetching info of chat {conv_id}")
            resp = await self.client.proto_get_group(googlechat.GetGroupRequest(
                request_header=self.client.get_gc_request_header(),
                group_id=group_id,
                fetch_options=[
                    googlechat.GetGroupRequest.MEMBERS,
                    googlechat.GetGroupRequest.INCLUDE_DYNAMIC_GROUP_NAME,
                ],
            ))
            self.groups[conv_id] = resp
        return resp

    async def on_connect_later(self) -> None:
        try:
            self_info = await self.get_self()
        except Exception:
            self.log.exception("Failed to get own info")
            return
        await self.push_bridge_state(BridgeStateEvent.BACKFILLING)

        self.name = self_info.name or self_info.first_name
        self.log.debug(f"Found own name: {self.name}")
        self.name_future.set_result(self.name)

        self._track_metric(METRIC_CONNECTED, True)
        self._track_metric(METRIC_LOGGED_IN, True)
        await self.save()

        try:
            puppet = await pu.Puppet.get_by_gcid(self.gcid)
            if puppet.custom_mxid != self.mxid and puppet.can_auto_login(self.mxid):
                self.log.info(f"Automatically enabling custom puppet")
                await puppet.switch_mxid(access_token="auto", mxid=self.mxid)
        except Exception:
            self.log.exception("Failed to automatically enable custom puppet")

        try:
            await self.sync()
        except Exception:
            self.log.exception("Failed to sync conversations and users")

        await self.push_bridge_state(BridgeStateEvent.CONNECTED)

    async def on_reconnect(self) -> None:
        self.connected = True
        await self.send_bridge_notice("Reconnected to Google Chat")
        await self.push_bridge_state(BridgeStateEvent.CONNECTED)

    async def on_disconnect(self) -> None:
        self.connected = False
        await self.send_bridge_notice("Disconnected from Google Chat")
        await self.push_bridge_state(BridgeStateEvent.TRANSIENT_DISCONNECT,
                                     error="googlechat-disconnected")

    @async_time(METRIC_SYNC)
    async def sync(self) -> None:
        self.log.debug("Fetching first page of the world")
        resp = await self.client.proto_paginated_world(googlechat.PaginatedWorldRequest(
            request_header=self.client.get_gc_request_header(),
            fetch_from_user_spaces=True,
            fetch_options=[
                googlechat.PaginatedWorldRequest.EXCLUDE_GROUP_LITE,
            ],
        ))
        items: List[googlechat.WorldItemLite] = list(resp.world_items)
        items.sort(key=lambda item: item.sort_timestamp, reverse=True)
        max_sync = self.config["bridge.initial_chat_sync"]
        for index, item in enumerate(items):
            conv_id = maugclib.parsers.id_from_group_id(item.group_id)
            portal = await po.Portal.get_by_gcid(conv_id, self.gcid)
            self.log.debug("Syncing %s", portal.gcid)
            if portal.mxid:
                await portal.update_matrix_room(self, item)
                # TODO backfill
                # if len(state.event) > 0 and not DBMessage.get_by_gid(state.event[0].event_id):
                #     self.log.debug("Last message %s in chat %s not found in db, backfilling...",
                #                    state.event[0].event_id, state.conversation_id.id)
                #     await portal.backfill(self, is_initial=False)
            elif index < max_sync:
                await portal.create_matrix_room(self, item)
        await self.update_direct_chats()

    async def get_direct_chats(self) -> Dict[UserID, List[RoomID]]:
        return {
            pu.Puppet.get_mxid_from_id(portal.other_user_id): [portal.mxid]
            async for portal in po.Portal.get_all_by_receiver(self.gcid)
            if portal.mxid
        }

    # region Google Chat event handling

    async def on_stream_event(self, evt: googlechat.Event) -> None:
        if not evt.group_id:
            return
        conv_id = maugclib.parsers.id_from_group_id(evt.group_id)
        portal = await po.Portal.get_by_gcid(conv_id, self.gcid)
        type_name = googlechat.Event.EventType.Name(evt.type)
        if evt.body.HasField("message_posted"):
            # await portal.backfill_lock.wait(event.id_)
            if evt.type == googlechat.Event.MESSAGE_UPDATED:
                await portal.handle_googlechat_edit(self, evt.body.message_posted.message)
            else:
                await portal.handle_googlechat_message(self, evt.body.message_posted.message)
        elif evt.body.HasField("message_reaction"):
            await portal.handle_googlechat_reaction(evt.body.message_reaction)
        elif evt.body.HasField("message_deleted"):
            await portal.handle_googlechat_redaction(evt.body.message_deleted)
        elif evt.body.HasField("read_receipt_changed"):
            await portal.handle_googlechat_read_receipts(evt.body.read_receipt_changed)
        elif evt.body.HasField("group_viewed"):
            await portal.mark_read(self.gcid, evt.body.group_viewed.view_time)
        else:
            self.log.debug(f"Unhandled event type {type_name}")

    # @async_time(METRIC_RECEIPT)
    # async def on_receipt(self, event: WatermarkNotification) -> None:
    #     if not self.chats:
    #         self.log.debug("Received receipt event before chat list, ignoring")
    #         return
    #     conv: Conversation = self.chats.get(event.conv_id)
    #     portal = await po.Portal.get_by_conversation(conv, self.gcid)
    #     if not portal:
    #         return
    #     message = await DBMessage.get_closest_before(portal.gcid, portal.gc_receiver,
    #                                                  event.read_timestamp)
    #     if not message:
    #         return
    #     puppet = await pu.Puppet.get_by_gcid(event.user_id)
    #     await puppet.intent_for(portal).mark_read(message.mx_room, message.mxid)

    # @async_time(METRIC_TYPING)
    # async def on_typing(self, event: TypingStatusMessage):
    #     portal = await po.Portal.get_by_gcid(event.conv_id, self.gcid)
    #     if not portal:
    #         return
    #     sender = await pu.Puppet.get_by_gcid(event.user_id, create=False)
    #     if not sender:
    #         return
    #     await portal.handle_hangouts_typing(self, sender, event.status)

    # endregion
    # region Google Chat API calls

    async def set_typing(self, conversation_id: str, typing: bool) -> None:
        self.log.debug(f"set_typing({conversation_id}, {typing})")
        # await self.client.set_typing(hangouts.SetTypingRequest(
        #     request_header=self.client.get_request_header(),
        #     conversation_id=hangouts.ConversationId(id=conversation_id),
        #     type=hangouts.TYPING_TYPE_STARTED if typing else hangouts.TYPING_TYPE_STOPPED,
        # ))

    async def mark_read(self, conversation_id: str,
                        timestamp: Optional[Union[datetime.datetime, int]] = None) -> None:
        pass
        # if isinstance(timestamp, datetime.datetime):
        #     timestamp = hangups.parsers.to_timestamp(timestamp)
        # elif not timestamp:
        #     timestamp = int(time.time() * 1_000_000)
        # await self.client.update_watermark(hangouts.UpdateWatermarkRequest(
        #     request_header=self.client.get_request_header(),
        #     conversation_id=hangouts.ConversationId(id=conversation_id),
        #     last_read_timestamp=timestamp,
        # ))

    # endregion


class UserRefreshTokenCache(RefreshTokenCache):
    user: User

    def __init__(self, user: User) -> None:
        self.user = user

    async def get(self) -> str:
        return self.user.refresh_token

    async def set(self, refresh_token: str) -> None:
        self.user.log.trace("New refresh token: %s", refresh_token)
        self.user.refresh_token = refresh_token
        await self.user.save()
