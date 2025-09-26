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
from .media import CollectionMedia, Pending

logger = logging.getLogger("streamrip")

# Resolve only N albums at a time to avoid
# initial latency of resolving ALL albums and tracks
# before any downloads
RESOLVE_CHUNK_SIZE = 10


@dataclass(slots=True)
class Artist(CollectionMedia):
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
        self._mark_collection_complete(self.artist_id, "artist")

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

        # Check if all albums are downloaded and log appropriately
        if self.filter_and_log_albums(album_ids, self.db, self.client.source, meta.name, self.id):
            return None

        albums = [
            PendingAlbum(album_id, self.client, self.config, self.db)
            for album_id in album_ids
        ]
        return Artist(meta.name, albums, self.client, self.config, self.id, self.db)

    async def stream_albums(self):
        """Async generator that yields albums as they're resolved.

        This enables true streaming: albums start downloading as soon as they're
        discovered, rather than waiting for all albums to be resolved first.
        """
        try:
            resp = await self.client.get_metadata(self.id, "artist")
        except NonStreamableError as e:
            logger.error(
                f"Artist {self.id} not available to stream on {self.client.source} ({e})",
            )
            return

        try:
            meta = ArtistMetadata.from_resp(resp, self.client.source)
        except Exception as e:
            logger.error(
                f"Error building artist metadata: {e}",
            )
            return

        album_ids = meta.album_ids()
        artist_name = meta.name

        # Get filters for this artist
        filter_conf = self.config.session.qobuz_filters

        # If repeat filtering is enabled, we need to resolve all albums first
        # to detect duplicates. Otherwise we can stream them.
        if filter_conf.repeats:
            logger.info(f"Resolving all albums for {artist_name} to detect repeats...")
            # Resolve all albums to apply repeat filter
            pending_albums = [
                PendingAlbum(album_id, self.client, self.config, self.db)
                for album_id in album_ids
            ]
            resolved_albums = await asyncio.gather(
                *[album.resolve() for album in pending_albums],
                return_exceptions=True
            )
            valid_albums = [a for a in resolved_albums if isinstance(a, Album)]

            # Apply filters including repeat removal
            filtered_albums = self._apply_filters_to_albums(valid_albums, filter_conf, artist_name)

            # Yield filtered albums
            for album in filtered_albums:
                yield album
        else:
            # Stream albums one by one, applying filters as we go
            for album_id in album_ids:
                # Check if already downloaded
                if self.db.downloaded(album_id):
                    logger.debug(f"Album {album_id} already downloaded, skipping")
                    continue

                try:
                    pending_album = PendingAlbum(album_id, self.client, self.config, self.db)
                    album = await pending_album.resolve()

                    if album is None:
                        continue

                    # Apply filters (except repeats which requires all albums)
                    if self._should_include_album(album, filter_conf, artist_name):
                        yield album

                except Exception as e:
                    logger.error(f"Error resolving album {album_id}: {e}")
                    continue

    def _apply_filters_to_albums(self, albums: list[Album], filters, artist_name: str) -> list[Album]:
        """Apply all filters to a list of albums (used when repeat filtering is enabled)."""
        _albums = albums
        if filters.repeats:
            _albums = Artist._filter_repeats(_albums)
        if filters.extras:
            _albums = [a for a in _albums if self._extras_for_album(a)]
        if filters.features:
            _albums = [a for a in _albums if a.meta.albumartist == artist_name]
        if filters.non_studio_albums:
            _albums = [a for a in _albums if a.meta.albumartist != "Various Artists" and self._extras_for_album(a)]
        if filters.non_remaster:
            _albums = [a for a in _albums if self._non_remaster_for_album(a)]
        return _albums

    def _should_include_album(self, album: Album, filters, artist_name: str) -> bool:
        """Check if an individual album should be included (for streaming mode)."""
        if filters.extras and not self._extras_for_album(album):
            return False
        if filters.features and album.meta.albumartist != artist_name:
            return False
        if filters.non_studio_albums and (album.meta.albumartist == "Various Artists" or not self._extras_for_album(album)):
            return False
        if filters.non_remaster and not self._non_remaster_for_album(album):
            return False
        return True

    def _extras_for_album(self, album: Album) -> bool:
        """Filter out extras for a single album."""
        return Artist._extra_re.search(album.meta.album) is None

    def _non_remaster_for_album(self, album: Album) -> bool:
        """Check if album is a remaster."""
        return Artist._remaster_re.search(album.meta.album) is not None
