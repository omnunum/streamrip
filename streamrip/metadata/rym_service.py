import logging
from pathlib import Path
from typing import Optional

from ..config import RymConfig

logger = logging.getLogger("streamrip")

try:
    from rym import RYMMetadataScraper
    RYM_AVAILABLE = True
except ImportError:
    logger.debug("rym not available")
    RYM_AVAILABLE = False


class RymMetadataService:
    def __init__(self, config: RymConfig, app_dir: str):
        self.config = config
        self.app_dir = Path(app_dir)
        self._scraper: Optional[RYMMetadataScraper] = None
        # RYM library cache and session state both go in app_dir with separate subdirs
        self._rym_cache_dir = str(self.app_dir / "rym_cache")
        self._session_state_path = str(self.app_dir / "rym_session_state.json")

    async def __aenter__(self):
        """Async context manager entry."""
        if RYM_AVAILABLE and self.config.enabled:
            await self._initialize_scraper()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self._scraper is not None:
            await self._scraper.__aexit__(exc_type, exc_val, exc_tb)
            self._scraper = None

    async def _initialize_scraper(self):
        """Initialize the RYM scraper with proper session management."""
        if self._scraper is None:
            logger.debug(f"Initializing RYM scraper with session state path: {self._session_state_path}")

            # Use the new flexible config method
            rym_config = self.config.get_rym_config(str(self.app_dir))
            if rym_config is None:
                logger.error("Failed to create RYM config - rym library not available or invalid config")
                return

            # Override cache and session paths with our app_dir-based paths
            rym_config.cache_dir = self._rym_cache_dir
            rym_config.session_state_file_path = self._session_state_path

            self._scraper = await RYMMetadataScraper(rym_config).__aenter__()

    def _get_scraper(self) -> Optional[RYMMetadataScraper]:
        """Get the scraper instance. Should only be called after __aenter__."""
        if not RYM_AVAILABLE or not self.config.enabled:
            return None
        return self._scraper


    async def get_album_metadata(self, artist: str, album: str, year: Optional[int] = None) -> Optional[dict]:
        """Get RYM metadata for an album."""
        if not self.config.enabled:
            return None

        scraper = self._get_scraper()
        if scraper is None:
            return None

        try:
            logger.debug(f"Searching RYM for: artist=\"{artist}\", album=\"{album}\", year={year}")

            # Check if session state file exists for debugging
            if Path(self._session_state_path).exists():
                logger.debug(f"Session state file exists: {self._session_state_path}")
            else:
                logger.debug(f"Session state file does not exist: {self._session_state_path}")

            metadata = await scraper.get_album_metadata(artist, album, year)

            if metadata:
                logger.debug(f"RYM search successful - found match: {metadata.url}")

                # Convert to dict for consistent interface
                metadata_dict = {
                    'genres': metadata.genres or [],
                    'descriptors': metadata.descriptors or [],
                    'rym_url': metadata.url or '',
                    'rating': getattr(metadata, 'rating', None),
                    'rating_count': getattr(metadata, 'rating_count', None)
                }

                # Check if session state was created/updated
                if Path(self._session_state_path).exists():
                    logger.debug(f"Session state file updated after request: {self._session_state_path}")

                return metadata_dict
            else:
                logger.debug(f"RYM search failed - no suitable match found for {artist} - {album} ({year})")
                return None

        except Exception as e:
            logger.debug(f"Error fetching RYM metadata for {artist} - {album}: {e}")
            return None

    def enrich_genres(self, existing_genres: list[str], rym_metadata: dict) -> list[str]:
        """Enrich genres based on RYM data and config."""
        if not rym_metadata or not rym_metadata.get('genres'):
            return existing_genres

        rym_genres = rym_metadata['genres']

        if self.config.genre_mode == "replace":
            return rym_genres
        elif self.config.genre_mode == "append":
            # Combine and deduplicate
            combined = existing_genres + rym_genres
            return list(dict.fromkeys(combined))  # Preserves order while removing duplicates
        else:
            logger.warning(f"Unknown genre_mode: {self.config.genre_mode}, defaulting to replace")
            return rym_genres

    def get_descriptors_string(self, rym_metadata: dict) -> Optional[str]:
        """Get RYM descriptors as a string for tagging."""
        if not rym_metadata or not rym_metadata.get('descriptors'):
            return None

        descriptors = rym_metadata['descriptors']
        return ", ".join(descriptors) if descriptors else None

    async def close(self):
        """Clean up resources."""
        if self._scraper is not None:
            try:
                await self._scraper.__aexit__(None, None, None)
                logger.debug("RYM scraper session closed and state saved")
            except Exception as e:
                logger.debug(f"Error closing RYM scraper: {e}")
            finally:
                self._scraper = None