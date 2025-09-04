import asyncio
import logging
from dataclasses import dataclass

from ..client import Client
from ..config import Config
from ..db import Database
from ..exceptions import NonStreamableError
from .album import PendingAlbum
from .artist import PendingArtist
from .media import Media, Pending
from .playlist import PendingPlaylist
from .track import PendingSingle

logger = logging.getLogger("streamrip")


@dataclass(slots=True)
class PendingUserFavorites(Pending):
    user_id: str
    media_type: str  # "tracks", "albums", "artists", "playlists"
    client: Client
    config: Config
    db: Database

    async def resolve(self) -> Media | None:
        """Resolve user favorites to a collection of media items."""
        try:
            # Get favorites data from client API - all clients now have standardized interface
            resp = await self.client.get_user_favorites(self.media_type, user_id=self.user_id)

        except NonStreamableError as e:
            logger.error(f"User favorites {self.user_id}/{self.media_type} not available: {e}")
            return None
        except Exception as e:
            logger.error(f"Error fetching user favorites: {e}")
            return None

        # All clients now return standardized format: {"items": [...]}
        if "items" not in resp or not resp["items"]:
            logger.info(f"No {self.media_type} found in user {self.user_id} favorites")
            return None
        items = resp["items"]
        logger.info(f"Found {len(items)} favorited {self.media_type} for user {self.user_id}")

        # Create a UserFavorites collection
        return UserFavorites(
            user_id=self.user_id,
            media_type=self.media_type,
            items=items,
            client=self.client,
            config=self.config,
            db=self.db,
        )


@dataclass(slots=True)
class UserFavorites(Media):
    """Collection of user favorited items that downloads each item individually."""
    user_id: str
    media_type: str
    items: list[dict]
    client: Client
    config: Config
    db: Database

    async def preprocess(self):
        """No special preprocessing needed for user favorites."""
        logger.info(f"Starting download of {len(self.items)} favorited {self.media_type} for user {self.user_id}")

    async def download(self):
        """Download all items in the user's favorites."""
        # Create Pending objects for each item
        pending_items = []
        for item in self.items:
            # All clients now return standardized items with "id" field
            item_id = str(item.get("id", "unknown"))
            
            if item_id == "unknown":
                logger.warning(f"Could not extract ID from item: {item}")
                continue
            
            if self.media_type == "tracks":
                pending_items.append(PendingSingle(item_id, self.client, self.config, self.db))
            elif self.media_type == "albums":
                pending_items.append(PendingAlbum(item_id, self.client, self.config, self.db))
            elif self.media_type == "artists":
                pending_items.append(PendingArtist(item_id, self.client, self.config, self.db))
            elif self.media_type == "playlists":
                pending_items.append(PendingPlaylist(item_id, self.client, self.config, self.db))

        # Process items in batches to avoid overwhelming the system
        batch_size = 5
        for i in range(0, len(pending_items), batch_size):
            batch = pending_items[i:i + batch_size]
            results = await asyncio.gather(*[item.resolve() for item in batch], return_exceptions=True)
            
            # Download resolved items
            for result in results:
                if isinstance(result, Exception):
                    logger.error(f"Error resolving item: {result}")
                    continue
                if result is not None:
                    try:
                        await result.rip()
                    except Exception as e:
                        logger.error(f"Error downloading item: {e}")

    async def postprocess(self):
        """No special postprocessing needed for user favorites."""
        logger.info(f"Completed download of favorited {self.media_type} for user {self.user_id}")