from __future__ import annotations
from typing import TYPE_CHECKING
import attr

if TYPE_CHECKING:
    from .peer import Peer
    from .trackers import Tracker
    from .util import InfoHash


class Error(Exception):
    pass


@attr.define
class TrackerError(Error):
    tracker: Tracker
    info_hash: InfoHash
    msg: str

    def __str__(self) -> str:
        return f"Error announcing to {self.tracker} for {self.info_hash}: {self.msg}"


@attr.define
class PeerError(Error):
    peer: Peer
    info_hash: InfoHash
    msg: str

    def __str__(self) -> str:
        return f"Error communicating with {self.peer} for {self.info_hash}: {self.msg}"


class DemagnetizeFailure(Error):
    pass


class UnbencodeError(ValueError):
    pass


class UnknownBEP9MsgType(Exception):
    def __init__(self, msg_type: int) -> None:
        self.msg_type = msg_type


class CellClosedError(Exception):
    pass


class TrackerFailure(Exception):
    # Raised when tracker returns a failure message
    ### TODO: Come up with a better name?
    pass
