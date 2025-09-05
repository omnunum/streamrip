import hashlib
import logging
import os

import pytest
from util import arun

from streamrip.client.downloadable import BasicDownloadable
from streamrip.client.qobuz import QobuzClient
from streamrip.config import Config
from streamrip.exceptions import MissingCredentialsError

logger = logging.getLogger("streamrip")


@pytest.fixture(scope="session")
def qobuz_client():
    config = Config.defaults()
    config.session.qobuz.email_or_userid = os.environ["QOBUZ_EMAIL"]
    config.session.qobuz.password_or_token = hashlib.md5(
        os.environ["QOBUZ_PASSWORD"].encode("utf-8"),
    ).hexdigest()
    if "QOBUZ_APP_ID" in os.environ and "QOBUZ_SECRETS" in os.environ:
        config.session.qobuz.app_id = os.environ["QOBUZ_APP_ID"]
        config.session.qobuz.secrets = os.environ["QOBUZ_SECRETS"].split(",")
    client = QobuzClient(config)
    arun(client.login())

    yield client

    arun(client.session.close())


def test_client_raises_missing_credentials():
    c = Config.defaults()
    with pytest.raises(MissingCredentialsError):
        arun(QobuzClient(c).login())


@pytest.mark.skipif(
    "QOBUZ_EMAIL" not in os.environ, reason="Qobuz credentials not found in env."
)
def test_client_get_metadata(qobuz_client):
    meta = arun(qobuz_client.get_metadata("0656605209067", "album"))
    assert meta["title"] == "In The Future"
    assert len(meta["tracks"]) == 10
    assert meta["maximum_bit_depth"] == 16


@pytest.mark.skipif(
    "QOBUZ_EMAIL" not in os.environ, reason="Qobuz credentials not found in env."
)
def test_client_get_downloadable(qobuz_client):
    d = arun(qobuz_client.get_downloadable("19512574", 3))
    assert isinstance(d, BasicDownloadable)
    assert d.extension == "flac"
    assert isinstance(d.url, str)
    assert "https://" in d.url


@pytest.mark.skipif(
    "QOBUZ_EMAIL" not in os.environ, reason="Qobuz credentials not found in env."
)
def test_client_search_limit(qobuz_client):
    res = qobuz_client.search("album", "rumours", limit=5)
    total = 0
    for r in arun(res):
        total += len(r["albums"]["items"])
    assert total == 5


@pytest.mark.skipif(
    "QOBUZ_EMAIL" not in os.environ, reason="Qobuz credentials not found in env."
)
def test_client_search_no_limit(qobuz_client):
    # Setting no limit has become impossible because `limit: int` now
    res = qobuz_client.search("album", "rumours", limit=10000)
    correct_total = 0
    total = 0
    for r in arun(res):
        total += len(r["albums"]["items"])
        correct_total = max(correct_total, r["albums"]["total"])
    assert total == correct_total


def test_parse_qobuz_performers():
    """Test parsing of Qobuz performers string."""
    # Test case from user example
    performers_str = "Earthless, Artist, MainArtist - Isaiah Mitchell, Composer, Author - Mario Rubalcaba, Composer - Mike Eginton, Composer"
    roles = QobuzClient.parse_performers(performers_str)
    
    expected_roles = {
        "Artist": ["Earthless"],
        "MainArtist": ["Earthless"],
        "Composer": ["Isaiah Mitchell", "Mario Rubalcaba", "Mike Eginton"],
        "Author": ["Isaiah Mitchell"]
    }
    assert roles == expected_roles
    
    # Test case from test data
    test_data_performers = "Trina Shoemaker, Producer - The Mountain Goats, MainArtist - John Darnielle, Composer, Lyricist - Cadmean Dawn (ASCAP) administered by Me Gusta Music, MusicPublisher"
    roles2 = QobuzClient.parse_performers(test_data_performers)
    
    expected_roles2 = {
        "Producer": ["Trina Shoemaker"],
        "MainArtist": ["The Mountain Goats"],
        "Composer": ["John Darnielle"],
        "Lyricist": ["John Darnielle"],
        "MusicPublisher": ["Cadmean Dawn (ASCAP) administered by Me Gusta Music"]
    }
    assert roles2 == expected_roles2
    
    # Test Elliott Smith example
    elliott_performers = "Elliott Smith, Composer, MainArtist - 2020 Spent Bullets Music/Universal Music Careers, MusicPublisher"
    roles3 = QobuzClient.parse_performers(elliott_performers)
    
    expected_roles3 = {
        "Composer": ["Elliott Smith"],
        "MainArtist": ["Elliott Smith"],
        "MusicPublisher": ["2020 Spent Bullets Music/Universal Music Careers"]
    }
    assert roles3 == expected_roles3
    
    # Edge cases
    assert QobuzClient.parse_performers(None) == {}
    assert QobuzClient.parse_performers("") == {}
    assert QobuzClient.parse_performers("Single Artist, MainArtist") == {"MainArtist": ["Single Artist"]}


