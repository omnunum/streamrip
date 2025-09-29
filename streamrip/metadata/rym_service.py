import logging
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
    """Minimal service for RYM integration with config and policy management."""

    def __init__(self, config: RymConfig, app_dir: str):
        self.config = config
        self.app_dir = app_dir

    async def get_release_metadata(self, artist: str, album: str, year: Optional[int] = None, album_type: str = "album"):
        """Get RYM metadata with artist fallback.

        Args:
            artist: Artist name
            album: Album/release name
            year: Release year (optional)
            album_type: "album", "single", "ep", "compilation" (from streaming metadata)

        Returns:
            RYM metadata object (AlbumMetadata or ArtistMetadata) or None
        """
        # Feature switching
        if not self.config.enabled or not RYM_AVAILABLE:
            return None

        # Config conversion
        rym_config = self.config.get_rym_config(self.app_dir)
        if not rym_config:
            logger.debug("Failed to create RYM config")
            return None

        try:
            # Use RYM library's context manager for automatic session management
            async with RYMMetadataScraper(rym_config) as scraper:
                logger.debug(f"RYM search: {artist} - {album} ({year}) [type: {album_type}]")

                # Step 1: Try album search with optimized flow built-in
                # (cache → direct URL → artist ID cache → discography → artist search)
                metadata = await scraper.get_album_metadata(artist, album, year, album_type)

                if metadata:
                    logger.debug(f"RYM album search successful: {metadata.url}")
                    return metadata

                # Step 2: Artist fallback
                logger.debug(f"Album search failed, trying artist fallback: {artist}")

                artist_metadata = await scraper.get_artist_metadata(artist)

                if artist_metadata:
                    logger.debug(f"RYM artist fallback successful: {artist_metadata.url}")
                    return artist_metadata

                logger.debug(f"Both album and artist search failed for: {artist}")
                return None

        except Exception as e:
            logger.debug(f"Error in RYM search for {artist} - {album}: {e}")
            return None

    def enrich_genres(self, existing_genres: list[str], rym_metadata) -> list[str]:
        """Apply genre enrichment policy (replace vs append).

        Args:
            existing_genres: Current genres from streaming service
            rym_metadata: RYM metadata object (AlbumMetadata or ArtistMetadata)

        Returns:
            Enriched genre list based on config policy
        """
        logger.debug(f"Genre enrichment - existing: {existing_genres}")

        if not rym_metadata or not rym_metadata.genres:
            logger.debug("No RYM metadata or genres found, keeping existing genres")
            return existing_genres

        rym_genres = rym_metadata.genres
        logger.debug(f"RYM genres: {rym_genres}")
        logger.debug(f"Genre mode: {self.config.genre_mode}")

        if self.config.genre_mode == "replace":
            logger.debug(f"Replacing genres: {existing_genres} -> {rym_genres}")
            return rym_genres
        elif self.config.genre_mode == "append":
            # Combine and deduplicate while preserving order
            combined = existing_genres + rym_genres
            result = list(dict.fromkeys(combined))
            logger.debug(f"Appending genres: {existing_genres} + {rym_genres} -> {result}")
            return result
        else:
            logger.warning(f"Unknown genre_mode: {self.config.genre_mode}, defaulting to replace")
            return rym_genres

    def get_descriptors_string(self, rym_metadata) -> Optional[str]:
        """Get RYM descriptors as a string for tagging.

        Args:
            rym_metadata: RYM metadata object (AlbumMetadata or ArtistMetadata)

        Returns:
            Comma-separated descriptors string or None
        """
        if not rym_metadata or not rym_metadata.descriptors:
            return None

        return ", ".join(rym_metadata.descriptors) if rym_metadata.descriptors else None