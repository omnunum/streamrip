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
            # Get favorites data from Deezer API
            if self.media_type == "tracks":
                resp = await self.client.get_user_favorites(self.user_id, "tracks")
            elif self.media_type == "albums":
                resp = await self.client.get_user_favorites(self.user_id, "albums")
            elif self.media_type == "artists":
                resp = await self.client.get_user_favorites(self.user_id, "artists")
            elif self.media_type == "playlists":
                resp = await self.client.get_user_favorites(self.user_id, "playlists")
            else:
                logger.error(f"Unsupported media type: {self.media_type}")
                return None

        except NonStreamableError as e:
            logger.error(f"User favorites {self.user_id}/{self.media_type} not available: {e}")
            return None
        except Exception as e:
            logger.error(f"Error fetching user favorites: {e}")
            return None

        # Handle different response formats based on client source
        if self.client.source == "deezer":
            if "data" not in resp or not resp["data"]:
                logger.info(f"No {self.media_type} found in user {self.user_id} favorites")
                return None
            items = resp["data"]
        elif self.client.source == "tidal":
            # Tidal API may return items directly or in different structure
            if "items" in resp:
                items = resp["items"]
            elif isinstance(resp, list):
                items = resp
            else:
                logger.info(f"No {self.media_type} found in user {self.user_id} favorites")
                return None
        else:
            # Generic fallback - try common patterns
            if "data" in resp and resp["data"]:
                items = resp["data"]
            elif "items" in resp:
                items = resp["items"]
            elif isinstance(resp, list):
                items = resp
            else:
                logger.info(f"No {self.media_type} found in user {self.user_id} favorites")
                return None
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
            # Handle different ID field names based on source
            if self.client.source == "tidal":
                # Tidal might use 'id', 'uuid', or other field names
                item_id = str(item.get("id") or item.get("uuid") or item.get("item", {}).get("id", "unknown"))
            elif self.client.source == "deezer":
                item_id = str(item["id"])
            else:
                # Generic fallback - try common ID field names
                item_id = str(item.get("id") or item.get("uuid") or item.get("item_id", "unknown"))
            
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