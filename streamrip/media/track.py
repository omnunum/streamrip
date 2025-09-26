import asyncio
import logging
import os
from dataclasses import dataclass

from .. import converter
from ..client import Client, Downloadable
from ..config import Config
from ..console import console
from ..db import Database
from ..exceptions import NonStreamableError
from ..filepath_utils import clean_filename
from ..metadata import AlbumMetadata, Covers, TrackMetadata, tag_file
from ..progress import add_title, get_progress_callback, remove_title
from ..utils.audio_validator import validate_audio_file
from .artwork import download_artwork
from .media import Media, Pending
from .semaphore import global_download_semaphore

logger = logging.getLogger("streamrip")


@dataclass(slots=True)
class Track(Media):
    meta: TrackMetadata
    downloadable: Downloadable
    config: Config
    folder: str
    # Is None if a cover doesn't exist for the track
    cover_path: str | None
    db: Database
    # change?
    download_path: str = ""
    is_single: bool = False

    async def preprocess(self):
        self._set_download_path()
        if not self.config.session.cli.dry_run:
            os.makedirs(self.folder, exist_ok=True)
        if self.is_single:
            add_title(self.meta.title)

    async def download(self):
        if self.config.session.cli.dry_run:
            await self._print_dry_run_info()
            return

        # TODO: progress bar description
        async with global_download_semaphore(self.config.session.downloads):
            with get_progress_callback(
                self.config.session.cli.progress_bars,
                await self.downloadable.size(),
                f"Track {self.meta.tracknumber}",
            ) as callback:
                try:
                    await self.downloadable.download(self.download_path, callback)
                    retry = False
                except Exception as e:
                    logger.error(
                        f"Error downloading track '{self.meta.title}', retrying: {e}"
                    )
                    retry = True

            if not retry:
                return

            with get_progress_callback(
                self.config.session.cli.progress_bars,
                await self.downloadable.size(),
                f"Track {self.meta.tracknumber} (retry)",
            ) as callback:
                try:
                    await self.downloadable.download(self.download_path, callback)
                except Exception as e:
                    logger.error(
                        f"Persistent error downloading track '{self.meta.title}', skipping: {e}"
                    )
                    self.db.set_failed(
                        self.downloadable.source, "track", self.meta.info.id
                    )

    async def _print_dry_run_info(self):
        """Print track information for dry run mode."""
        try:
            size_mb = (await self.downloadable.size()) / (1024 * 1024)
            size_str = f"{size_mb:.1f} MB"
        except:
            size_str = "Unknown size"

        quality_info = f"Quality: {self.meta.info.quality}"
        if hasattr(self.meta.info, 'bit_depth') and hasattr(self.meta.info, 'sampling_rate'):
            if self.meta.info.bit_depth and self.meta.info.sampling_rate:
                quality_info += f" ({self.meta.info.bit_depth}-bit/{self.meta.info.sampling_rate//1000}kHz)"

        console.print(f"[cyan]Would download:[/cyan] [bold]{self.meta.title}[/bold]")
        console.print(f"  Artist: {self.meta.artist}")
        console.print(f"  Album: {self.meta.album.album}")
        console.print(f"  Track: {self.meta.tracknumber}/{self.meta.album.tracktotal}")
        if self.meta.album.disctotal > 1:
            console.print(f"  Disc: {self.meta.discnumber}/{self.meta.album.disctotal}")
        console.print(f"  Source: {self.downloadable.source}")
        console.print(f"  {quality_info}")
        console.print(f"  Format: {self.meta.info.container}")
        console.print(f"  Size: {size_str}")
        console.print(f"  Path: [dim]{self.download_path}[/dim]")
        console.print("")

    async def postprocess(self):
        if self.is_single:
            remove_title(self.meta.title)

        if self.config.session.cli.dry_run:
            return

        # Validate audio file if enabled
        if self.config.session.downloads.validate_audio:
            await self._validate_audio_file()

        await tag_file(self.download_path, self.meta, self.cover_path)
        if self.config.session.conversion.enabled:
            await self._convert()

        self.db.set_downloaded(self.meta.info.id)

    async def _validate_audio_file(self):
        """Validate the downloaded audio file for corruption."""
        if not os.path.exists(self.download_path):
            logger.error(f"Audio file not found for validation: {self.download_path}")
            self.db.set_failed(self.downloadable.source, "track", self.meta.info.id)
            return

        logger.debug(f"Validating audio file: {self.download_path}")
        validation_result = await validate_audio_file(self.download_path)

        if not validation_result.is_valid:
            error_msg = f"Audio validation failed for '{self.meta.title}' by {self.meta.artist}"
            if validation_result.error_message:
                error_msg += f": {validation_result.error_message}"
            if validation_result.validation_method:
                error_msg += f" (method: {validation_result.validation_method})"

            logger.error(error_msg)

            # Delete invalid file if configured
            if self.config.session.downloads.delete_invalid_files:
                try:
                    os.remove(self.download_path)
                    logger.debug(f"Deleted invalid audio file: {self.download_path}")
                except OSError as e:
                    logger.warning(f"Failed to delete invalid file {self.download_path}: {e}")

            # Mark as failed and potentially retry
            self.db.set_failed(self.downloadable.source, "track", self.meta.info.id)

            # Retry download if configured
            if self.config.session.downloads.retry_on_validation_failure:
                logger.info(f"Retrying download due to validation failure: '{self.meta.title}'")
                await self._retry_download()
            else:
                raise Exception(f"Audio validation failed: {validation_result.error_message}")
        else:
            logger.debug(f"Audio validation passed: {self.download_path} (method: {validation_result.validation_method})")

    async def _retry_download(self):
        """Retry downloading the track after validation failure."""
        try:
            # Use the same progress callback logic as in the original download method
            with get_progress_callback(
                self.config.session.cli.progress_bars,
                await self.downloadable.size(),
                f"Track {self.meta.tracknumber} (retry after validation failure)",
            ) as callback:
                await self.downloadable.download(self.download_path, callback)

            # Validate the retry
            validation_result = await validate_audio_file(self.download_path)
            if not validation_result.is_valid:
                # Still invalid after retry
                error_msg = f"Audio validation failed again after retry for '{self.meta.title}': {validation_result.error_message}"
                logger.error(error_msg)

                # Delete invalid file
                if self.config.session.downloads.delete_invalid_files and os.path.exists(self.download_path):
                    try:
                        os.remove(self.download_path)
                        logger.debug(f"Deleted invalid audio file after retry: {self.download_path}")
                    except OSError as e:
                        logger.warning(f"Failed to delete invalid file after retry {self.download_path}: {e}")

                raise Exception(error_msg)
            else:
                logger.info(f"Retry successful - audio validation passed: '{self.meta.title}'")

        except Exception as e:
            logger.error(f"Retry download failed for '{self.meta.title}': {e}")
            # Keep the track marked as failed
            raise

    async def _convert(self):
        c = self.config.session.conversion
        engine_class = converter.get(c.codec)
        engine = engine_class(
            filename=self.download_path,
            sampling_rate=c.sampling_rate,
            bit_depth=c.bit_depth,
            remove_source=True,  # always going to delete the old file
        )
        await engine.convert()
        self.download_path = engine.final_fn  # because the extension changed

    def _set_download_path(self):
        c = self.config.session.filepaths
        formatter = c.track_format
        track_path = clean_filename(
            self.meta.format_track_path(formatter),
            restrict=c.restrict_characters,
        )
        if c.truncate_to > 0 and len(track_path) > c.truncate_to:
            track_path = track_path[: c.truncate_to]

        self.download_path = os.path.join(
            self.folder,
            f"{track_path}.{self.downloadable.extension}",
        )


