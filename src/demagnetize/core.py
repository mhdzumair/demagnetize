from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from random import randint
from anyio import CapacityLimiter
import attr
import click
from torf import Magnet, Torrent
from .consts import CLIENT, MAGNET_LIMIT
from .errors import DemagnetizeError
from .session import TorrentSession
from .util import Key, Report, acollect, log, make_peer_id, template_torrent_filename


@attr.define
class Demagnetizer:
    key: Key = attr.Factory(Key.generate)
    peer_id: bytes = attr.Factory(make_peer_id)
    peer_port: int = attr.Factory(lambda: randint(1025, 65535))

    def __attrs_post_init__(self) -> None:
        log.debug("Using key = %s", self.key)
        log.debug("Using peer ID = %r", self.peer_id)
        log.debug("Using peer port = %d", self.peer_port)

    async def download_torrent_info(
        self, magnets: list[Magnet], fntemplate: str
    ) -> Report:
        report = Report()
        coros = [self.demagnetize2file(m, fntemplate) for m in magnets]
        async with acollect(coros, limit=CapacityLimiter(MAGNET_LIMIT)) as ait:
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
        except DemagnetizeError as e:
            log.error("%s", e)
            return Report.for_failure(magnet)
        try:
            Path(filename).parent.mkdir(parents=True, exist_ok=True)
            with click.open_file(filename, "wb") as fp:
                torrent.write_stream(fp)
        except Exception as e:
            log.error(
                "Error writing torrent to file %r: %s: %s",
                filename,
                type(e).__name__,
                e,
            )
            return Report.for_failure(magnet)
        return Report.for_success(magnet, filename)

    async def demagnetize(self, magnet: Magnet) -> Torrent:
        session = self.open_session(magnet)
        md = await session.get_info()
        return compose_torrent(magnet, md)

    def open_session(self, magnet: Magnet) -> TorrentSession:
        return TorrentSession(app=self, magnet=magnet)


def compose_torrent(magnet: Magnet, info: dict) -> Torrent:
    torrent = Torrent()
    
    # Convert byte keys to string keys for torf compatibility
    # torf expects string keys, but info dicts from peers use byte keys
    string_key_info = convert_byte_keys_to_strings(info)
    
    # Log the piece length for debugging
    piece_length = string_key_info.get("piece length")
    if piece_length and piece_length % 16384 != 0:
        log.info("Using non-standard piece length: %d (not divisible by 16384)", piece_length)
    
    torrent.metainfo["info"] = string_key_info
    torrent.trackers = magnet.tr  # type: ignore[assignment]
    torrent.created_by = CLIENT
    torrent.creation_date = datetime.now(tz=timezone.utc)
    return torrent


def convert_byte_keys_to_strings(info: dict) -> dict:
    """
    Convert byte keys to string keys for torf compatibility.
    
    Info dicts from peers use byte keys, but torf expects string keys.
    This function recursively converts byte keys to strings.
    """
    converted = {}
    for key, value in info.items():
        # Convert byte keys to string keys
        if isinstance(key, bytes):
            try:
                string_key = key.decode('utf-8')
            except UnicodeDecodeError:
                # If decoding fails, keep as bytes (shouldn't happen for standard keys)
                string_key = key
        else:
            string_key = key
            
        # Handle nested dictionaries and lists
        if isinstance(value, dict):
            converted[string_key] = convert_byte_keys_to_strings(value)
        elif isinstance(value, list):
            converted[string_key] = [
                convert_byte_keys_to_strings(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            converted[string_key] = value
            
    return converted


