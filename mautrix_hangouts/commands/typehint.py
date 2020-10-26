from typing import TYPE_CHECKING

from mautrix.bridge.commands import CommandEvent as BaseCommandEvent

if TYPE_CHECKING:
    from ..__main__ import HangoutsBridge
    from ..user import User


class CommandEvent(BaseCommandEvent):
    bridge: 'HangoutsBridge'
    sender: 'User'