@dataclass(slots=True)
class PendingTrack(Pending):
    id: str
    album: AlbumMetadata
    client: Client
    config: Config
    folder: str
    db: Database
    # cover_path is None <==> Artwork for this track doesn't exist in API
    cover_path: str | None

    async def resolve(self) -> Track | None:
        if self.db.downloaded(self.id):
            logger.info(
                f"Skipping track {self.id}. Marked as downloaded in the database.",
            )
            return None

        source = self.client.source
        try:
            resp = await self.client.get_metadata(self.id, "track")
        except NonStreamableError as e:
            logger.error(f"Track {self.id} not available for stream on {source}: {e}")
            return None

        try:
            meta = TrackMetadata.from_resp(self.album, source, resp)
        except Exception as e:
            logger.error(f"Error building track metadata for {self.id}: {e}")
            return None

        # Check if track is streamable
        if not meta.info.streamable:
            logger.error(f"Track '{meta.title}' by {meta.artist} (Album: {meta.album.album}) [{self.id}] not available for stream on {source}")
            self.db.set_failed(source, "track", self.id)
            return None

        # Check quality requirements and select appropriate quality
        source_config = self.config.session.get_source(source)
        requested_quality = source_config.quality
        lower_quality_fallback = getattr(source_config, 'lower_quality_if_not_available', False)
        
        # meta.info.quality contains the highest available quality for this track
        # Now select the actual quality to download
        if meta.info.quality < requested_quality and not lower_quality_fallback:
            logger.error(f"Track '{meta.title}' by {meta.artist} (Album: {meta.album.album}) [{self.id}]: Quality {meta.info.quality} available but {requested_quality} requested - skipping due to lower_quality_if_not_available=false")
            self.db.set_failed(source, "track", self.id)
            return None

        # Select the quality to download: min of requested and available
        quality = min(requested_quality, meta.info.quality)
        
        # Get downloadable info for the determined quality
        try:
            downloadable = await self.client.get_downloadable(self.id, quality)

        except NonStreamableError as e:
            logger.error(
                f"Error getting downloadable data for track {meta.tracknumber} '{meta.title}' by {meta.artist} (Album: {meta.album.album}) [{self.id}]: {e}"
            )
            self.db.set_failed(source, "track", self.id)
            return None

        # Update container format based on actual downloadable format
        meta.info.container = downloadable.extension.upper()

        downloads_config = self.config.session.downloads
        if downloads_config.disc_subdirectories and self.album.disctotal > 1:
            folder = os.path.join(self.folder, f"Disc {meta.discnumber}")
        else:
            folder = self.folder

        return Track(
            meta,
            downloadable,
            self.config,
            folder,
            self.cover_path,
            self.db,
        )


