import asyncio
import binascii
import hashlib
import logging

import deezer
import requests
from Cryptodome.Cipher import AES

from ..config import Config
from ..exceptions import (
    AuthenticationError,
    MissingCredentialsError,
    NonStreamableError,
)
from .client import Client
from .downloadable import DeezerDownloadable

logger = logging.getLogger("streamrip")
logging.captureWarnings(True)


class DeezerClient(Client):
    """Client to handle deezer API. Does not do rate limiting.

    Attributes:
        global_config: Entire config object
        client: client from deezer py used for API requests
        logged_in: True if logged in
        config: deezer local config
        session: aiohttp.ClientSession, used only for track downloads not API requests

    """

    source = "deezer"
    max_quality = 2

    def __init__(self, config: Config):
        self.global_config = config
        self.logged_in = False
        self.config = config.session.deezer
        
        # Get config values
        requests_per_minute = getattr(config.session.downloads, 'requests_per_minute', 600)
        max_connections = getattr(config.session.downloads, 'max_connections', 10)
        
        # Create deezer client and configure its requests session with proper connection pool size
        self.client = deezer.Deezer()
        
        # Configure the requests session connection pool size to match max_connections
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=max_connections,
            pool_maxsize=max_connections,
            max_retries=0
        )
        self.client.session.mount('http://', adapter)
        self.client.session.mount('https://', adapter)
        
        # Rate limit: 10 requests/second = 600 requests/minute (Deezer API limit)
        self.rate_limiter = self.get_rate_limiter(requests_per_minute)
        # Concurrency limit using config value
        self.concurrency_limiter = asyncio.Semaphore(max_connections)

    async def login(self):
        # Used for track downloads
        self.session = await self.get_session(
            verify_ssl=self.global_config.session.downloads.verify_ssl
        )
        arl = self.config.arl
        if not arl:
            raise MissingCredentialsError
        success = self.client.login_via_arl(arl)
        if not success:
            raise AuthenticationError
        self.logged_in = True

    async def _api_call(self, func, *args, **kwargs):
        """Wrapper for deezer API calls with rate and concurrency limiting."""
        async with self.concurrency_limiter:
            async with self.rate_limiter:
                # Handle mocks directly without threading for tests
                if hasattr(func, '_mock_name') or str(type(func).__name__) == 'MagicMock':
                    return func(*args, **kwargs)
                return await asyncio.to_thread(func, *args, **kwargs)

    async def get_metadata(self, item_id: str, media_type: str) -> dict:
        # TODO: open asyncio PR to deezer py and integrate
        if media_type == "track":
            return await self.get_track(item_id)
        elif media_type == "album":
            return await self.get_album(item_id)
        elif media_type == "playlist":
            return await self.get_playlist(item_id)
        elif media_type == "artist":
            return await self.get_artist(item_id)
        else:
            raise Exception(f"Media type {media_type} not available on deezer")

    async def get_track(self, item_id: str) -> dict:
        try:
            item = await self._api_call(self.client.api.get_track, item_id)
        except Exception as e:
            raise NonStreamableError(e)

        album_id = item["album"]["id"]
        try:
            album_metadata, album_tracks, detailed_track_info = await asyncio.gather(
                self._api_call(self.client.api.get_album, album_id),
                self._api_call(self.client.api.get_album_tracks, album_id),
                self._api_call(self.client.gw.get_track, item_id),
            )
        except Exception as e:
            logger.error(f"Error fetching album of track {item_id}: {e}")
            return item

        album_metadata["tracks"] = album_tracks["data"]
        album_metadata["track_total"] = len(album_tracks["data"])
        item["album"] = album_metadata
        
        # Add detailed track info with composer/author data if available
        if detailed_track_info and "SNG_CONTRIBUTORS" in detailed_track_info:
            contributors = detailed_track_info["SNG_CONTRIBUTORS"]
            if "composer" in contributors:
                item["composer"] = contributors["composer"]
            if "author" in contributors:
                item["author"] = contributors["author"]

        return item

    async def get_album(self, item_id: str) -> dict:
        album_metadata, album_tracks = await asyncio.gather(
            self._api_call(self.client.api.get_album, item_id),
            self._api_call(self.client.api.get_album_tracks, item_id),
        )
        album_metadata["tracks"] = album_tracks["data"]
        album_metadata["track_total"] = len(album_tracks["data"])
        return album_metadata

    async def get_playlist(self, item_id: str) -> dict:
        pl_metadata, pl_tracks = await asyncio.gather(
            self._api_call(self.client.api.get_playlist, item_id),
            self._api_call(self.client.api.get_playlist_tracks, item_id),
        )
        pl_metadata["tracks"] = pl_tracks["data"]
        pl_metadata["track_total"] = len(pl_tracks["data"])
        return pl_metadata

    async def get_artist(self, item_id: str) -> dict:
        artist, albums = await asyncio.gather(
            self._api_call(self.client.api.get_artist, item_id),
            self._api_call(self.client.api.get_artist_albums, item_id),
        )
        artist["albums"] = albums["data"]
        return artist

    async def get_user_favorites(self, user_id: str, media_type: str) -> dict:
        """Get user favorites from Deezer API."""
        if media_type == "tracks":
            return await self._api_call(self.client.api.get_user_tracks, user_id, limit=-1)
        elif media_type == "albums":
            return await self._api_call(self.client.api.get_user_albums, user_id, limit=-1)
        elif media_type == "artists":
            return await self._api_call(self.client.api.get_user_artists, user_id, limit=-1)
        elif media_type == "playlists":
            return await self._api_call(self.client.api.get_user_playlists, user_id, limit=-1)
        else:
            raise Exception(f"Media type {media_type} not supported for user favorites")

    async def search(self, media_type: str, query: str, limit: int = 200) -> list[dict]:
        # TODO: use limit parameter
        if media_type == "featured":
            try:
                if query:
                    search_function = getattr(self.client.api, f"get_editorial_{query}")
                else:
                    search_function = self.client.api.get_editorial_releases
            except AttributeError:
                raise Exception(f'Invalid editorical selection "{query}"')
        else:
            try:
                search_function = getattr(self.client.api, f"search_{media_type}")
            except AttributeError:
                raise Exception(f"Invalid media type {media_type}")

        response = search_function(query, limit=limit)  # type: ignore
        if response["total"] > 0:
            return [response]
        return []

    async def get_downloadable(
        self,
        item_id: str,
        quality: int = 2,
        is_retry: bool = False,
    ) -> DeezerDownloadable:
        if item_id is None:
            raise NonStreamableError(
                "No item id provided. This can happen when searching for fallback songs.",
            )
        # TODO: optimize such that all of the ids are requested at once
        dl_info: dict = {"quality": quality, "id": item_id}

        track_info = await self._api_call(self.client.gw.get_track, item_id)

        fallback_id = track_info.get("FALLBACK", {}).get("SNG_ID")

        quality_map = [
            (9, "MP3_128"),  # quality 0
            (3, "MP3_320"),  # quality 1
            (1, "FLAC"),  # quality 2
        ]
        size_map = [
            int(track_info.get(f"FILESIZE_{format}", 0)) for _, format in quality_map
        ]
        dl_info["quality_to_size"] = size_map
        
        # Check if requested quality is available
        if size_map[quality] == 0:
            if self.config.lower_quality_if_not_available:
                # Fallback to lower quality
                while size_map[quality] == 0 and quality > 0:
                    artist_name = track_info.get("ART_NAME", "Unknown Artist")
                    album_title = track_info.get("ALB_TITLE", "Unknown Album") 
                    track_title = track_info.get("SNG_TITLE", "Unknown Track")
                    logger.warning(
                        "The requested quality %s is not available for '%s' by %s (Album: %s). Falling back to quality %s",
                        quality,
                        track_title,
                        artist_name,
                        album_title,
                        quality - 1,
                    )
                    quality -= 1
            else:
                # No fallback - raise error
                raise NonStreamableError(
                    f"The requested quality {quality} is not available and fallback is disabled."
                )
        
        # Update the quality in dl_info to reflect the final quality used
        dl_info["quality"] = quality

        _, format_str = quality_map[quality]

        token = track_info["TRACK_TOKEN"]
        try:
            logger.debug("Fetching deezer url with token %s", token)
            url = self.client.get_track_url(token, format_str)
        except deezer.WrongLicense:
            raise NonStreamableError(
                "The requested quality is not available with your subscription. "
                "Deezer HiFi is required for quality 2. Otherwise, the maximum "
                "quality allowed is 1.",
            )
        except deezer.WrongGeolocation:
            if not is_retry and fallback_id:
                return await self.get_downloadable(fallback_id, quality, is_retry=True)
            raise NonStreamableError(
                "The requested track is not available. This may be due to your country/location.",
            )

        if url is None:
            url = self._get_encrypted_file_url(
                item_id,
                track_info["MD5_ORIGIN"],
                track_info["MEDIA_VERSION"],
            )

        dl_info["url"] = url
        logger.debug("dz track info: %s", track_info)
        return DeezerDownloadable(self.session, dl_info)

    def _get_encrypted_file_url(
        self,
        meta_id: str,
        track_hash: str,
        media_version: str,
    ):
        logger.debug("Unable to fetch URL. Trying encryption method.")
        format_number = 1

        url_bytes = b"\xa4".join(
            (
                track_hash.encode(),
                str(format_number).encode(),
                str(meta_id).encode(),
                str(media_version).encode(),
            ),
        )
        url_hash = hashlib.md5(url_bytes).hexdigest()
        info_bytes = bytearray(url_hash.encode())
        info_bytes.extend(b"\xa4")
        info_bytes.extend(url_bytes)
        info_bytes.extend(b"\xa4")
        # Pad the bytes so that len(info_bytes) % 16 == 0
        padding_len = 16 - (len(info_bytes) % 16)
        info_bytes.extend(b"." * padding_len)

        path = binascii.hexlify(
            AES.new(b"jo6aey6haid2Teih", AES.MODE_ECB).encrypt(info_bytes),
        ).decode("utf-8")
        url = f"https://e-cdns-proxy-{track_hash[0]}.dzcdn.net/mobile/1/{path}"
        logger.debug("Encrypted file path %s", url)
        return url
