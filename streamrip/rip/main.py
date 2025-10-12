import asyncio
import json
import logging
import platform

import aiofiles

from .. import db
from ..download_task import DownloadTask
from ..client import Client, DeezerClient, QobuzClient, SoundcloudClient, TidalClient
from ..config import APP_DIR, Config
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
from ..metadata.rym_service import RymMetadataService
from ..progress import clear_progress
from .parse_url import parse_url
from .prompter import get_prompter
from rym import RYMMetadataScraper

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

        # Global download queue and worker management
        self.download_queue: asyncio.Queue[DownloadTask] = asyncio.Queue()
        self.worker_tasks: list[asyncio.Task] = []
        self.max_workers = config.session.downloads.max_connections
        self.shutdown_event = asyncio.Event()

        # Initialize RYM service if enabled (singleton for session)
        self.rym_service = None
        self.rym_scraper = None
        self.rym_semaphore = None
        if config.session.rym.enabled:
            try:
                # Create shared scraper instance (browser not started yet)
                rym_config = config.session.rym.get_rym_config(APP_DIR)
                if rym_config:
                    self.rym_scraper = RYMMetadataScraper(rym_config)
                    # Create semaphore to limit concurrent RYM operations
                    self.rym_semaphore = asyncio.Semaphore(self.max_workers)
                    # Pass shared scraper to service instead of config
                    self.rym_service = RymMetadataService(self.rym_scraper, config.session.rym)
                    logger.info("RYM metadata enrichment enabled")
                else:
                    logger.warning("Failed to create RYM config")
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

    async def start_workers(self):
        """Start download worker tasks."""
        logger.debug(f"Starting {self.max_workers} download workers")
        for i in range(self.max_workers):
            worker = asyncio.create_task(self._download_worker(f"worker-{i}"))
            self.worker_tasks.append(worker)

    async def stop_workers(self):
        """Stop all download worker tasks."""
        logger.debug("Stopping download workers")
        self.shutdown_event.set()

        # Add sentinel values to wake up workers
        for _ in self.worker_tasks:
            await self.download_queue.put(None)

        # Wait for workers to finish
        if self.worker_tasks:
            await asyncio.gather(*self.worker_tasks, return_exceptions=True)
            self.worker_tasks.clear()

        self.shutdown_event.clear()

    async def _download_worker(self, name: str):
        """Download worker coroutine that processes tasks from the queue."""
        logger.debug(f"Download worker {name} started")
        max_retries = 3

        while not self.shutdown_event.is_set():
            try:
                # Get task from queue (with timeout to allow shutdown)
                task = await asyncio.wait_for(self.download_queue.get(), timeout=1.0)

                # Sentinel value to stop worker
                if task is None:
                    break

                logger.debug(f"Worker {name} processing task: {task.track.meta.title if hasattr(task.track, 'meta') else 'unknown'}")

                try:
                    # Process the download task
                    await self._process_download_task(task)
                    logger.debug(f"Worker {name} completed task successfully")

                except Exception as e:
                    logger.error(f"Worker {name} task failed: {e}")

                    # Retry logic
                    if task.retry_count < max_retries:
                        task.retry_count += 1
                        logger.info(f"Retrying task (attempt {task.retry_count}/{max_retries})")
                        await asyncio.sleep(task.retry_count * 2)  # Exponential backoff
                        await self.download_queue.put(task)
                    else:
                        logger.error(f"Task failed after {max_retries} retries: {task.track}")
                        # Add to failed downloads database
                        if hasattr(task.track, 'meta') and hasattr(task.track.meta, 'info'):
                            self.database.set_failed(task.track.meta.info.id)

                finally:
                    self.download_queue.task_done()

            except asyncio.TimeoutError:
                # Timeout is normal during shutdown
                continue
            except Exception as e:
                logger.error(f"Worker {name} error: {e}")

        logger.debug(f"Download worker {name} stopped")

    async def _process_download_task(self, task: DownloadTask):
        """Process a single download task (download + validate + tag)."""
        # Resolve the track to get actual Track object
        track_media = await task.track.resolve()
        if not track_media:
            raise Exception("Failed to resolve track")

        # RYM enrichment if enabled (limited by semaphore)
        if self.rym_service and self.rym_semaphore and hasattr(track_media, 'meta'):
            async with self.rym_semaphore:
                if hasattr(track_media.meta, 'enrich_with_rym'):
                    await track_media.meta.enrich_with_rym(self.rym_service)
                elif hasattr(track_media.meta, 'album') and hasattr(track_media.meta.album, 'enrich_with_rym'):
                    await track_media.meta.album.enrich_with_rym(self.rym_service)

        # Download and process the track
        await track_media.rip()

    async def _queue_album_tracks(self, album):
        """Extract tracks from album and queue them for download."""
        logger.debug(f"Queuing {len(album.tracks)} tracks from album: {album.meta.album}")

        # Run album preprocessing (progress tracking, dry run info, etc.)
        await album.preprocess()

        for track in album.tracks:
            task = DownloadTask(
                track=track,
                album_ref=album,
                retry_count=0,
                task_type="download"
            )
            await self.download_queue.put(task)

        # Note: album.postprocess() will be called after all tracks are downloaded
        # This happens in the worker when the last track of an album completes

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
                # Log into client using credentials from config
                # Note: Not using console.status() to avoid conflicts with concurrent logins
                await client.login()

        assert client.logged_in
        return client



    async def stream_process_urls(self, urls: list[str]):
        """True streaming pipeline: process URLs and download items as they're discovered.

        This eliminates all blocking by streaming items immediately rather than
        batching them. Each album/track starts downloading as soon as it's discovered.
        """
        # Start workers for queue-based processing
        await self.start_workers()

        try:
            # Track all download tasks to ensure they complete
            download_tasks = set()

            async def process_single_item(media_item):
                """Process a single album or track with RYM enrichment"""
                try:
                    # RYM enrichment for albums and tracks (limited by semaphore)
                    if hasattr(media_item, 'meta') and self.rym_service and self.rym_semaphore:
                        async with self.rym_semaphore:
                            # For albums, enrich the album metadata directly
                            if hasattr(media_item.meta, 'enrich_with_rym'):
                                await media_item.meta.enrich_with_rym(self.rym_service)
                            # For tracks, enrich the album metadata within the track
                            elif hasattr(media_item.meta, 'album') and hasattr(media_item.meta.album, 'enrich_with_rym'):
                                await media_item.meta.album.enrich_with_rym(self.rym_service)

                    # Handle downloads based on media type
                    if hasattr(media_item, 'tracks'):
                        # For Albums: extract tracks and queue them individually
                        await self._queue_album_tracks(media_item)
                    else:
                        # For individual Tracks: download directly
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
                            # Create task for single item processing to ensure it uses the queue system
                            task = asyncio.create_task(process_single_item(media))
                            download_tasks.add(task)
                            task.add_done_callback(download_tasks.discard)

                except Exception as e:
                    logger.error(
                        f"Error streaming from URL {url}: {e}",
                        exc_info=logger.level == logging.DEBUG
                    )

            # Process all URLs concurrently in streaming fashion
            # Create tasks for each URL to process concurrently
            url_tasks = [asyncio.create_task(stream_from_url(url)) for url in urls]

            # Wait for all URL discovery to complete
            await asyncio.gather(*url_tasks, return_exceptions=True)

            # Now wait for all download tasks to complete
            if download_tasks:
                logger.info(f"Waiting for {len(download_tasks)} downloads to complete...")
                await asyncio.gather(*download_tasks, return_exceptions=True)

            # Wait for download queue to be fully processed
            await self.download_queue.join()
            logger.info("All streaming downloads completed")

        finally:
            # Stop workers when done
            await self.stop_workers()

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

        # Start workers for queue-based processing
        await self.start_workers()

        try:
            download_tasks = set()

            async def process_single_item(media_item):
                """Process a single album or track with RYM enrichment"""
                try:
                    # RYM enrichment for albums and tracks (limited by semaphore)
                    if hasattr(media_item, 'meta') and self.rym_service and self.rym_semaphore:
                        async with self.rym_semaphore:
                            # For albums, enrich the album metadata directly
                            if hasattr(media_item.meta, 'enrich_with_rym'):
                                await media_item.meta.enrich_with_rym(self.rym_service)
                            # For tracks, enrich the album metadata within the track
                            elif hasattr(media_item.meta, 'album') and hasattr(media_item.meta.album, 'enrich_with_rym'):
                                await media_item.meta.album.enrich_with_rym(self.rym_service)

                    # Handle downloads based on media type
                    if hasattr(media_item, 'tracks'):
                        # For Albums: extract tracks and queue them individually
                        await self._queue_album_tracks(media_item)
                    else:
                        # For individual Tracks: download directly
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

            # Wait for download queue to be fully processed
            await self.download_queue.join()

            # Clear pending items
            self.pending.clear()
            logger.info("All streaming downloads completed")

        finally:
            # Stop workers when done
            await self.stop_workers()

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
                # RYM enrichment for tracks (limited by semaphore)
                if hasattr(media_item, 'meta') and hasattr(media_item.meta, 'enrich_with_rym'):
                    if self.rym_service and self.rym_semaphore:
                        async with self.rym_semaphore:
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

        # Wait for download queue to be fully processed
        await self.download_queue.join()
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
        # Start RYM scraper browser session if enabled
        if self.rym_scraper:
            await self.rym_scraper.__aenter__()
            logger.debug("RYM scraper browser session started")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # Ensure all client sessions are closed
        for client in self.clients.values():
            if hasattr(client, "session"):
                await client.session.close()

        # Cleanup RYM scraper browser session
        if self.rym_scraper:
            await self.rym_scraper.__aexit__(exc_type, exc_val, exc_tb)
            logger.debug("RYM scraper browser session cleaned up")

        # close global progress bar manager
        clear_progress()
        # We remove artwork tempdirs here because multiple singles
        # may be able to share downloaded artwork in the same `rip` session
        # We don't know that a cover will not be used again until end of execution
        remove_artwork_tempdirs()