def test_deduplicate_copyright():
    """Test deduplication of copyright strings."""
    # Test basic duplication cases
    assert QobuzClient.deduplicate_copyright("2022 Nuclear Blast 2022 Nuclear Blast") == "2022 Nuclear Blast"
    assert QobuzClient.deduplicate_copyright("2023 Merge Records 2023 Merge Records") == "2023 Merge Records"
    assert QobuzClient.deduplicate_copyright("2020 Universal Music 2020 Universal Music") == "2020 Universal Music"
    
    # Test cases that should not be modified
    assert QobuzClient.deduplicate_copyright("Single Label") == "Single Label"
    assert QobuzClient.deduplicate_copyright("") == ""
    assert QobuzClient.deduplicate_copyright("Different First Second Different") == "Different First Second Different"
    
    # Test three-word duplication
    assert QobuzClient.deduplicate_copyright("A B C A B C") == "A B C"


def test_qobuz_metadata_integration():
    """Test that parsing happens correctly in metadata classes."""
    import json
    from streamrip.metadata.album import AlbumMetadata
    from streamrip.metadata.track import TrackMetadata
    
    # Load real test data
    with open("tests/qobuz_track_resp.json") as f:
        qobuz_track_resp = json.load(f)
    album_resp = qobuz_track_resp["album"].copy()
    album_resp["copyright"] = "2022 Nuclear Blast 2022 Nuclear Blast"  # Add duplication
    
    # Simulate client-level deduplication
    album_resp["copyright"] = QobuzClient.deduplicate_copyright(album_resp["copyright"])
    
    album_meta = AlbumMetadata.from_qobuz(album_resp)
    assert album_meta.copyright == "2022 Nuclear Blast"  # Should be deduplicated
    
    # Test track performer parsing (simulate client processing)
    track_resp = qobuz_track_resp.copy()
    track_resp["performers"] = "Earthless, Artist, MainArtist - Isaiah Mitchell, Composer, Author - Mario Rubalcaba, Composer"
    
    # Simulate client-level parsing
    track_resp["_parsed_performer_roles"] = QobuzClient.parse_performers(track_resp["performers"])
    
    track_meta = TrackMetadata.from_qobuz(album_meta, track_resp)
    
    # Should combine base composer with parsed composers (now lists)
    assert "John Darnielle" in track_meta.composer  # Base composer from API
    assert "Isaiah Mitchell" in track_meta.composer  # From performers
    assert "Mario Rubalcaba" in track_meta.composer  # From performers
    assert track_meta.author == ["Isaiah Mitchell"]  # From performers Author role
    
    # Test duplicate avoidance - if same composer is in both base and performers
    track_resp_duplicate = qobuz_track_resp.copy()
    track_resp_duplicate["performers"] = "John Darnielle, Composer - Isaiah Mitchell, Author"
    
    # Simulate client-level parsing
    track_resp_duplicate["_parsed_performer_roles"] = QobuzClient.parse_performers(track_resp_duplicate["performers"])
    
    track_meta_duplicate = TrackMetadata.from_qobuz(album_meta, track_resp_duplicate)
    
    # John Darnielle should only appear once despite being in both base and performers
    john_count = track_meta_duplicate.composer.count("John Darnielle")
    assert john_count == 1, f"John Darnielle appears {john_count} times, should be 1"


