import asyncio
import logging
from dataclasses import dataclass

from streamrip.exceptions import NonStreamableError

from ..client import Client
from ..config import Config
from ..db import Database
from ..metadata import LabelMetadata
from .album import PendingAlbum
from .media import Media, Pending

logger = logging.getLogger("streamrip")


@dataclass(slots=True)
class Label(Media):
    """Represents a list of albums. Used by Artist and Label classes."""

    name: str
    albums: list[PendingAlbum]
    client: Client
    config: Config
    label_id: str = None  # Store the label ID for release tracking
    db: Database = None

    async def preprocess(self):
        pass

    async def download(self):
        # Resolve only 3 albums at a time to avoid
        # initial latency of resolving ALL albums and tracks
        # before any downloads
        album_resolve_chunk_size = 10

        async def _resolve_download(item: PendingAlbum):
            album = await item.resolve()
            if album is None:
                return
            await album.rip()

        batches = self.batch(
            [_resolve_download(album) for album in self.albums],
            album_resolve_chunk_size,
        )
        for batch in batches:
            await asyncio.gather(*batch)

    async def postprocess(self):
        # Check if all albums for this label were successfully processed
        if self.label_id and self.db:
            # Mark label as complete if at least one album was processed
            if len(self.albums) > 0:
                source = getattr(self.client, 'source', 'unknown')
                self.db.set_release_downloaded(self.label_id, "label", source, len(self.albums))
                logger.info(f"Label {self.label_id} processed ({len(self.albums)} albums) - marked as complete")

    @staticmethod
    def batch(iterable, n=1):
        total = len(iterable)
        for ndx in range(0, total, n):
            yield iterable[ndx : min(ndx + n, total)]


@dataclass(slots=True)
class PendingLabel(Pending):
    id: str
    client: Client
    config: Config
    db: Database

    async def resolve(self) -> Label | None:
        try:
            resp = await self.client.get_metadata(self.id, "label")
        except NonStreamableError as e:
            logger.error(f"Error resolving Label: {e}")
            return None
        try:
            meta = LabelMetadata.from_resp(resp, self.client.source)
        except Exception as e:
            logger.error(f"Error resolving Label: {e}")
            return None
        album_ids = meta.album_ids()
        
        # Check if all albums are already downloaded - if so, log summary instead of per-album
        all_albums_downloaded = all(
            self.db.release_downloaded(album_id, "album", self.client.source) 
            for album_id in album_ids
        )
        if all_albums_downloaded and len(album_ids) > 0:
            logger.info(f"Label {meta.name} ({self.id}) - all {len(album_ids)} albums already downloaded")
            return None
        
        # Log if we have some new albums to process
        new_albums = [
            album_id for album_id in album_ids
            if not self.db.release_downloaded(album_id, "album", self.client.source)
        ]
        if len(new_albums) > 0:
            logger.info(f"Label {meta.name} ({self.id}) - found {len(new_albums)} new albums to download")
        
        albums = [
            PendingAlbum(album_id, self.client, self.config, self.db)
            for album_id in album_ids
        ]
        return Label(meta.name, albums, self.client, self.config, self.id, self.db)
