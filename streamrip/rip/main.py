import asyncio
import json
import logging
import platform

import aiofiles

from .. import db
from ..client import Client, DeezerClient, QobuzClient, SoundcloudClient, TidalClient
from ..config import Config
from ..console import console
from ..media import (
    Media,
    Pending,
    PendingAlbum,
    PendingArtist,
    PendingLabel,
    PendingLastfmPlaylist,
    PendingPlaylist,
    PendingSingle,
    remove_artwork_tempdirs,
)
from ..metadata import SearchResults
from ..progress import clear_progress
from .parse_url import parse_url
from .prompter import get_prompter

logger = logging.getLogger("streamrip")

if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class Main:
    """Provides all of the functionality called into by the CLI.

    * Logs in to Clients and prompts for credentials
    * Handles output logging
    * Handles downloading Media
    * Handles interactive search

    User input (urls) -> Main --> Download files & Output messages to terminal
    """

    def __init__(self, config: Config):
        # Data pipeline:
        # input URL -> (URL) -> (Pending) -> (Media) -> (Downloadable) -> audio file
        self.pending: list[Pending] = []
        self.media: list[Media] = []
        self.config = config
        self.clients: dict[str, Client] = {
            "qobuz": QobuzClient(config),
            "tidal": TidalClient(config),
            "deezer": DeezerClient(config),
            "soundcloud": SoundcloudClient(config),
        }

        # Initialize RYM service if enabled (singleton for session)
        self.rym_service = None
        if config.session.rym.enabled:
            try:
                from ..config import APP_DIR
                from ..metadata.rym_service import RymMetadataService
                self.rym_service = RymMetadataService(config.session.rym, APP_DIR)
                logger.info("RYM metadata enrichment enabled")
            except ImportError:
                logger.warning("RYM metadata enrichment enabled but rym_metadata package not available")
            except Exception as e:
                logger.warning(f"Failed to initialize RYM service: {e}")

        self.database: db.Database

        c = self.config.session.database
        if c.downloads_enabled:
            downloads_db = db.Downloads(c.downloads_path)
        else:
            downloads_db = db.Dummy()

        if c.failed_downloads_enabled:
            failed_downloads_db = db.Failed(c.failed_downloads_path)
        else:
            failed_downloads_db = db.Dummy()

        # Use same path as downloads for releases table
        if c.downloads_enabled:
            releases_db = db.DownloadedReleases(c.downloads_path.replace('.db', '_releases.db'))
        else:
            releases_db = db.Dummy()

        self.database = db.Database(downloads_db, failed_downloads_db, releases_db)

    async def add(self, url: str):
        """Add url as a pending item.

        Do not `asyncio.gather` calls to this! Use `add_all` for concurrency.
        """
        parsed = parse_url(url)
        if parsed is None:
            raise Exception(f"Unable to parse url {url}")

        client = await self.get_logged_in_client(parsed.source)
        self.pending.append(
            await parsed.into_pending(client, self.config, self.database),
        )
        logger.debug("Added url=%s", url)

    async def add_by_id(self, source: str, media_type: str, id: str):
        client = await self.get_logged_in_client(source)
        self._add_by_id_client(client, media_type, id)

    async def add_all_by_id(self, info: list[tuple[str, str, str]]):
        sources = set(s for s, _, _ in info)
        clients = {s: await self.get_logged_in_client(s) for s in sources}
        for source, media_type, id in info:
            self._add_by_id_client(clients[source], media_type, id)

    def _add_by_id_client(self, client: Client, media_type: str, id: str):
        if media_type == "track":
            item = PendingSingle(id, client, self.config, self.database)
        elif media_type == "album":
            item = PendingAlbum(id, client, self.config, self.database)
        elif media_type == "playlist":
            item = PendingPlaylist(id, client, self.config, self.database)
        elif media_type == "label":
            item = PendingLabel(id, client, self.config, self.database)
        elif media_type == "artist":
            item = PendingArtist(id, client, self.config, self.database)
        else:
            raise Exception(media_type)

        self.pending.append(item)

    async def add_all(self, urls: list[str]):
        """Add multiple urls concurrently as pending items."""
        parsed = [parse_url(url) for url in urls]
        url_client_pairs = []
        for i, p in enumerate(parsed):
            if p is None:
                console.print(
                    f"[red]Found invalid url [cyan]{urls[i]}[/cyan], skipping.",
                )
                continue
            url_client_pairs.append((p, await self.get_logged_in_client(p.source)))

        pendings = await asyncio.gather(
            *[
                url.into_pending(client, self.config, self.database)
                for url, client in url_client_pairs
            ],
        )
        self.pending.extend(pendings)

    async def get_logged_in_client(self, source: str):
        """Return a functioning client instance for `source`."""
        client = self.clients.get(source)
        if client is None:
            raise Exception(
                f"No client named {source} available. Only have {self.clients.keys()}",
            )
        if not client.logged_in:
            prompter = get_prompter(client, self.config)
            if not prompter.has_creds():
                # Get credentials from user and log into client
                await prompter.prompt_and_login()
                prompter.save()
            else:
                with console.status(f"[cyan]Logging into {source}", spinner="dots"):
                    # Log into client using credentials from config
                    await client.login()

        assert client.logged_in
        return client

    async def resolve(self):
        """Resolve all currently pending items."""
        with console.status("Resolving URLs...", spinner="dots"):
            coros = [p.resolve() for p in self.pending]
            new_media: list[Media] = [
                m for m in await asyncio.gather(*coros) if m is not None
            ]

        self.media.extend(new_media)
        self.pending.clear()

    async def rip(self):
        """Download all resolved items."""
        results = await asyncio.gather(
            *[item.rip() for item in self.media], return_exceptions=True
        )

        failed_items = 0
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Error processing media item: {result}")
                failed_items += 1

        if failed_items > 0:
            total_items = len(self.media)
            logger.info(
                f"Download completed with {failed_items} failed items out of {total_items} total items."
            )

    async def resolve_enrich_and_rip(self):
        """Pipeline: resolve → RYM enrich → download for each item"""

        async def process_item(pending):
            try:
                # Step 1: Get metadata from streaming service
                media = await pending.resolve()
                if not media:
                    return None

                # Step 2: Enrich with RYM data (if enabled)
                if self.rym_service:
                    # For albums, enrich the album metadata directly
                    if hasattr(media.meta, 'enrich_with_rym'):
                        await media.meta.enrich_with_rym(self.rym_service)
                    # For tracks, enrich the album metadata within the track
                    elif hasattr(media.meta, 'album') and hasattr(media.meta.album, 'enrich_with_rym'):
                        await media.meta.album.enrich_with_rym(self.rym_service)

                # Step 3: Download audio and tag with enriched metadata
                await media.rip()
                return media

            except Exception as e:
                logger.error(f"Pipeline error for {pending}: {e}")
                return None

        # Process all items in parallel pipeline
        with console.status("Processing downloads...", spinner="dots"):
            results = await asyncio.gather(
                *[process_item(p) for p in self.pending],
                return_exceptions=True
            )

        # Handle results and cleanup
        successful_items = [r for r in results if r is not None and not isinstance(r, Exception)]
        failed_count = len(results) - len(successful_items)

        if failed_count > 0:
            logger.warning(f"{failed_count} items failed to process")

        self.media.extend(successful_items)
        self.pending.clear()

    async def stream_process_urls(self, urls: list[str]):
        """True streaming pipeline: process URLs and download items as they're discovered.

        This eliminates all blocking by streaming items immediately rather than
        batching them. Each album/track starts downloading as soon as it's discovered.
        """
        # Track all download tasks to ensure they complete
        download_tasks = set()

        async def process_single_item(media_item):
            """Process a single album or track with RYM enrichment"""
            try:
                # RYM enrichment for albums and tracks
                if hasattr(media_item, 'meta') and self.rym_service:
                    # For albums, enrich the album metadata directly
                    if hasattr(media_item.meta, 'enrich_with_rym'):
                        await media_item.meta.enrich_with_rym(self.rym_service)
                    # For tracks, enrich the album metadata within the track
                    elif hasattr(media_item.meta, 'album') and hasattr(media_item.meta.album, 'enrich_with_rym'):
                        await media_item.meta.album.enrich_with_rym(self.rym_service)

                # Download the item
                await media_item.rip()
                return media_item
            except Exception as e:
                logger.error(f"Error processing item {media_item}: {e}")
                return None

        async def stream_from_url(url: str):
            """Stream items from a single URL as they're discovered"""
            try:
                parsed = parse_url(url)
                if parsed is None:
                    logger.error(f"Unable to parse url {url}")
                    return

                client = await self.get_logged_in_client(parsed.source)
                pending = await parsed.into_pending(client, self.config, self.database)

                # Handle different URL types with streaming
                if hasattr(pending, 'stream_albums'):
                    # Artist/Label URLs - stream albums as discovered
                    async for album in pending.stream_albums():
                        # Start processing this album immediately
                        task = asyncio.create_task(process_single_item(album))
                        download_tasks.add(task)
                        # Remove completed tasks to prevent memory buildup
                        task.add_done_callback(download_tasks.discard)

                elif hasattr(pending, 'stream_tracks'):
                    # Playlist URLs - stream tracks as discovered
                    async for track in pending.stream_tracks():
                        # Process track immediately
                        task = asyncio.create_task(process_single_item(track))
                        download_tasks.add(task)
                        # Remove completed tasks to prevent memory buildup
                        task.add_done_callback(download_tasks.discard)

                else:
                    # Single album/track - resolve and process
                    media = await pending.resolve()
                    if media:
                        await process_single_item(media)

            except Exception as e:
                logger.error(f"Error streaming from URL {url}: {e}")

        # Process all URLs concurrently in streaming fashion
        # Create tasks for each URL to process concurrently
        url_tasks = [asyncio.create_task(stream_from_url(url)) for url in urls]

        # Wait for all URL discovery to complete
        await asyncio.gather(*url_tasks, return_exceptions=True)

        # Now wait for all download tasks to complete
        if download_tasks:
            logger.info(f"Waiting for {len(download_tasks)} downloads to complete...")
            await asyncio.gather(*download_tasks, return_exceptions=True)

        logger.info("All streaming downloads completed")

    async def stream_process_single_url(self, url: str):
        """Stream process a single URL - convenience method"""
        await self.stream_process_urls([url])

    async def stream_process_pending(self):
        """Stream process all currently pending items with RYM enrichment.

        This is used for search and ID commands that add items to pending
        without URLs.
        """
        if not self.pending:
            return

        download_tasks = set()

        async def process_single_item(media_item):
            """Process a single album or track with RYM enrichment"""
            try:
                # RYM enrichment for albums and tracks
                if hasattr(media_item, 'meta') and self.rym_service:
                    # For albums, enrich the album metadata directly
                    if hasattr(media_item.meta, 'enrich_with_rym'):
                        await media_item.meta.enrich_with_rym(self.rym_service)
                    # For tracks, enrich the album metadata within the track
                    elif hasattr(media_item.meta, 'album') and hasattr(media_item.meta.album, 'enrich_with_rym'):
                        await media_item.meta.album.enrich_with_rym(self.rym_service)

                # Download the item
                await media_item.rip()
                return media_item
            except Exception as e:
                logger.error(f"Error processing item {media_item}: {e}")
                return None

        # Process each pending item
        for pending in self.pending:
            try:
                # Resolve the pending item
                media = await pending.resolve()
                if media:
                    # Start processing this item immediately
                    task = asyncio.create_task(process_single_item(media))
                    download_tasks.add(task)
                    # Remove completed tasks to prevent memory buildup
                    task.add_done_callback(download_tasks.discard)

            except Exception as e:
                logger.error(f"Error resolving pending item {pending}: {e}")

        # Wait for all download tasks to complete
        if download_tasks:
            logger.info(f"Waiting for {len(download_tasks)} downloads to complete...")
            await asyncio.gather(*download_tasks, return_exceptions=True)

        # Clear pending items
        self.pending.clear()
        logger.info("All streaming downloads completed")

    async def stream_process_lastfm(self, playlist_url: str):
        """Stream process a Last.fm playlist URL with RYM enrichment."""
        c = self.config.session.lastfm
        client = await self.get_logged_in_client(c.source)

        if len(c.fallback_source) > 0:
            fallback_client = await self.get_logged_in_client(c.fallback_source)
        else:
            fallback_client = None

        pending_playlist = PendingLastfmPlaylist(
            playlist_url,
            client,
            fallback_client,
            self.config,
            self.database,
        )

        # Track all download tasks to ensure they complete
        download_tasks = set()

        async def process_single_item(media_item):
            """Process a single track with RYM enrichment"""
            try:
                # RYM enrichment for tracks
                if hasattr(media_item, 'meta') and hasattr(media_item.meta, 'enrich_with_rym'):
                    if self.rym_service:
                        await media_item.meta.enrich_with_rym(self.rym_service)

                # Download the item
                await media_item.rip()
                return media_item
            except Exception as e:
                logger.error(f"Error processing item {media_item}: {e}")
                return None

        # Stream tracks from the Last.fm playlist
        if hasattr(pending_playlist, 'stream_tracks'):
            async for track in pending_playlist.stream_tracks():
                # Start processing this track immediately
                task = asyncio.create_task(process_single_item(track))
                download_tasks.add(task)
                # Remove completed tasks to prevent memory buildup
                task.add_done_callback(download_tasks.discard)
        else:
            # Fallback to old method if streaming not available
            playlist = await pending_playlist.resolve()
            if playlist:
                await process_single_item(playlist)

        # Wait for all download tasks to complete
        if download_tasks:
            logger.info(f"Waiting for {len(download_tasks)} downloads to complete...")
            await asyncio.gather(*download_tasks, return_exceptions=True)

        logger.info("All streaming downloads completed")

    async def search_interactive(self, source: str, media_type: str, query: str):
        client = await self.get_logged_in_client(source)

        with console.status(f"[bold]Searching {source}", spinner="dots"):
            pages = await client.search(media_type, query, limit=100)
            if len(pages) == 0:
                console.print(f"[red]No search results found for query {query}")
                return
            search_results = SearchResults.from_pages(source, media_type, pages)

        if platform.system() == "Windows":  # simple term menu not supported for windows
            from pick import pick

            choices = pick(
                search_results.results,
                title=(
                    f"{source.capitalize()} {media_type} search.\n"
                    "Press SPACE to select, RETURN to download, CTRL-C to exit."
                ),
                multiselect=True,
                min_selection_count=1,
            )
            assert isinstance(choices, list)

            await self.add_all_by_id(
                [(source, media_type, item.id) for item, _ in choices],
            )

        else:
            from simple_term_menu import TerminalMenu

            menu = TerminalMenu(
                search_results.summaries(),
                preview_command=search_results.preview,
                preview_size=0.5,
                title=(
                    f"Results for {media_type} '{query}' from {source.capitalize()}\n"
                    "SPACE - select, ENTER - download, ESC - exit"
                ),
                cycle_cursor=True,
                clear_screen=True,
                multi_select=True,
            )
            chosen_ind = menu.show()
            if chosen_ind is None:
                console.print("[yellow]No items chosen. Exiting.")
            else:
                choices = search_results.get_choices(chosen_ind)
                await self.add_all_by_id(
                    [(source, item.media_type(), item.id) for item in choices],
                )

    async def search_take_first(self, source: str, media_type: str, query: str):
        client = await self.get_logged_in_client(source)
        with console.status(f"[bold]Searching {source}", spinner="dots"):
            pages = await client.search(media_type, query, limit=1)

        if len(pages) == 0:
            console.print(f"[red]No search results found for query {query}")
            return

        search_results = SearchResults.from_pages(source, media_type, pages)
        assert len(search_results.results) > 0
        first = search_results.results[0]
        await self.add_by_id(source, first.media_type(), first.id)

    async def search_output_file(
        self, source: str, media_type: str, query: str, filepath: str, limit: int
    ):
        client = await self.get_logged_in_client(source)
        with console.status(f"[bold]Searching {source}", spinner="dots"):
            pages = await client.search(media_type, query, limit=limit)

        if len(pages) == 0:
            console.print(f"[red]No search results found for query {query}")
            return

        search_results = SearchResults.from_pages(source, media_type, pages)
        file_contents = json.dumps(search_results.as_list(source), indent=4)
        async with aiofiles.open(filepath, "w") as f:
            await f.write(file_contents)

        console.print(
            f"Wrote [purple]{len(search_results.results)}[/purple] results to [cyan]{filepath} as JSON!"
        )

    async def resolve_lastfm(self, playlist_url: str):
        """Resolve a last.fm playlist."""
        c = self.config.session.lastfm
        client = await self.get_logged_in_client(c.source)

        if len(c.fallback_source) > 0:
            fallback_client = await self.get_logged_in_client(c.fallback_source)
        else:
            fallback_client = None

        pending_playlist = PendingLastfmPlaylist(
            playlist_url,
            client,
            fallback_client,
            self.config,
            self.database,
        )
        playlist = await pending_playlist.resolve()

        if playlist is not None:
            self.media.append(playlist)

    async def __aenter__(self):
        # Initialize RYM service with async context manager
        if self.rym_service:
            try:
                await self.rym_service.__aenter__()
                logger.debug("RYM service initialized with session persistence")
            except Exception as e:
                logger.warning(f"Failed to initialize RYM session: {e}")
                self.rym_service = None
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # Ensure all client sessions are closed
        for client in self.clients.values():
            if hasattr(client, "session"):
                await client.session.close()

        # Close RYM service if initialized with proper context manager exit
        if self.rym_service:
            try:
                await self.rym_service.__aexit__(exc_type, exc_val, exc_tb)
                logger.debug("RYM service closed with session state saved")
            except Exception as e:
                logger.warning(f"Error closing RYM service: {e}")

        # close global progress bar manager
        clear_progress()
        # We remove artwork tempdirs here because multiple singles
        # may be able to share downloaded artwork in the same `rip` session
        # We don't know that a cover will not be used again until end of execution
        remove_artwork_tempdirs()
