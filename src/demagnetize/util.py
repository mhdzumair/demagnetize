from __future__ import annotations
from base64 import b32decode
from binascii import unhexlify
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import logging
import re
from typing import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    List,
    Optional,
    Tuple,
    TypeVar,
)
from anyio import create_memory_object_stream, create_task_group
from anyio.streams.memory import MemoryObjectSendStream
from torf import Magnet, Torrent

log = logging.getLogger(__package__)

TRACE = 5

T = TypeVar("T")


@dataclass
class InfoHash:
    as_str: str
    as_bytes: bytes

    @classmethod
    def from_string(cls, s: str) -> InfoHash:
        if len(s) == 40:
            b = unhexlify(s)
        elif len(s) == 32:
            b = b32decode(s)
        else:
            raise ValueError(f"Invalid info hash: {s!r}")
        return cls(as_str=s, as_bytes=b)

    def __str__(self) -> str:
        return self.as_str

    def __bytes__(self) -> bytes:
        return self.as_bytes


@dataclass
class Report:
    #: Collection of magnet URLs and the files their torrents were saved to
    #: (None if the demagnetization failed)
    downloads: List[Tuple[Magnet, Optional[str]]] = field(default_factory=list)

    @classmethod
    def for_success(cls, magnet: Magnet, filename: str) -> Report:
        return cls(downloads=[(magnet, filename)])

    @classmethod
    def for_failure(cls, magnet: Magnet) -> Report:
        return cls(downloads=[(magnet, None)])

    @property
    def total(self) -> int:
        return len(self.downloads)

    @property
    def finished(self) -> int:
        return sum(1 for _, fname in self.downloads if fname is not None)

    @property
    def ok(self) -> bool:
        return bool(self.downloads) and all(
            fname is not None for _, fname in self.downloads
        )

    def __add__(self, other: Report) -> Report:
        return type(self)(self.downloads + other.downloads)

    def __iadd__(self, other: Report) -> Report:
        self.downloads.extend(other.downloads)
        return self


def yield_lines(fp: Iterable[str]) -> Iterator[str]:
    for line in fp:
        line = line.strip()
        if line and not line.startswith("#"):
            yield line


def template_torrent_filename(pattern: str, torrent: Torrent) -> str:
    fields = {
        "name": sanitize_pathname(str(torrent.name)),
        "hash": torrent.infohash,
    }
    return pattern.format_map(fields)


def sanitize_pathname(s: str) -> str:
    return re.sub(r'[\0\x5C/<>:|"?*%]', "_", re.sub(r"\s", " ", s))


def make_peer_id() -> str:
    raise NotImplementedError


@asynccontextmanager
async def acollect(
    funcs: Iterable[Callable[[], Awaitable[T]]]
) -> AsyncIterator[AsyncIterator[T]]:
    async with create_task_group() as tg:
        sender, receiver = create_memory_object_stream()
        async with sender:
            for f in funcs:
                tg.start_soon(_acollect_pipe, f, sender.clone())
        async with receiver:
            yield receiver


async def _acollect_pipe(
    func: Callable[[], Awaitable[T]], sender: MemoryObjectSendStream[T]
) -> None:
    async with sender:
        value = await func()
        await sender.send(value)
