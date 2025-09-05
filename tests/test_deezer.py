import os
import pytest
from unittest.mock import Mock, patch
from util import arun

from streamrip.client.deezer import DeezerClient
from streamrip.config import Config

@pytest.fixture(scope="session")
def deezer_client():
    """Integration test fixture - requires DEEZER_ARL environment variable"""
    config = Config.defaults()
    config.session.deezer.arl = os.environ.get("DEEZER_ARL", "")
    config.session.deezer.quality = 2  # FLAC
    config.session.deezer.lower_quality_if_not_available = True
    client = DeezerClient(config)
    arun(client.login())
    
    yield client
    
    arun(client.session.close())

@pytest.fixture
def mock_deezer_client():
    """Unit test fixture - mocked client for fast testing"""
    config = Config.defaults()
    config.session.deezer.arl = "test_arl"
    config.session.deezer.quality = 2
    config.session.deezer.lower_quality_if_not_available = True
    
    client = DeezerClient(config)
    client.client = Mock()
    client.client.gw = Mock()
    client.session = Mock()
    
    return client

def test_deezer_track_metadata_quality_selection():
    """Unit test: TrackMetadata.from_deezer sets quality to highest available"""
    from streamrip.metadata import AlbumMetadata, TrackMetadata
    
    # Mock album metadata
    mock_album_resp = {
        "id": "123",
        "title": "Test Album", 
        "artist": {"name": "Test Artist", "id": "456"},
        "release_date": "2020-01-01",
        "genres": {"data": [{"name": "Pop"}]},
        "tracks": [{"disk_number": 1}],
        "cover_xl": "https://test.jpg",
        "cover_big": "https://test.jpg", 
        "cover_medium": "https://test.jpg",
        "cover_small": "https://test.jpg",
        "nb_tracks": 1,
        "track_total": 1
    }
    album = AlbumMetadata.from_deezer(mock_album_resp)
    
    # Mock track response with mixed quality availability
    mock_track_resp = {
        "id": "789",
        "title": "Test Track",
        "isrc": "TEST123456789",
        "contributors": [{"name": "Test Artist", "id": "456"}],
        "track_position": 1,
        "disk_number": 1,
        "explicit_lyrics": False,
        "qualities": [
            "FILESIZE_MP3_128",  # Quality 0 available
            None,                # Quality 1 unavailable
            "FILESIZE_FLAC"      # Quality 2 available
        ]
    }
    
    # Create track metadata
    track = TrackMetadata.from_deezer(album, mock_track_resp)
    
    # Should select highest available quality (2 = FLAC)
    assert track.info.quality == 2
    assert track.info.streamable == True
    
    # Test with no qualities available
    mock_track_resp["qualities"] = [None, None, None]
    track_no_quality = TrackMetadata.from_deezer(album, mock_track_resp)
    assert track_no_quality.info.quality == 0  # Default fallback
    assert track_no_quality.info.streamable == False

# ===== UNIT TESTS =====

def test_deezer_quality_mapping(mock_deezer_client):
    """Unit test: quality int correctly maps to Deezer format"""
    mock_track_info = {
        "FILESIZE_FLAC": 25000000,
        "FILESIZE_MP3_320": 5000000,
        "FILESIZE_MP3_128": 2000000,
        "TRACK_TOKEN": "test_token"
    }
    
    mock_deezer_client.client.gw.get_track.return_value = mock_track_info
    mock_deezer_client.client.get_track_url.return_value = "https://test.flac"
    
    with patch.object(mock_deezer_client, 'get_session'):
        # Test quality 2 (FLAC)
        downloadable = arun(mock_deezer_client.get_downloadable("123", quality=2))
        assert downloadable.quality == 2
        
        # Test quality 1 (MP3_320)
        mock_deezer_client.client.get_track_url.return_value = "https://test.mp3"
        downloadable = arun(mock_deezer_client.get_downloadable("123", quality=1))
        assert downloadable.quality == 1
        
        # Test quality 0 (MP3_128)
        downloadable = arun(mock_deezer_client.get_downloadable("123", quality=0))
        assert downloadable.quality == 0

# ===== INTEGRATION TEST =====

@pytest.mark.skipif(
    "DEEZER_ARL" not in os.environ, reason="Deezer ARL not found in env."
)
def test_deezer_get_track_metadata(deezer_client):
    """Integration test: verify track metadata contains highest available quality"""
    # Get track metadata which should contain highest available quality
    track_data = arun(deezer_client.get_track("77874822"))
    
    # Verify qualities array is populated
    assert "qualities" in track_data
    qualities = track_data["qualities"]
    assert len(qualities) == 3  # [MP3_128, MP3_320, FLAC]
    
    # Find highest available quality
    available_indices = [i for i, q in enumerate(qualities) if q is not None]
    assert len(available_indices) > 0, "Should have at least one available quality"
    highest_quality = max(available_indices)
    
    print(f"Track qualities: {qualities}")
    print(f"Highest available quality: {highest_quality}")
    
    # Test downloadable creation works with the metadata
    downloadable = arun(deezer_client.get_downloadable("77874822", quality=highest_quality))
    assert downloadable.quality == highest_quality
    assert downloadable.url.startswith("https://")
    assert arun(downloadable.size()) > 0
