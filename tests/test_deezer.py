import os
import pytest
from unittest.mock import Mock, patch
from util import arun

from streamrip.client.downloadable import DeezerDownloadable
from streamrip.client.deezer import DeezerClient
from streamrip.config import Config

@pytest.fixture(scope="session")
def deezer_client():
    """Integration test fixture - requires DEEZER_ARL environment variable"""
    config = Config.defaults()
    config.session.deezer.arl = os.environ.get("DEEZER_ARL", "")
    config.session.deezer.quality = 2  # FLAC
    config.session.deezer.fallback_quality = 1  # MP3_320
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
    config.session.deezer.fallback_quality = 1
    
    client = DeezerClient(config)
    client.client = Mock()
    client.session = Mock()
    
    return client

# ===== UNIT TESTS =====

def test_deezer_fallback_logic_with_mock_data(mock_deezer_client):
    """Unit test: fallback logic works with mocked track data"""
    # Mock track info where FLAC is unavailable but MP3_320 is available
    mock_track_info = {
        "FILESIZE_1": 0,      # FLAC unavailable
        "FILESIZE_3": 5000000, # MP3_320 available
        "FILESIZE_9": 2000000, # MP3_128 available
        "TRACK_TOKEN": "test_token"
    }
    
    # Mock the client methods
    mock_deezer_client.client.gw.get_track.return_value = mock_track_info
    mock_deezer_client.client.get_track_url.return_value = "https://test.mp3"
    
    # Test fallback behavior
    with patch.object(mock_deezer_client, 'get_session'):
        downloadable = arun(mock_deezer_client.get_downloadable("123", quality=2))
        
        # Should have fallen back to quality 1 (MP3_320)
        assert downloadable.quality == 1

def test_deezer_no_fallback_when_quality_available(mock_deezer_client):
    """Unit test: no fallback when requested quality is available"""
    # Mock track info where FLAC is available
    mock_track_info = {
        "FILESIZE_1": 25000000, # FLAC available
        "FILESIZE_3": 5000000,  # MP3_320 available
        "FILESIZE_9": 2000000,  # MP3_128 available
        "TRACK_TOKEN": "test_token"
    }
    
    mock_deezer_client.client.gw.get_track.return_value = mock_track_info
    mock_deezer_client.client.get_track_url.return_value = "https://test.flac"
    
    with patch.object(mock_deezer_client, 'get_session'):
        downloadable = arun(mock_deezer_client.get_downloadable("123", quality=2))
        
        # Should use requested quality 2 (FLAC)
        assert downloadable.quality == 2

# ===== INTEGRATION TEST =====

@pytest.mark.skipif(
    "DEEZER_ARL" not in os.environ, reason="Deezer ARL not found in env."
)
def test_deezer_fallback_actually_occurred(deezer_client):
    """Integration test: verify fallback works with real track 77874822"""
    # We know track 77874822 doesn't have FLAC available, so test fallback scenario
    downloadable = arun(deezer_client.get_downloadable("77874822", quality=2))
    
    # Since we requested FLAC (quality=2) but it's not available,
    # we should have fallen back to the configured fallback_quality (1 = MP3_320)
    assert downloadable.quality == 1, "Should have fallen back to MP3_320 when FLAC unavailable"
    print("Fallback occurred: FLAC unavailable, fell back to MP3_320")
    
    # Verify the URL is actually accessible and working
    assert downloadable.url.startswith("https://")
    assert downloadable._size > 0, "Downloadable should have a valid file size"
    assert downloadable.extension == "mp3", "MP3_320 should have .mp3 extension"
