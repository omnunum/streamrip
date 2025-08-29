from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .album import AlbumMetadata
from .util import safe_get, typed

logger = logging.getLogger("streamrip")


@dataclass(slots=True)
class TrackInfo:
    id: str
    quality: int

    bit_depth: Optional[int] = None
    explicit: bool = False
    sampling_rate: Optional[int | float] = None
    work: Optional[str] = None


@dataclass(slots=True)
class TrackMetadata:
    info: TrackInfo

    title: str
    album: AlbumMetadata
    artist: str | list[str]  # Support both single string and multi-value
    tracknumber: int
    discnumber: int
    composer: str | None
    isrc: str | None = None
    lyrics: str | None = ""
    source_platform: str | None = None  # e.g., "deezer", "tidal", "qobuz"
    source_track_id: str | None = None  # Platform-specific track ID
    source_album_id: str | None = None  # Platform-specific album ID
    source_artist_id: str | None = None # Platform-specific artist ID
    # Additional Deezer tags
    bpm: int | None = None
    gain: str | None = None  # ReplayGain format: "+/-X.XX dB"
    # New standard tags
    track_artist_credit: str | None = None  # Different from track artist
    media_type: str | None = None  # "WEB" for streaming sources

    @classmethod
    def from_qobuz(cls, album: AlbumMetadata, resp: dict) -> TrackMetadata | None:
        title = typed(resp["title"].strip(), str)
        isrc = typed(resp["isrc"], str)
        streamable = typed(resp.get("streamable", False), bool)

        if not streamable:
            return None

        version = typed(resp.get("version"), str | None)
        work = typed(resp.get("work"), str | None)
        if version is not None and version not in title:
            title = f"{title} ({version})"
        if work is not None and work not in title:
            title = f"{work}: {title}"

        composer = typed(resp.get("composer", {}).get("name"), str | None)
        tracknumber = typed(resp.get("track_number", 1), int)
        discnumber = typed(resp.get("media_number", 1), int)
        artist = typed(
            safe_get(
                resp,
                "performer",
                "name",
            ),
            str,
        )
        track_id = str(resp["id"])
        bit_depth = typed(resp.get("maximum_bit_depth"), int | None)
        sampling_rate = typed(resp.get("maximum_sampling_rate"), int | float | None)
        # Is the info included?
        explicit = False

        info = TrackInfo(
            id=track_id,
            quality=album.info.quality,
            bit_depth=bit_depth,
            explicit=explicit,
            sampling_rate=sampling_rate,
            work=work,
        )
        return cls(
            info=info,
            title=title,
            album=album,
            artist=artist,
            tracknumber=tracknumber,
            discnumber=discnumber,
            composer=composer,
            isrc=isrc,
        )

    @classmethod
    def from_deezer(cls, album: AlbumMetadata, resp) -> TrackMetadata | None:
        track_id = str(resp["id"])
        # Get first artist ID from contributors list
        artist_id = str(resp["contributors"][0]["id"]) if resp["contributors"] else None
        isrc = typed(resp["isrc"], str)
        
        # Extract track-level metadata  
        bpm = resp.get("bpm")
        if bpm == 0:
            bpm = None
        gain = resp.get("gain")
        bit_depth = 16
        sampling_rate = 44.1
        explicit = typed(resp["explicit_lyrics"], bool)
        work = None
        title = typed(resp["title"], str)
        # Multi-value artist support
        contributors = resp.get("contributors", [])
        if contributors:
            artist = [contributor["name"] for contributor in contributors]
        else:
            # Fallback to single artist if no contributors
            artist = typed(resp.get("artist", {}).get("name", "Unknown Artist"), str)
        
        # Additional metadata
        track_artist_credit = resp.get("artist_credit")
        media_type = "Digital Media"  # MusicBrainz standard for digital/streaming sources
        tracknumber = typed(resp["track_position"], int)
        discnumber = typed(resp["disk_number"], int)
        composer = None
        info = TrackInfo(
            id=track_id,
            quality=album.info.quality,
            bit_depth=bit_depth,
            explicit=explicit,
            sampling_rate=sampling_rate,
            work=work,
        )
        return cls(
            info=info,
            title=title,
            album=album,
            artist=artist,
            tracknumber=tracknumber,
            discnumber=discnumber,
            composer=composer,
            isrc=isrc,
            source_platform=album.source_platform,
            source_track_id=track_id,
            source_album_id=album.source_album_id,
            source_artist_id=artist_id,
            bpm=bpm,
            gain=gain,
            track_artist_credit=track_artist_credit,
            media_type=media_type,
        )

    @classmethod
    def from_soundcloud(cls, album: AlbumMetadata, resp: dict) -> TrackMetadata:
        track = resp
        track_id = track["id"]
        isrc = typed(safe_get(track, "publisher_metadata", "isrc"), str | None)
        bit_depth, sampling_rate = None, None
        explicit = typed(
            safe_get(track, "publisher_metadata", "explicit", default=False),
            bool,
        )

        title = typed(track["title"].strip(), str)
        artist = typed(track["user"]["username"], str)
        tracknumber = 1

        info = TrackInfo(
            id=track_id,
            quality=album.info.quality,
            bit_depth=bit_depth,
            explicit=explicit,
            sampling_rate=sampling_rate,
            work=None,
        )
        return cls(
            info=info,
            title=title,
            album=album,
            artist=artist,
            tracknumber=tracknumber,
            discnumber=0,
            composer=None,
            isrc=isrc,
        )

    @classmethod
    def from_tidal(cls, album: AlbumMetadata, track) -> TrackMetadata:
        title = typed(track["title"], str).strip()
        item_id = str(track["id"])
        isrc = typed(track["isrc"], str)
        version = track.get("version")
        explicit = track.get("explicit", False)
        if version:
            title = f"{title} ({version})"

        tracknumber = typed(track.get("trackNumber", 1), int)
        discnumber = typed(track.get("volumeNumber", 1), int)

        artists = track.get("artists")
        if len(artists) > 0:
            artist = ", ".join(a["name"] for a in artists)
        else:
            artist = track["artist"]["name"]

        lyrics = track.get("lyrics", "")

        quality_map: dict[str, int] = {
            "LOW": 0,
            "HIGH": 1,
            "LOSSLESS": 2,
            "HI_RES": 3,
        }

        tidal_quality = track.get("audioQuality")
        if tidal_quality is not None:
            quality = quality_map[tidal_quality]
        else:
            quality = 0

        if quality >= 2:
            sampling_rate = 44100
            if quality == 3:
                bit_depth = 24
            else:
                bit_depth = 16
        else:
            sampling_rate = bit_depth = None

        info = TrackInfo(
            id=item_id,
            quality=quality,
            bit_depth=bit_depth,
            explicit=explicit,
            sampling_rate=sampling_rate,
            work=None,
        )
        return cls(
            info=info,
            title=title,
            album=album,
            artist=artist,
            tracknumber=tracknumber,
            discnumber=discnumber,
            composer=None,
            isrc=isrc,
            lyrics=lyrics,
        )

    @classmethod
    def from_resp(cls, album: AlbumMetadata, source, resp) -> TrackMetadata | None:
        if source == "qobuz":
            return cls.from_qobuz(album, resp)
        if source == "tidal":
            return cls.from_tidal(album, resp)
        if source == "soundcloud":
            return cls.from_soundcloud(album, resp)
        if source == "deezer":
            return cls.from_deezer(album, resp)
        raise Exception

    def format_track_path(self, format_string: str) -> str:
        # Available keys: "tracknumber", "artist", "albumartist", "composer", "title",
        # and "explicit", "albumcomposer"
        none_text = "Unknown"
        info = {
            "title": self.title,
            "tracknumber": self.tracknumber,
            "artist": self.artist,
            "albumartist": self.album.albumartist,
            "albumcomposer": self.album.albumcomposer or none_text,
            "composer": self.composer or none_text,
            "explicit": " (Explicit) " if self.info.explicit else "",
        }
        return format_string.format(**info)