@dataclass(slots=True)
class PendingSingle(Pending):
    """Whereas PendingTrack is used in the context of an album, where the album metadata
    and cover have been resolved, PendingSingle is used when a single track is downloaded.

    This resolves the Album metadata and downloads the cover to pass to the Track class.
    """

    id: str
    client: Client
    config: Config
    db: Database

    async def resolve(self) -> Track | None:
        if self.db.downloaded(self.id):
            logger.info(
                f"Skipping track {self.id}. Marked as downloaded in the database.",
            )
            return None

        try:
            resp = await self.client.get_metadata(self.id, "track")
        except NonStreamableError as e:
            logger.error(f"Error fetching track {self.id}: {e}")
            return None
        # Patch for soundcloud
        try:
            album = AlbumMetadata.from_track_resp(resp, self.client.source)
        except Exception as e:
            logger.error(f"Error building album metadata for track {id=}: {e}")
            return None

        if album is None:
            self.db.set_failed(self.client.source, "track", self.id)
            logger.error(
                f"Cannot stream track (am) ({self.id}) on {self.client.source}",
            )
            return None

        try:
            meta = TrackMetadata.from_resp(album, self.client.source, resp)
        except Exception as e:
            logger.error(f"Error building track metadata for track {id=}: {e}")
            return None

        # Check if track is streamable
        if not meta.info.streamable:
            logger.error(f"Track '{meta.title}' by {meta.artist} (Album: {meta.album.album}) [{self.id}] not available for stream on {self.client.source}")
            self.db.set_failed(self.client.source, "track", self.id)
            return None

        # Check quality requirements and select appropriate quality
        source_config = self.config.session.get_source(self.client.source)
        requested_quality = source_config.quality
        lower_quality_fallback = getattr(source_config, 'lower_quality_if_not_available', False)
        
        # meta.info.quality contains the highest available quality for this track
        # Now select the actual quality to download
        if meta.info.quality < requested_quality and not lower_quality_fallback:
            logger.error(f"Track '{meta.title}' by {meta.artist} (Album: {meta.album.album}) [{self.id}]: Quality {meta.info.quality} available but {requested_quality} requested - skipping due to lower_quality_if_not_available=false")
            self.db.set_failed(self.client.source, "track", self.id)
            return None

        # Select the quality to download: min of requested and available
        quality = min(requested_quality, meta.info.quality)
        
        config = self.config.session
        parent = config.downloads.folder
        if config.filepaths.add_singles_to_folder:
            folder = self._format_folder(album)
        else:
            folder = parent

        os.makedirs(folder, exist_ok=True)

        embedded_cover_path, downloadable = await asyncio.gather(
            self._download_cover(album.covers, folder),
            self.client.get_downloadable(self.id, quality),
        )
        
        # Update container format based on actual downloadable format
        meta.info.container = downloadable.extension.upper()
        
        return Track(
            meta,
            downloadable,
            self.config,
            folder,
            embedded_cover_path,
            self.db,
            is_single=True,
        )

    def _format_folder(self, meta: AlbumMetadata) -> str:
        c = self.config.session
        parent = c.downloads.folder
        formatter = c.filepaths.folder_format
        if c.downloads.source_subdirectories:
            parent = os.path.join(parent, self.client.source.capitalize())

        return os.path.join(parent, meta.format_folder_path(formatter))

    async def _download_cover(self, covers: Covers, folder: str) -> str | None:
        embed_path, _ = await download_artwork(
            self.client.session,
            folder,
            covers,
            self.config.session.artwork,
            for_playlist=False,
        )
        return embed_path
