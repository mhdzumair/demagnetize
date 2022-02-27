# <https://www.bittorrent.org/beps/bep_0015.html>
from __future__ import annotations
from contextlib import nullcontext
from dataclasses import dataclass, field
from functools import partial
from random import randint
from socket import AF_INET6
import struct
from time import time
from typing import Any, Callable, ContextManager, List, Optional, TypeVar
from anyio import create_connected_udp_socket, fail_after
from anyio.abc import AsyncResource, ConnectedUDPSocket, SocketAttribute
from .base import Tracker, unpack_peers, unpack_peers6
from ..consts import LEFT, NUMWANT
from ..errors import TrackerError
from ..peers import Peer
from ..util import TRACE, InfoHash, Key, log

T = TypeVar("T")

PROTOCOL_ID = 0x41727101980


@dataclass
class UDPTracker(Tracker):
    host: str = field(init=False)
    port: int = field(init=False)

    def __post_init__(self) -> None:
        if self.url.scheme != "udp":
            raise ValueError("URL scheme must be 'udp'")
        if self.url.host is None:
            raise ValueError("URL missing host")
        if self.url.port is None:
            raise ValueError("URL missing port")
        self.host = self.url.host
        self.port = self.url.port

    async def get_peers(self, info_hash: InfoHash) -> List[Peer]:
        try:
            async with await create_connected_udp_socket(self.host, self.port) as conn:
                async with Communicator(tracker=self, conn=conn) as cmm:
                    return await cmm.get_peers(info_hash)
        except OSError as e:
            raise TrackerError(
                f"Error communicating with {self}: {type(e).__name__}: {e}"
            )


@dataclass
class Communicator(AsyncResource):
    tracker: UDPTracker
    conn: ConnectedUDPSocket

    @property
    def is_ipv6(self) -> bool:
        return self.conn.extra(SocketAttribute.family) == AF_INET6

    async def aclose(self) -> None:
        await self.conn.aclose()

    async def get_peers(self, info_hash: InfoHash) -> List[Peer]:
        while True:
            try:
                cnx = await self.connect()
                r = await cnx.announce(info_hash)
            except TimeoutError:
                log.log(
                    TRACE,
                    "Connection to %s timed out; restarting",
                    self.tracker,
                )
            else:
                log.info("%s returned %d peers", self.tracker, len(r.peers))
                log.log(
                    TRACE,
                    "%s returned peers: %s",
                    self.tracker,
                    ", ".join(map(str, r.peers)),
                )
                return r.peers

    async def send_receive(
        self,
        msg: bytes,
        response_parser: Callable[[bytes], T],
        expiration: Optional[float] = None,
    ) -> T:
        ctx: ContextManager[Any]
        if expiration is None:
            ctx = nullcontext()
        else:
            ctx = fail_after(expiration - time())
        with ctx:
            n = 0
            while True:
                log.log(TRACE, "Sending to %s: %r", self.tracker, msg)
                await self.conn.send(msg)
                try:
                    with fail_after(15 << n):
                        resp = await self.conn.receive()
                except TimeoutError:
                    log.log(
                        TRACE,
                        "%s did not reply in time; resending message",
                        self.tracker,
                    )
                    if n < 8:
                        ### TODO: Should this count remember timeouts from
                        ### previous connections & connection attempts?
                        n += 1
                    continue
                log.log(TRACE, "%s responded with: %r", self.tracker, resp)
                try:
                    data = response_parser(resp)
                except Exception as e:
                    log.log(
                        TRACE,
                        "Response from %s was invalid, will resend: %s: %s",
                        self.tracker,
                        type(e).__name__,
                        e,
                    )
                    continue
                else:
                    return data

    async def connect(self) -> Connection:
        transaction_id = make_transaction_id()
        conn_id = await self.send_receive(
            build_connection_request(transaction_id),
            partial(parse_connection_response, transaction_id),
        )
        return Connection(communicator=self, id=conn_id)


@dataclass
class Connection:
    communicator: Communicator
    id: int
    expiration: float = field(init=False)

    def __post_init__(self) -> None:
        self.expiration = time() + 60

    async def announce(self, info_hash: InfoHash) -> AnnounceResponse:
        transaction_id = make_transaction_id()
        msg = build_announce_request(
            transaction_id=transaction_id,
            connection_id=self.id,
            info_hash=info_hash,
            peer_id=self.communicator.tracker.app.peer_id,
            peer_port=self.communicator.tracker.app.peer_port,
            key=self.communicator.tracker.app.key,
        )
        return await self.communicator.send_receive(
            msg,
            partial(
                parse_announce_response,
                transaction_id,
                is_ipv6=self.communicator.is_ipv6,
            ),
            expiration=self.expiration,
        )


@dataclass
class AnnounceResponse:
    interval: int
    leechers: int
    seeders: int
    peers: List[Peer]


def make_transaction_id() -> int:
    return randint(-(1 << 31), (1 << 31) - 1)


def build_connection_request(transaction_id: int) -> bytes:
    return struct.pack("!qii", PROTOCOL_ID, 0, transaction_id)


def parse_connection_response(transaction_id: int, resp: bytes) -> int:
    # Returns connection ID
    action, xaction_id, connection_id = struct.unpack_from("!iiq", resp)
    # Use `struct.unpack_from()` instead of `unpack()` because "Clients ...
    # should not assume packets to be of a certain size"
    if xaction_id != transaction_id:
        raise ValueError(
            f"Transaction ID mismatch: expected {transaction_id}, got {xaction_id}"
        )
    if action != 0:
        raise ValueError(f"Action mismatch: expected 0, got {action}")
    assert isinstance(connection_id, int)
    return connection_id


def build_announce_request(
    transaction_id: int,
    connection_id: int,
    info_hash: InfoHash,
    peer_id: bytes,
    peer_port: int,
    key: Key,
) -> bytes:
    downloaded = 0
    uploaded = 0
    event = 2
    ip_address = b"\0\0\0\0"
    return (
        struct.pack("!qii", connection_id, 1, transaction_id)
        + bytes(info_hash)
        + (peer_id + b"\0" * 20)[:20]
        + struct.pack("!qqqi", downloaded, LEFT, uploaded, event)
        + ip_address
        + bytes(key)
        + struct.pack("!iH", NUMWANT, peer_port)
    )


def parse_announce_response(
    transaction_id: int, resp: bytes, is_ipv6: bool
) -> AnnounceResponse:
    header = struct.Struct("!iiiii")
    action, xaction_id, interval, leechers, seeders = header.unpack_from(resp)
    if xaction_id != transaction_id:
        raise ValueError(
            f"Transaction ID mismatch: expected {transaction_id}, got {xaction_id}"
        )
    if action != 1:
        raise ValueError(f"Action mismatch: expected 1, got {action}")
    resp = resp[header.size :]
    if is_ipv6:
        peers = unpack_peers6(resp)
    else:
        peers = unpack_peers(resp)
    return AnnounceResponse(
        interval=interval, leechers=leechers, seeders=seeders, peers=peers
    )
