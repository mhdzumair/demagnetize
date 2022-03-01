from functools import partial
from random import randint
from time import time
from typing import Awaitable, Callable, List, Union
import attr
import click
from torf import Magnet, Torrent
from .consts import CLIENT
from .errors import DemagnetizeFailure
from .peer import Peer
from .session import TorrentSession
from .trackers import Tracker
from .util import (
    TRACE,
    InfoHash,
    InfoPiecer,
    Key,
    Report,
    acollect,
    log,
    make_peer_id,
    template_torrent_filename,
)


@attr.define
class Demagnetizer:
    key: Key = attr.Factory(Key.generate)
    peer_id: bytes = attr.Factory(make_peer_id)
    peer_port: int = attr.Factory(lambda: randint(1025, 65535))

    def __attrs_post_init__(self) -> None:
        log.log(TRACE, "Using key = %s", self.key)
        log.log(TRACE, "Using peer ID = %r", self.peer_id)
        log.log(TRACE, "Using peer port = %d", self.peer_port)

    async def download_torrent_info(
        self, magnets: List[Union[str, Magnet]], fntemplate: str
    ) -> Report:
        funcs: List[Callable[[], Awaitable[Report]]] = []
        for m in magnets:
            if not isinstance(m, Magnet):
                try:
                    m = Magnet.from_string(m)
                except ValueError:
                    log.error("Invalid magnet URL: %s", m)
                    continue
            funcs.append(partial(self.demagnetize2file, m, fntemplate))
        report = Report()
        if funcs:
            async with acollect(funcs) as ait:
                async for r in ait:
                    report += r
        return report

    async def demagnetize2file(self, magnet: Magnet, fntemplate: str) -> Report:
        try:
            torrent = await self.demagnetize(magnet)
            filename = template_torrent_filename(fntemplate, torrent)
            log.info(
                "Saving torrent for info hash %s to file %s", magnet.infohash, filename
            )
        except DemagnetizeFailure as e:
            log.error("%s", e)
            return Report.for_failure(magnet)
        try:
            with click.open_file(filename, "wb") as fp:
                torrent.write_stream(fp)
        except Exception as e:
            log.error("Error writing to file: %s: %s", type(e).__name__, e)
            return Report.for_failure(magnet)
        return Report.for_success(magnet, filename)

    async def demagnetize(self, magnet: Magnet) -> Torrent:
        session = self.open_session(magnet)
        md = await session.get_info()
        return compose_torrent(magnet, md)

    async def get_peers(self, tracker_url: str, info_hash: InfoHash) -> List[Peer]:
        tracker = Tracker.from_url(tracker_url)
        return await tracker.get_peers(self, info_hash)

    async def get_info_from_peer(self, peer: Peer, info_hash: InfoHash) -> bytes:
        info_piecer = InfoPiecer()
        n = await peer.get_info(self, info_hash, info_piecer)
        if info_piecer.done:
            return info_piecer.whole
        else:
            raise DemagnetizeFailure(
                f"Only received {n}/{info_piecer.piece_qty} pieces from peer"
            )

    def open_session(self, magnet: Magnet) -> TorrentSession:
        return TorrentSession(app=self, magnet=magnet)


def compose_torrent(magnet: Magnet, info: dict) -> Torrent:
    torrent = Torrent()
    torrent.metadata["info"] = info
    torrent.trackers = magnet.tr
    torrent.created_by = CLIENT
    torrent.creation_date = time()
    return torrent
