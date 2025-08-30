import asyncio
import logging
import re
from dataclasses import dataclass

from ..client import Client
from ..config import Config, QobuzDiscographyFilterConfig
from ..console import console
from ..db import Database
from ..exceptions import NonStreamableError
from ..metadata import ArtistMetadata
from .album import Album, PendingAlbum
from .media import Media, Pending

logger = logging.getLogger("streamrip")

# Resolve only N albums at a time to avoid
# initial latency of resolving ALL albums and tracks
# before any downloads
RESOLVE_CHUNK_SIZE = 10


@dataclass(slots=True)
class Artist(Media):
    """Represents a list of albums. Used by Artist and Label classes."""

    name: str
    albums: list[PendingAlbum]
    client: Client
    config: Config
    artist_id: str = None  # Store the artist ID for release tracking
    db: Database = None

    async def preprocess(self):
        pass

    async def download(self):
        filter_conf = self.config.session.qobuz_filters
        if filter_conf.repeats:
            console.log(
                "Resolving [purple]ALL[/purple] artist albums to detect repeats. This may take a while."
            )
            await self._resolve_then_download(filter_conf)
        else:
            await self._download_async(filter_conf)

    async def postprocess(self):
        # Check if all albums for this artist were successfully downloaded
        if self.artist_id and self.db:
            # For artists, we need to check if all albums were processed
            # This is complex since artist downloads are filtered, so we'll mark as complete
            # only if at least one album was processed (indicating successful artist resolution)
            if len(self.albums) > 0:
                source = getattr(self.client, 'source', 'unknown')
                self.db.set_release_downloaded(self.artist_id, "artist", source, len(self.albums))
                logger.info(f"Artist {self.artist_id} processed ({len(self.albums)} albums) - marked as complete")

    async def _resolve_then_download(self, filters: QobuzDiscographyFilterConfig):
        """Resolve all artist albums, then download.

        This is used if the repeat filter is turned on, since we need the titles
        of all albums to remove repeated items.
        """
        resolved_or_none: list[Album | None] = await asyncio.gather(
            *[album.resolve() for album in self.albums]
        )
        resolved = [a for a in resolved_or_none if a is not None]
        filtered_albums = self._apply_filters(resolved, filters)
        batches = self.batch([a.rip() for a in filtered_albums], RESOLVE_CHUNK_SIZE)
        for batch in batches:
            await asyncio.gather(*batch)

    async def _download_async(self, filters: QobuzDiscographyFilterConfig):
        async def _rip(item: PendingAlbum):
            album = await item.resolve()
            # Skip if album doesn't pass the filter
            if (
                album is None
                or (filters.extras and not self._extras(album))
                or (filters.features and not self._features(album))
                or (filters.non_studio_albums and not self._non_studio_albums(album))
                or (filters.non_remaster and not self._non_remaster(album))
            ):
                return
            await album.rip()

        batches = self.batch(
            [_rip(album) for album in self.albums],
            RESOLVE_CHUNK_SIZE,
        )
        for batch in batches:
            await asyncio.gather(*batch)

    def _apply_filters(
        self, albums: list[Album], filt: QobuzDiscographyFilterConfig
    ) -> list[Album]:
        _albums = albums
        if filt.repeats:
            _albums = self._filter_repeats(_albums)
        if filt.extras:
            _albums = filter(self._extras, _albums)
        if filt.features:
            _albums = filter(self._features, _albums)
        if filt.non_studio_albums:
            _albums = filter(self._non_studio_albums, _albums)
        if filt.non_remaster:
            _albums = filter(self._non_remaster, _albums)
        return list(_albums)

    # Will not fail on any nonempty string
    _essence_re = re.compile(r"([^\(\[]+)(?:\s*[\(\[][^\)][\)\]])*")

    @classmethod
    def _filter_repeats(cls, albums: list[Album]) -> list[Album]:
        """When there are different versions of an album on the artist,
        choose the one with the best quality.

        It determines that two albums are identical if they have the same title
        ignoring contents in brackets or parentheses.
        """
        groups: dict[str, list[Album]] = {}
        for a in albums:
            match = cls._essence_re.match(a.meta.album)
            assert match is not None
            title = match.group(1).strip().lower()
            items = groups.get(title, [])
            items.append(a)
            groups[title] = items

        unique_albums: list[Album] = []
        for group in groups.values():
            # Move explicit versions to the beginning
            group = sorted(
                group,
                key=lambda album: album.meta.info.explicit,
                reverse=True,
            )
            group = sorted(
                group,
                key=lambda album: album.meta.info.sampling_rate or 0,
                reverse=True,
            )
            group = sorted(
                group,
                key=lambda album: album.meta.info.bit_depth or 0,
                reverse=True,
            )
            # group guaranteed to be nonempty
            unique_albums.append(group[0])

        return unique_albums

    _extra_re = re.compile(
        r"(?i)(anniversary|deluxe|live|collector|demo|expanded|remix)"
    )

    # ----- Filter predicates -----
    def _non_studio_albums(self, a: Album) -> bool:
        """Filter out non studio albums."""
        return a.meta.albumartist != "Various Artists" and self._extras(a)

    def _features(self, a: Album) -> bool:
        """Filter out features."""
        return a.meta.albumartist == self.name

    def _extras(self, a: Album) -> bool:
        """Filter out extras.

        See `_extra_re` for criteria.
        """
        return self._extra_re.search(a.meta.album) is None

    _remaster_re = re.compile(r"(?i)(re)?master(ed)?")

    def _non_remaster(self, a: Album) -> bool:
        """Filter out albums that are not remasters."""
        return self._remaster_re.search(a.meta.album) is not None

    def _non_albums(self, a: Album) -> bool:
        """Filter out singles."""
        return len(a.tracks) > 1

    @staticmethod
    def batch(iterable, n=1):
        total = len(iterable)
        for ndx in range(0, total, n):
            yield iterable[ndx : min(ndx + n, total)]


@dataclass(slots=True)
class PendingArtist(Pending):
    id: str
    client: Client
    config: Config
    db: Database

    async def resolve(self) -> Artist | None:
        try:
            resp = await self.client.get_metadata(self.id, "artist")
        except NonStreamableError as e:
            logger.error(
                f"Artist {self.id} not available to stream on {self.client.source} ({e})",
            )
            return None

        try:
            meta = ArtistMetadata.from_resp(resp, self.client.source)
        except Exception as e:
            logger.error(
                f"Error building artist metadata: {e}",
            )
            return None

        album_ids = meta.album_ids()
        
        # Check if all albums are already downloaded - if so, log summary instead of per-album
        all_albums_downloaded = all(
            self.db.release_downloaded(album_id, "album", self.client.source) 
            for album_id in album_ids
        )
        if all_albums_downloaded and len(album_ids) > 0:
            logger.info(f"Artist {meta.name} ({self.id}) - all {len(album_ids)} albums already downloaded")
            return None
        
        # Log if we have some new albums to process
        new_albums = [
            album_id for album_id in album_ids
            if not self.db.release_downloaded(album_id, "album", self.client.source)
        ]
        if len(new_albums) > 0:
            logger.info(f"Artist {meta.name} ({self.id}) - found {len(new_albums)} new albums to download")
        
        albums = [
            PendingAlbum(album_id, self.client, self.config, self.db)
            for album_id in album_ids
        ]
        return Artist(meta.name, albums, self.client, self.config, self.id, self.db)
