import os
import pytest
from util import arun

from streamrip.client.downloadable import DeezerDownloadable
from streamrip.client.deezer import DeezerClient
from streamrip.config import Config
from streamrip.exceptions import MissingCredentialsError

@pytest.fixture(scope="session")
def deezer_client():
    config = Config.defaults()
    config.session.deezer.arl = os.environ.get("DEEZER_ARL", "")
    config.session.deezer.quality = 2  # FLAC
    config.session.deezer.fallback_quality = 1  # MP3_320
    client = DeezerClient(config)
    arun(client.login())
    
    yield client
    
    arun(client.session.close())

def test_client_raises_missing_credentials():
    c = Config.defaults()
    with pytest.raises(MissingCredentialsError):
        arun(DeezerClient(c).login())

@pytest.mark.skipif(
    "DEEZER_ARL" not in os.environ, reason="Deezer ARL not found in env."
)
def test_deezer_fallback_behavior_with_track_77874822(deezer_client):
    """Test fallback quality behavior with actual track 77874822"""
    # Test with high quality (FLAC) - should fallback if not available
    downloadable = arun(deezer_client.get_downloadable("77874822", quality=2))
    
    assert isinstance(downloadable, DeezerDownloadable)
    assert downloadable.quality in [1, 2]  # Either FLAC or fallback MP3_320
    assert isinstance(downloadable.url, str)
    assert "https://" in downloadable.url
    
    # Log what quality we actually got for debugging
    quality_names = {0: "MP3_128", 1: "MP3_320", 2: "FLAC"}
    print(f"Track 77874822 downloaded at quality {downloadable.quality} ({quality_names[downloadable.quality]})")

@pytest.mark.skipif(
    "DEEZER_ARL" not in os.environ, reason="Deezer ARL not found in env."
)
def test_deezer_metadata_track_77874822(deezer_client):
    """Test metadata retrieval for track 77874822"""
    metadata = arun(deezer_client.get_metadata("77874822", "track"))
    
    assert "title" in metadata
    assert "artist" in metadata
    assert "album" in metadata
    assert metadata["id"] == "77874822"
    
    print(f"Track: {metadata['title']} by {metadata['artist']['name']}")

@pytest.mark.skipif(
    "DEEZER_ARL" not in os.environ, reason="Deezer ARL not found in env."
)
def test_deezer_fallback_config_respected(deezer_client):
    """Test that fallback quality config is properly respected"""
    # Verify the client has the correct fallback config
    assert deezer_client.config.fallback_quality == 1
    assert deezer_client.config.quality == 2
    
    # Test that fallback logic works when requesting unavailable quality
    # This will test the actual fallback behavior in get_downloadable
    downloadable = arun(deezer_client.get_downloadable("77874822", quality=2))
    
    # Should either get requested quality or fallback
    assert downloadable.quality in [1, 2]
