from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from ..filepath_utils import clean_filename, clean_filepath
from .covers import Covers
from .util import get_quality_id, safe_get, typed

PHON_COPYRIGHT = "\u2117"
COPYRIGHT = "\u00a9"

logger = logging.getLogger("streamrip")


genre_clean = re.compile(r"([^\u2192\/]+)")


@dataclass(slots=True)
class AlbumInfo:
    id: str
    quality: int
    container: str
    label: Optional[str] = None
    explicit: bool = False
    sampling_rate: int | float | None = None
    bit_depth: int | None = None
    booklets: list[dict] | None = None
    streamable: bool = True  # Whether the album is available for streaming



@dataclass(slots=True)
class AlbumMetadata:
    info: AlbumInfo
    album: str
    albumartist: str
    year: str
    genre: list[str]
    covers: Covers
    tracktotal: int
    disctotal: int = 1
    albumcomposer: str | None = None
    comment: str | None = None
    compilation: str | None = None
    copyright: str | None = None
    date: str | None = None
    description: str | None = None
    encoder: str | None = None
    grouping: str | None = None
    lyrics: str | None = None
    purchase_date: str | None = None
    source_platform: str | None = None
    source_album_id: str | None = None
    source_artist_id: str | None = None
    # Additional Deezer tags
    bpm: int | None = None
    barcode: str | None = None  # UPC/Barcode
    replaygain_album_gain: str | None = None  # ReplayGain format: "+/-X.XX dB"
    releasetype: str | None = None  # Vorbis standard name
    # New standard tags
    album_artist_credit: str | None = None  # Different from album artist
    originaldate: str | None = None  # Vorbis standard name
    media_type: str | None = None  # "WEB" for streaming sources

    def get_genres(self) -> str:
        return ", ".join(self.genre)

    def get_copyright(self) -> str | None:
        if self.copyright is None:
            return None
        # Add special chars
        _copyright = re.sub(r"(?i)\(P\)", PHON_COPYRIGHT, self.copyright)
        _copyright = re.sub(r"(?i)\(C\)", COPYRIGHT, _copyright)
        return _copyright

    def format_folder_path(self, formatter: str) -> str:
        # Available keys: "albumartist", "title", "year", "bit_depth", "sampling_rate",
        # "id", "albumcomposer", "releasetype"

        none_str = "Unknown"
        # Format releasetype with title case, except keep EP uppercase
        releasetype_formatted = none_str
        if self.releasetype:
            rt = clean_filename(self.releasetype)
            if rt.upper() == "EP":
                releasetype_formatted = "EP"
            else:
                releasetype_formatted = rt.title()
        
        info: dict[str, str | int | float] = {
            "albumartist": clean_filename(self.albumartist),
            "albumcomposer": clean_filename(self.albumcomposer or "") or none_str,
            "bit_depth": self.info.bit_depth or none_str,
            "id": self.info.id,
            "sampling_rate": self.info.sampling_rate or none_str,
            "title": clean_filename(self.album),
            "year": self.year,
            "container": self.info.container,
            "releasetype": releasetype_formatted,
        }

        return clean_filepath(formatter.format(**info))

    @classmethod
    def from_qobuz(cls, resp: dict) -> AlbumMetadata:
        album = resp.get("title", "Unknown Album")
        tracktotal = resp.get("tracks_count", 1)
        genre = resp.get("genres_list") or resp.get("genre") or []
        genres = list(set(genre_clean.findall("/".join(genre))))
        date = resp.get("release_date_original") or resp.get("release_date")
        year = date[:4] if date is not None else "Unknown"

        _copyright = resp.get("copyright", "")

        if artists := resp.get("artists"):
            # Get first artist as primary albumartist (MusicBrainz standard)
            albumartist = artists[0]["name"]
        else:
            albumartist = typed(safe_get(resp, "artist", "name"), str)

        albumcomposer = typed(safe_get(resp, "composer", "name", default=""), str)
        _label = resp.get("label")
        if isinstance(_label, dict):
            _label = _label["name"]
        label = typed(_label or "", str)
        description = typed(resp.get("description", ""), str)
        disctotal = typed(
            max(
                track.get("media_number", 1)
                for track in safe_get(resp, "tracks", default=[{}])  # type: ignore
            )
            or 1,
            int,
        )
        explicit = typed(resp.get("parental_warning", False), bool)

        # Non-embedded information
        cover_urls = Covers.from_qobuz(resp)

        bit_depth = typed(resp.get("maximum_bit_depth", -1), int)
        sampling_rate = typed(resp.get("maximum_sampling_rate", -1.0), int | float)
        quality = get_quality_id(bit_depth, sampling_rate)
        # Make sure it is non-empty list
        booklets = typed(resp.get("goodies", None) or None, list | None)
        item_id = str(resp.get("qobuz_id"))
        
        # Extract additional Qobuz metadata
        barcode = resp.get("upc")
        # Normalize release type casing: keep EP uppercase, others title case
        raw_type = resp.get("release_type")
        type_mapper = {
            "epmini": "EP",
            "bestof": "Best Of",
        }
        releasetype = type_mapper.get(raw_type.lower(), str(raw_type).title())
        originaldate = resp.get("release_date_original")
        media_type = "Digital Media"  # MusicBrainz standard for digital/streaming sources
        
        # Source platform identification
        source_platform = "qobuz"
        source_album_id = item_id
        # Get first artist ID for source_artist_id
        source_artist_id = None
        if artists:
            source_artist_id = str(artists[0]["id"])
        elif resp.get("artist", {}).get("id"):
            source_artist_id = str(resp["artist"]["id"])

        if sampling_rate and bit_depth:
            container = "FLAC"
        else:
            container = "MP3"

        info = AlbumInfo(
            id=item_id,
            quality=quality,
            container=container,
            label=label,
            explicit=explicit,
            sampling_rate=sampling_rate,
            bit_depth=bit_depth,
            booklets=booklets,
            streamable=True,
        )
        return AlbumMetadata(
            info,
            album,
            albumartist,
            year,
            genre=genres,
            covers=cover_urls,
            albumcomposer=albumcomposer,
            comment=None,
            compilation=None,
            copyright=_copyright,
            date=date,
            description=description,
            disctotal=disctotal,
            encoder=None,
            grouping=None,
            lyrics=None,
            purchase_date=None,
            tracktotal=tracktotal,
            source_platform=source_platform,
            source_album_id=source_album_id,
            source_artist_id=source_artist_id,
            barcode=barcode,
            releasetype=releasetype,
            originaldate=originaldate,
            media_type=media_type,
        )

    @classmethod
    def from_deezer(cls, resp: dict) -> AlbumMetadata:
        album = resp.get("title", "Unknown Album")
        tracktotal = typed(resp.get("track_total", 0) or resp.get("nb_tracks", 0), int)
        disctotal = typed(resp["tracks"][-1]["disk_number"], int)
        genres = [typed(g["name"], str) for g in resp["genres"]["data"]]

        date = typed(resp["release_date"], str)
        year = date[:4]
        _copyright = None
        description = None
        albumartist = typed(safe_get(resp, "artist", "name"), str)
        artist_id = str(safe_get(resp, "artist", "id")) if safe_get(resp, "artist", "id") else None
        albumcomposer = None
        label = resp.get("label")
        booklets = None
        
        # Extract additional Deezer metadata - keep it simple
        bpm = None  # Album-level BPM typically not used, let tracks handle it
        barcode = resp.get("upc")  # Use standardized name
        replaygain_album_gain = resp.get("gain")
        releasetype = resp.get("record_type")  # Use Vorbis standard name
        
        # Additional standard metadata
        album_artist_credit = resp.get("album_artist_credit")
        originaldate = resp.get("original_release_date")  # Use Vorbis standard name
        media_type = "Digital Media"  # MusicBrainz standard for digital/streaming sources
        explicit = typed(
            resp.get("parental_warning", False) or resp.get("explicit_lyrics", False),
            bool,
        )

        # not embedded
        quality = 2
        bit_depth = 16
        sampling_rate = 44100
        container = "FLAC"

        cover_urls = Covers.from_deezer(resp)
        item_id = str(resp["id"])

        info = AlbumInfo(
            id=item_id,
            quality=quality,
            container=container,
            label=label,
            explicit=explicit,
            sampling_rate=sampling_rate,
            bit_depth=bit_depth,
            booklets=booklets,
            streamable=True,
        )
        return AlbumMetadata(
            info,
            album,
            albumartist,
            year,
            genre=genres,
            covers=cover_urls,
            albumcomposer=albumcomposer,
            comment=None,
            compilation=None,
            copyright=_copyright,
            date=date,
            description=description,
            disctotal=disctotal,
            encoder=None,
            grouping=None,
            lyrics=None,
            purchase_date=None,
            tracktotal=tracktotal,
            source_platform="deezer",
            source_album_id=item_id,
            source_artist_id=artist_id,
            bpm=bpm,
            barcode=barcode,
            replaygain_album_gain=replaygain_album_gain,
            releasetype=releasetype,
            album_artist_credit=album_artist_credit,
            originaldate=originaldate,
            media_type=media_type,
        )

    @classmethod
    def from_soundcloud(cls, resp) -> AlbumMetadata:
        track = resp
        track_id = track["id"]
        bit_depth, sampling_rate = None, None
        explicit = typed(
            safe_get(track, "publisher_metadata", "explicit", default=False),
            bool,
        )
        genre = typed(track.get("genre"), str | None)
        genres = [genre] if genre is not None else []
        artist = typed(safe_get(track, "publisher_metadata", "artist"), str | None)
        artist = artist or typed(track["user"]["username"], str)
        albumartist = artist
        date = typed(track.get("created_at"), str)
        year = date[:4]
        label = typed(track.get("label_name"), str | None)
        description = typed(track.get("description"), str | None)
        album_title = typed(
            safe_get(track, "publisher_metadata", "album_title"),
            str | None,
        )
        album_title = album_title or "Unknown album"
        copyright = typed(safe_get(track, "publisher_metadata", "p_line"), str | None)
        tracktotal = 1
        disctotal = 1
        quality = 0
        covers = Covers.from_soundcloud(resp)

        info = AlbumInfo(
            # There are no albums in soundcloud, so we just identify them by a track ID
            id=track_id,
            quality=quality,
            container="MP3",
            label=label,
            explicit=explicit,
            sampling_rate=sampling_rate,
            bit_depth=bit_depth,
            booklets=None,
            streamable=True,
        )
        return AlbumMetadata(
            info,
            album_title,
            albumartist,
            year,
            genre=genres,
            covers=covers,
            albumcomposer=None,
            comment=None,
            compilation=None,
            copyright=copyright,
            date=date,
            description=description,
            disctotal=disctotal,
            encoder=None,
            grouping=None,
            lyrics=None,
            purchase_date=None,
            tracktotal=tracktotal,
        )

    @classmethod
    def from_tidal(cls, resp) -> AlbumMetadata:
        """
        Args:
        ----
            resp: API response containing album metadata.
        Returns: AlbumMetadata instance with streamable attribute set.
        """
        streamable = resp.get("allowStreaming", True)

        item_id = str(resp["id"])
        album = typed(resp.get("title", "Unknown Album"), str)
        tracktotal = typed(resp.get("numberOfTracks", 1), int)
        # genre not returned by API
        date = typed(resp.get("releaseDate"), str)
        year = date[:4]
        _copyright = typed(resp.get("copyright", ""), str)

        artists = typed(resp.get("artists", []), list)
        artist_id = None
        if artists:
            # Get first artist as primary albumartist (MusicBrainz standard)
            albumartist = artists[0]["name"]
            # Get first artist ID for source_artist_id
            artist_id = str(artists[0]["id"])
        else:
            albumartist = typed(safe_get(resp, "artist", "name", default=""), str)
            if "artist" in resp and "id" in resp["artist"]:
                artist_id = str(resp["artist"]["id"])

        disctotal = typed(resp.get("numberOfVolumes", 1), int)
        
        # Extract label from copyright field since Tidal doesn't provide direct label field
        label = None
        if _copyright:
            # Parse copyright to extract label: "(C) 2000 Label Name" -> "Label Name"
            import re
            copyright_match = re.match(r'^\([CP]\)\s*\d{4}\s*(.+)$', _copyright, re.IGNORECASE)
            if copyright_match:
                label = copyright_match.group(1).strip()
            else:
                # Fallback: if no year pattern, just remove (C) or (P) prefix
                label_match = re.match(r'^\([CP]\)\s*(.+)$', _copyright, re.IGNORECASE)
                if label_match:
                    label = label_match.group(1).strip()
        
        # Extract additional Tidal metadata
        barcode = resp.get("upc")  # UPC/Barcode
        # Normalize release type casing: keep EP uppercase, others title case
        raw_type = resp.get("type")
        if raw_type:
            if raw_type.upper() == "EP":
                releasetype = "EP"
            else:
                releasetype = raw_type.title()  # Album, Single, etc.
        else:
            releasetype = None
        media_type = "Digital Media"  # MusicBrainz standard for digital/streaming sources

        # non-embedded
        explicit = typed(resp.get("explicit", False), bool)
        covers = Covers.from_tidal(resp)
        if covers is None:
            covers = Covers()

        quality_map: dict[str, int] = {
            "LOW": 0,
            "HIGH": 1,
            "LOSSLESS": 2,
            "HI_RES": 3,
        }

        tidal_quality = resp.get("audioQuality", "LOW")
        quality = quality_map[tidal_quality]
        if quality >= 2:
            sampling_rate = 44100
            if quality == 3:
                bit_depth = 24
                container = "FLAC"
            else:
                bit_depth = 16
                container = "FLAC"
        else:
            sampling_rate = None
            bit_depth = None
            container = "MP4"  # AAC for lower qualities

        info = AlbumInfo(
            id=item_id,
            quality=quality,
            container=container,
            label=label,
            explicit=explicit,
            sampling_rate=sampling_rate,
            bit_depth=bit_depth,
            booklets=None,
            streamable=streamable,
        )
        return AlbumMetadata(
            info,
            album,
            albumartist,
            year,
            genre=[],
            covers=covers,
            albumcomposer=None,
            comment=None,
            compilation=None,
            copyright=_copyright,
            date=date,
            description=None,
            disctotal=disctotal,
            encoder=None,
            grouping=None,
            lyrics=None,
            purchase_date=None,
            tracktotal=tracktotal,
            source_platform="tidal",
            source_album_id=item_id,
            source_artist_id=artist_id,
            barcode=barcode,
            releasetype=releasetype,
            media_type=media_type,
        )

    @classmethod
    def from_tidal_playlist_track_resp(cls, resp: dict) -> AlbumMetadata:
        album_resp = resp["album"]
        streamable = resp.get("allowStreaming", True)

        item_id = str(resp["id"])
        album = typed(album_resp.get("title", "Unknown Album"), str)
        tracktotal = 1
        # genre not returned by API
        date = typed(resp.get("streamStartDate"), str | None)
        if date is not None:
            year = date[:4]
        else:
            year = "Unknown Year"

        _copyright = typed(resp.get("copyright", ""), str)
        artists = typed(resp.get("artists", []), list)
        artist_id = None
        if artists:
            # Get first artist as primary albumartist (MusicBrainz standard)
            albumartist = artists[0]["name"]
            # Get first artist ID for source_artist_id
            artist_id = str(artists[0]["id"])
        else:
            albumartist = typed(
                safe_get(resp, "artist", "name", default="Unknown Albumbartist"), str
            )
            if "artist" in resp and "id" in resp["artist"]:
                artist_id = str(resp["artist"]["id"])

        disctotal = typed(resp.get("volumeNumber", 1), int)
        
        # Extract label from copyright field since Tidal doesn't provide direct label field
        label = None
        if _copyright:
            # Parse copyright to extract label: "(C) 2000 Label Name" -> "Label Name"
            import re
            copyright_match = re.match(r'^\([CP]\)\s*\d{4}\s*(.+)$', _copyright, re.IGNORECASE)
            if copyright_match:
                label = copyright_match.group(1).strip()
            else:
                # Fallback: if no year pattern, just remove (C) or (P) prefix
                label_match = re.match(r'^\([CP]\)\s*(.+)$', _copyright, re.IGNORECASE)
                if label_match:
                    label = label_match.group(1).strip()
        
        # Extract additional Tidal metadata
        # Normalize release type casing: keep EP uppercase, others title case
        raw_type = resp.get("type")
        if raw_type:
            if raw_type.upper() == "EP":
                releasetype = "EP"
            else:
                releasetype = raw_type.title()  # Album, Single, etc.
        else:
            releasetype = None
        media_type = "Digital Media"  # MusicBrainz standard for digital/streaming sources

        # non-embedded
        explicit = typed(resp.get("explicit", False), bool)
        covers = Covers.from_tidal(album_resp)
        if covers is None:
            covers = Covers()

        quality_map: dict[str, int] = {
            "LOW": 0,
            "HIGH": 1,
            "LOSSLESS": 2,
            "HI_RES": 3,
        }

        tidal_quality = resp.get("audioQuality", "LOW")
        quality = quality_map[tidal_quality]
        if quality >= 2:
            sampling_rate = 44100
            if quality == 3:
                bit_depth = 24
                container = "FLAC"
            else:
                bit_depth = 16
                container = "FLAC"
        else:
            sampling_rate = None
            bit_depth = None
            container = "MP4"  # AAC for lower qualities

        info = AlbumInfo(
            id=item_id,
            quality=quality,
            container=container,
            label=label,
            explicit=explicit,
            sampling_rate=sampling_rate,
            bit_depth=bit_depth,
            booklets=None,
            streamable=streamable,
        )
        return AlbumMetadata(
            info,
            album,
            albumartist,
            year,
            genre=[],
            covers=covers,
            albumcomposer=None,
            comment=None,
            compilation=None,
            copyright=_copyright,
            date=date,
            description=None,
            disctotal=disctotal,
            encoder=None,
            grouping=None,
            lyrics=None,
            purchase_date=None,
            tracktotal=tracktotal,
            source_platform="tidal",
            source_album_id=str(album_resp["id"]),
            source_artist_id=artist_id,
            releasetype=releasetype,
            media_type=media_type,
        )

    @classmethod
    def from_incomplete_deezer_track_resp(cls, resp: dict) -> AlbumMetadata:
        album_resp = resp["album"]
        album_id = album_resp["id"]
        album = album_resp["title"]
        covers = Covers.from_deezer(album_resp)
        date = album_resp["release_date"]
        year = date[:4]
        albumartist = ", ".join(a["name"] for a in resp["contributors"])
        explicit = resp.get("explicit_lyrics", False)

        info = AlbumInfo(
            id=album_id,
            quality=2,
            container="MP4",
            label=None,
            explicit=explicit,
            sampling_rate=None,
            bit_depth=None,
            booklets=None,
            streamable=True,
        )
        return AlbumMetadata(
            info,
            album,
            albumartist,
            year,
            genre=[],
            covers=covers,
            albumcomposer=None,
            comment=None,
            compilation=None,
            copyright=None,
            date=date,
            description=None,
            disctotal=1,
            encoder=None,
            grouping=None,
            lyrics=None,
            purchase_date=None,
            tracktotal=1,
        )

    @classmethod
    def from_track_resp(cls, resp: dict, source: str) -> AlbumMetadata:
        if source == "qobuz":
            return cls.from_qobuz(resp["album"])
        if source == "tidal":
            return cls.from_tidal_playlist_track_resp(resp)
        if source == "soundcloud":
            return cls.from_soundcloud(resp)
        if source == "deezer":
            if "tracks" not in resp["album"]:
                return cls.from_incomplete_deezer_track_resp(resp)
            return cls.from_deezer(resp["album"])
        raise Exception("Invalid source")

    @classmethod
    def from_album_resp(cls, resp: dict, source: str) -> AlbumMetadata:
        if source == "qobuz":
            return cls.from_qobuz(resp)
        if source == "tidal":
            return cls.from_tidal(resp)
        if source == "soundcloud":
            return cls.from_soundcloud(resp)
        if source == "deezer":
            return cls.from_deezer(resp)
        raise Exception("Invalid source")
