import logging
import os
from enum import Enum

import aiofiles
from mutagen import id3
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    APIC,  # type: ignore
    ID3,
    ID3NoHeaderError,
)
from mutagen.mp4 import MP4, MP4Cover

from .track import TrackMetadata

logger = logging.getLogger("streamrip")

FLAC_MAX_BLOCKSIZE = 16777215  # 16.7 MB

MP4_KEYS = (
    "\xa9nam",
    "\xa9ART",
    "----:com.apple.iTunes:ARTISTS",
    "\xa9alb",
    r"aART",
    "\xa9wrt",  # composer
    "----:com.apple.iTunes:AUTHOR",  # author/songwriter
    "\xa9day",
    "\xa9cmt",
    "desc",
    "purd",
    "\xa9grp",
    "\xa9gen",
    "\xa9lyr",
    "\xa9too",
    "cprt",
    "cpil",
    "trkn",
    "disk",
    None,
    None,
    None,
    "----:com.apple.iTunes:ISRC",
    "©pub",  # label/publisher
    "tmpo",  # bpm
    "----:com.apple.iTunes:BARCODE",  # was UPC
    "----:com.apple.iTunes:REPLAYGAIN_TRACK_GAIN",  # was GAIN
    "----:com.apple.iTunes:REPLAYGAIN_ALBUM_GAIN",  # new
    "----:com.apple.iTunes:RELEASETYPE",  # was RECORD_TYPE
    None,  # source_track_id (handled dynamically)
    None,  # source_album_id (handled dynamically)
    None,  # source_artist_id (handled dynamically)
    "----:com.apple.iTunes:TRACK_ARTIST_CREDIT",
    "----:com.apple.iTunes:ALBUM_ARTIST_CREDIT", 
    "----:com.apple.iTunes:ORIGINALDATE",  # was ORIGINAL_RELEASE_DATE
    "----:com.apple.iTunes:MEDIA_TYPE",
)

MP3_KEYS = (
    id3.TIT2,  # type: ignore
    id3.TPE1,  # type: ignore
    None,  # artists (handled as TXXX)
    id3.TALB,  # type: ignore
    id3.TPE2,  # type: ignore
    id3.TCOM,  # type: ignore
    id3.TEXT,  # author/lyricist/songwriter
    id3.TYER,  # type: ignore
    id3.COMM,  # type: ignore
    id3.TIT1,  # description (content group)
    None,  # purchase_date (handled as TXXX)
    id3.GP1,  # type: ignore
    id3.TCON,  # type: ignore
    id3.USLT,  # type: ignore
    id3.TEN,  # type: ignore
    id3.TCOP,  # type: ignore
    id3.TCMP,  # type: ignore
    id3.TRCK,  # type: ignore
    id3.TPOS,  # type: ignore
    None,
    None,
    None,
    id3.TSRC,
    id3.TPUB,  # label/publisher 
    id3.TBPM,  # bpm
    None,  # barcode (handled as TXXX)
    None,  # replaygain_track_gain (handled as TXXX)
    None,  # replaygain_album_gain (handled as TXXX)
    None,  # releasetype (handled as TXXX)
    None,  # source_track_id (handled dynamically)
    None,  # source_album_id (handled dynamically) 
    None,  # source_artist_id (handled dynamically)
    None,  # track_artist_credit (handled as TXXX)
    None,  # album_artist_credit (handled as TXXX)
    id3.TDOR,  # originaldate
    None,  # media_type (handled as TXXX)
)

METADATA_TYPES = (
    "title",
    "artist",
    "artists",
    "album",
    "albumartist",
    "composer",
    "author",
    "year",
    "comment",
    "description",
    "purchase_date",
    "grouping",
    "genre",
    "lyrics",
    "encoder",
    "copyright",
    "compilation",
    "tracknumber",
    "discnumber",
    "tracktotal",
    "disctotal", 
    "date",
    "isrc",
    "label",
    "bpm",
    "barcode",
    "replaygain_track_gain",
    "replaygain_album_gain", 
    "releasetype",
    "source_track_id",
    "source_album_id", 
    "source_artist_id",
    "track_artist_credit",
    "album_artist_credit",
    "originaldate",
    "media_type",
)


FLAC_KEY = {v: v.upper() for v in METADATA_TYPES}
MP4_KEY = dict(zip(METADATA_TYPES, MP4_KEYS))
MP3_KEY = dict(zip(METADATA_TYPES, MP3_KEYS))


