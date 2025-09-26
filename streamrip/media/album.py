import asyncio
import logging
import os
from dataclasses import dataclass

from .. import progress
from ..client import Client
from ..config import Config
from ..console import console
from ..db import Database
from ..exceptions import NonStreamableError
from ..filepath_utils import clean_filepath
from ..metadata import AlbumMetadata
from .artwork import download_artwork
from .media import Media, Pending
from .track import PendingTrack

logger = logging.getLogger("streamrip")


@dataclass(slots=True)
class Album(Media):
    meta: AlbumMetadata
    tracks: list[PendingTrack]
    config: Config
    # folder where the tracks will be downloaded
    folder: str
    db: Database


    async def preprocess(self):
        progress.add_title(self.meta.album)
        if self.config.session.cli.dry_run:
            await self._print_dry_run_info()

    async def download(self):
        async def _resolve_and_download(pending: Pending):
            try:
                track = await pending.resolve()
                if track is None:
                    return
                await track.rip()
            except Exception as e:
                logger.error(f"Error downloading track: {type(e).__name__}: {e}", exc_info=True)

        results = await asyncio.gather(
            *[_resolve_and_download(p) for p in self.tracks], return_exceptions=True
        )

        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Album track processing error: {result}")

    async def _print_dry_run_info(self):
        """Print album information for dry run mode."""
        console.print(f"[green]Would download album:[/green] [bold]{self.meta.album}[/bold]")
        console.print(f"  Artist: {self.meta.albumartist}")
        if hasattr(self.meta, 'year') and self.meta.year:
            console.print(f"  Year: {self.meta.year}")
        console.print(f"  Tracks: {len(self.tracks)}")
        if self.meta.disctotal > 1:
            console.print(f"  Discs: {self.meta.disctotal}")
        if hasattr(self.tracks[0], 'client') and hasattr(self.tracks[0].client, 'source'):
            console.print(f"  Source: {self.tracks[0].client.source}")
        console.print(f"  Folder: [dim]{self.folder}[/dim]")
        console.print("")

    async def postprocess(self):
        progress.remove_title(self.meta.album)

        if self.config.session.cli.dry_run:
            return

        # Check if all tracks in album were successfully downloaded
        track_ids = [track.id for track in self.tracks]
        downloaded_tracks = sum(1 for track_id in track_ids if self.db.downloaded(track_id))
        total_tracks = len(track_ids)

        # Only mark complete if ALL tracks succeeded
        if downloaded_tracks == total_tracks and total_tracks > 0:
            # Get album ID from meta or first track's album info
            album_id = getattr(self.meta, 'id', None)
            if album_id:
                # Get source from tracks' client
                source = getattr(self.tracks[0].client, 'source', 'unknown') if self.tracks else 'unknown'
                self.db.set_release_downloaded(album_id, "album", source, total_tracks)
                logger.info(f"Album {album_id} fully downloaded ({total_tracks} tracks) - marked as complete")
            else:
                logger.debug(f"Album completed but no ID available for tracking")


@dataclass(slots=True)
class PendingAlbum(Pending):
    id: str
    client: Client
    config: Config
    db: Database

    async def resolve(self) -> Album | None:
        # Check if this album is already fully downloaded
        if self.db.release_downloaded(self.id, "album", self.client.source):
            logger.info(f"Album {self.id} already fully downloaded - skipping")
            return None
            
        try:
            resp = await self.client.get_metadata(self.id, "album")
        except NonStreamableError as e:
            logger.error(
                f"Album {self.id} not available to stream on {self.client.source} ({e})",
            )
            return None

        try:
            meta = AlbumMetadata.from_album_resp(resp, self.client.source)
        except Exception as e:
            logger.error(f"Error building album metadata for {id=}: {e}")
            return None

        # Check if album is streamable
        if not meta.info.streamable:
            logger.error(f"Album '{meta.album}' by {meta.albumartist} [{self.id}] not available for stream on {self.client.source}")
            return None

        tracklist = [track["id"] for track in resp["tracks"]]
        
        # Check if all tracks are already downloaded (edge case for pre-optimization downloads)
        all_tracks_downloaded = all(self.db.downloaded(track_id) for track_id in tracklist)
        if all_tracks_downloaded and len(tracklist) > 0:
            logger.info(f"Album {self.id} has all tracks already downloaded - marking as complete")
            self.db.set_release_downloaded(self.id, "album", self.client.source, len(tracklist))
            return None
        
        folder = self.config.session.downloads.folder
        album_folder = self._album_folder(folder, meta)
        os.makedirs(album_folder, exist_ok=True)
        embed_cover, _ = await download_artwork(
            self.client.session,
            album_folder,
            meta.covers,
            self.config.session.artwork,
            for_playlist=False,
        )
        pending_tracks = [
            PendingTrack(
                id,
                album=meta,
                client=self.client,
                config=self.config,
                folder=album_folder,
                db=self.db,
                cover_path=embed_cover,
            )
            for id in tracklist
        ]
        logger.debug("Pending tracks: %s", pending_tracks)
        return Album(meta, pending_tracks, self.config, album_folder, self.db)

    def _album_folder(self, parent: str, meta: AlbumMetadata) -> str:
        config = self.config.session
        if config.downloads.source_subdirectories:
            parent = os.path.join(parent, self.client.source.capitalize())
        formatter = config.filepaths.folder_format
        folder = clean_filepath(
            meta.format_folder_path(formatter), config.filepaths.restrict_characters
        )

        return os.path.join(parent, folder)
