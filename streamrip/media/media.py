from abc import ABC, abstractmethod
import logging

logger = logging.getLogger("streamrip")


class Media(ABC):
    @property
    def source_name(self) -> str:
        """Get the source platform name."""
        if hasattr(self, 'client') and hasattr(self.client, 'source'):
            return self.client.source
        return 'unknown'

    async def rip(self):
        await self.preprocess()
        await self.download()
        await self.postprocess()

    @abstractmethod
    async def preprocess(self):
        """Create directories, download cover art, etc."""
        raise NotImplementedError

    @abstractmethod
    async def download(self):
        """Download and tag the actual audio files in the correct directories."""
        raise NotImplementedError

    @abstractmethod
    async def postprocess(self):
        """Update database, run conversion, delete garbage files etc."""
        raise NotImplementedError


class Pending(ABC):
    """A request to download a `Media` whose metadata has not been fetched."""

    @abstractmethod
    async def resolve(self) -> Media | None:
        """Fetch metadata and resolve into a downloadable `Media` object."""
        raise NotImplementedError

    @staticmethod
    def filter_and_log_albums(album_ids: list[str], db, source: str, entity_name: str, entity_id: str) -> bool:
        """Check if all albums are downloaded and log appropriate message.
        
        Returns:
            True if should skip (all albums downloaded), False otherwise
        """
        if not album_ids:
            return False
            
        new_albums = [
            album_id for album_id in album_ids
            if not db.release_downloaded(album_id, "album", source)
        ]
        
        if len(new_albums) == 0:
            logger.info(f"{entity_name} ({entity_id}) - all {len(album_ids)} albums already downloaded")
            return True
        elif len(new_albums) < len(album_ids):
            logger.info(f"{entity_name} ({entity_id}) - found {len(new_albums)} new albums to download")
        
        return False


class CollectionMedia(Media):
    """Base class for media types that contain multiple albums (Artist, Label)."""
    
    def _mark_collection_complete(self, entity_id: str, entity_type: str):
        """Mark a collection (artist/label) as complete."""
        if entity_id and self.db and hasattr(self, 'albums') and len(self.albums) > 0:
            self.db.set_release_downloaded(entity_id, entity_type, self.source_name, len(self.albums))
            logger.info(f"{entity_type.title()} {entity_id} processed ({len(self.albums)} albums) - marked as complete")