class Container(Enum):
    FLAC = 1
    AAC = 2
    MP3 = 3

    def get_mutagen_class(self, path: str):
        if self == Container.FLAC:
            return FLAC(path)
        elif self == Container.AAC:
            return MP4(path)
        elif self == Container.MP3:
            try:
                return ID3(path)
            except ID3NoHeaderError:
                return ID3()
        # unreachable
        return {}

    def get_tag_pairs(self, meta) -> list[tuple]:
        if self == Container.FLAC:
            return self._tag_flac(meta)
        elif self == Container.MP3:
            return self._tag_mp3(meta)
        elif self == Container.AAC:
            return self._tag_mp4(meta)
        # unreachable
        return []

    def _tag_flac(self, meta: TrackMetadata) -> list[tuple]:
        out = []
        for k, v in FLAC_KEY.items():
            tag = self._attr_from_meta(meta, k)
            if tag:
                if k in {
                    "tracknumber",
                    "discnumber",
                    "tracktotal",
                    "disctotal",
                }:
                    tag = f"{int(tag):02}"
                elif k in ["source_track_id", "source_album_id", "source_artist_id"]:
                    # Format source tags dynamically based on platform
                    if meta.source_platform and tag:
                        formatted_key = f"{meta.source_platform}_{k.replace('source_', '')}".upper()
                        out.append((formatted_key, str(tag)))
                    continue
                elif k == "artists":
                    # Handle multi-value artists for FLAC - return as list for mutagen
                    if isinstance(tag, list):
                        out.append((v, tag))  # Let mutagen handle the list natively
                    else:
                        out.append((v, str(tag)))
                    continue
                
                out.append((v, str(tag)))
        return out

    def _tag_mp3(self, meta: TrackMetadata):
        out = []
        for k, v in MP3_KEY.items():
            if k == "tracknumber":
                text = f"{meta.tracknumber}/{meta.album.tracktotal}"
            elif k == "discnumber":
                text = f"{meta.discnumber}/{meta.album.disctotal}"
            elif k in ["source_track_id", "source_album_id", "source_artist_id"]:
                # Format source tags dynamically based on platform
                if meta.source_platform and self._attr_from_meta(meta, k):
                    formatted_key = f"TXXX:{meta.source_platform}_{k.replace('source_', '')}".upper()
                    text = self._attr_from_meta(meta, k)
                    if text is not None:
                        out.append((formatted_key, text))
                continue
            elif k == "artists":
                # Handle artists as TXXX with comma-separated values
                artists = self._attr_from_meta(meta, k)
                if artists is not None:
                    text = ", ".join(artists) if isinstance(artists, list) else str(artists)
                    out.append((f"TXXX:{k.upper()}", text))
                continue
            elif k in ["barcode", "replaygain_track_gain", "replaygain_album_gain", "releasetype", "track_artist_credit", "album_artist_credit", "media_type", "purchase_date", "originaldate"]:
                # Handle as TXXX custom tags
                text = self._attr_from_meta(meta, k)
                if text is not None:
                    out.append((f"TXXX:{k.upper()}", str(text)))
                continue
            else:
                text = self._attr_from_meta(meta, k)

            if text is not None and v is not None:
                out.append((v.__name__, v(encoding=3, text=text)))
        return out

    def _tag_mp4(self, meta: TrackMetadata):
        out = []
        for k, v in MP4_KEY.items():
            if k == "tracknumber":
                text = [(meta.tracknumber, meta.album.tracktotal)]
            elif k == "discnumber":
                text = [(meta.discnumber, meta.album.disctotal)]
            elif k == "isrc" and meta.isrc is not None:
                # because ISRC is an mp4 freeform value (not supported natively)
                # we have to pass in the actual bytes to mutagen
                # See mutagen.MP4Tags.__render_freeform
                text = meta.isrc.encode("utf-8")
            elif k in ["source_track_id", "source_album_id", "source_artist_id"]:
                # Format source tags dynamically based on platform
                if meta.source_platform and self._attr_from_meta(meta, k):
                    formatted_key = f"----:com.apple.iTunes:{meta.source_platform.upper()}_{k.replace('source_', '').upper()}"
                    text = self._attr_from_meta(meta, k)
                    if text is not None:
                        text = text.encode("utf-8")  # MP4 freeform tags need bytes
                        out.append((formatted_key, text))
                continue
            elif k == "artists" and v is not None:
                # Handle artists as MP4 freeform with byte encoding
                artists = self._attr_from_meta(meta, k)
                if artists is not None:
                    text = ", ".join(artists) if isinstance(artists, list) else str(artists)
                    text = text.encode("utf-8")
                    out.append((v, text))
                continue
            elif k in ["barcode", "replaygain_track_gain", "replaygain_album_gain", "releasetype", "track_artist_credit", "album_artist_credit", "originaldate", "media_type"] and v is not None:
                # Handle custom MP4 freeform tags that need bytes encoding
                text = self._attr_from_meta(meta, k)
                if text is not None:
                    text = str(text).encode("utf-8")
                    out.append((v, text))
                continue
            else:
                text = self._attr_from_meta(meta, k)

            if v is not None and text is not None:
                out.append((v, text))
        return out

    def _attr_from_meta(self, meta: TrackMetadata, attr: str) -> str | None:
        # TODO: verify this works
        in_trackmetadata = {
            "title",
            "album",
            "artist",
            "artists",
            "tracknumber",
            "discnumber",
            "composer",
            "author",
            "isrc",
            "lyrics",
            # Track-specific source metadata
            "source_platform",
            "source_track_id",
            "source_album_id", 
            "source_artist_id",
            # Track-specific additional metadata
            "bpm",
            "replaygain_track_gain",
            "track_artist_credit",
            "media_type",
        }
        if attr in in_trackmetadata:
            if attr == "album":
                return meta.album.album
            elif attr == "artist":
                # Return primary artist only (string)
                return getattr(meta, attr)
            elif attr == "artists":
                # Return all artists as list for format handlers to process
                return getattr(meta, attr)
            val = getattr(meta, attr)
            if val is None:
                return None
            return str(val)
        else:
            if attr == "genre":
                return meta.album.get_genres()
            elif attr == "copyright":
                return meta.album.get_copyright()
            elif attr == "label":
                return meta.album.info.label
            val = getattr(meta.album, attr)
            if val is None:
                return None
            return str(val)

    def tag_audio(self, audio, tags: list[tuple]):
        for k, v in tags:
            if k.startswith("TXXX:"):
                # Handle TXXX frames for custom tags
                description = k.split(":", 1)[1]
                txxx = id3.TXXX(encoding=3, desc=description, text=v)
                audio.add(txxx)
            else:
                # Handle regular tags
                audio[k] = v

    async def embed_cover(self, audio, cover_path):
        if self == Container.FLAC:
            size = os.path.getsize(cover_path)
            if size > FLAC_MAX_BLOCKSIZE:
                raise Exception("Cover art too big for FLAC")
            cover = Picture()
            cover.type = 3
            cover.mime = "image/jpeg"
            async with aiofiles.open(cover_path, "rb") as img:
                cover.data = await img.read()
            audio.add_picture(cover)
        elif self == Container.MP3:
            cover = APIC()
            cover.type = 3
            cover.mime = "image/jpeg"
            async with aiofiles.open(cover_path, "rb") as img:
                cover.data = await img.read()
            audio.add(cover)
        elif self == Container.AAC:
            async with aiofiles.open(cover_path, "rb") as img:
                cover = MP4Cover(await img.read(), imageformat=MP4Cover.FORMAT_JPEG)
            audio["covr"] = [cover]

    def save_audio(self, audio, path):
        if self == Container.FLAC:
            audio.save()
        elif self == Container.AAC:
            audio.save()
        elif self == Container.MP3:
            audio.save(path, "v2_version=3")


async def tag_file(path: str, meta: TrackMetadata, cover_path: str | None):
    ext = path.split(".")[-1].lower()
    if ext == "flac":
        container = Container.FLAC
    elif ext == "m4a":
        container = Container.AAC
    elif ext == "mp3":
        container = Container.MP3
    else:
        raise Exception(f"Invalid extension {ext}")

    audio = container.get_mutagen_class(path)
    tags = container.get_tag_pairs(meta)
    logger.debug("Tagging with %s", tags)
    container.tag_audio(audio, tags)
    if cover_path is not None:
        await container.embed_cover(audio, cover_path)
    container.save_audio(audio, path)
