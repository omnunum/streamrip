"""Tests for release-level skip optimization."""

import asyncio
import tempfile
import os
from unittest.mock import Mock, AsyncMock
import pytest

from streamrip.db import Database, Downloads, Failed, DownloadedReleases
from streamrip.media.album import PendingAlbum
from streamrip.media.artist import PendingArtist
from streamrip.media.label import PendingLabel


@pytest.fixture
def temp_database():
    """Create a temporary database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        downloads_path = os.path.join(tmpdir, 'downloads.db')
        failed_path = os.path.join(tmpdir, 'failed.db') 
        releases_path = os.path.join(tmpdir, 'releases.db')
        
        downloads_db = Downloads(downloads_path)
        failed_db = Failed(failed_path)
        releases_db = DownloadedReleases(releases_path)
        
        yield Database(downloads_db, failed_db, releases_db)


class MockClient:
    def __init__(self, source="deezer"):
        self.source = source
        self.session = Mock()
    
    async def get_metadata(self, id, media_type):
        # Return minimal mock data to avoid actual API calls
        return {"id": id, "title": f"Test {media_type.title()}", "tracks": []}


class TestCoreReleaseOptimization:
    """Test core release optimization functionality."""

    @pytest.mark.asyncio
    async def test_album_skip_optimization_works(self, temp_database):
        """Test that album skip optimization works for already downloaded albums."""
        client = MockClient("deezer")
        album_id = "test-album-456"
        
        # Mark album as already downloaded
        temp_database.set_release_downloaded(album_id, "album", "deezer", 10)
        
        # Mock get_metadata - this should NOT be called due to optimization
        client.get_metadata = AsyncMock()
        
        pending_album = PendingAlbum(album_id, client, Mock(), temp_database)
        result = await pending_album.resolve()
        
        # Should return None (skipped) and not call API
        assert result is None
        client.get_metadata.assert_not_called()

    @pytest.mark.asyncio
    async def test_album_proceeds_when_not_downloaded(self, temp_database):
        """Test that albums not in database proceed with resolution."""
        client = MockClient("deezer")
        album_id = "test-album-123"
        
        # Should not be marked as downloaded yet
        assert not temp_database.release_downloaded(album_id, "album", "deezer")
        
        client.get_metadata = AsyncMock(return_value={"id": album_id})
        pending_album = PendingAlbum(album_id, client, Mock(), temp_database)
        
        # Should proceed (get_metadata called)
        await pending_album.resolve()
        client.get_metadata.assert_called_once_with(album_id, "album")

    def test_database_tracking_cross_source_and_type(self, temp_database):
        """Test that release tracking correctly handles different sources and media types."""
        release_id = "multi-test-123"
        
        # Mark album from Deezer as downloaded
        temp_database.set_release_downloaded(release_id, "album", "deezer", 10)
        
        # Test source and type specificity
        assert temp_database.release_downloaded(release_id, "album", "deezer")  # True
        assert not temp_database.release_downloaded(release_id, "album", "qobuz")  # Different source
        assert not temp_database.release_downloaded(release_id, "artist", "deezer")  # Different type
        
        # Mark same ID as artist from Qobuz - should track independently
        temp_database.set_release_downloaded(release_id, "artist", "qobuz", 5)
        
        assert temp_database.release_downloaded(release_id, "album", "deezer")
        assert temp_database.release_downloaded(release_id, "artist", "qobuz")
        assert not temp_database.release_downloaded(release_id, "artist", "deezer")

    @pytest.mark.asyncio
    async def test_artist_new_release_detection(self, temp_database):
        """Test that artists correctly detect and process new releases."""
        client = MockClient("deezer")
        client.get_metadata = AsyncMock(return_value={
            "id": "artist123", 
            "name": "Test Artist",
            "albums": [{"id": "album1"}, {"id": "album2"}, {"id": "album3"}]
        })
        
        # Mark one album as already downloaded
        temp_database.set_release_downloaded("album2", "album", "deezer", 8)
        
        pending_artist = PendingArtist("artist123", client, Mock(), temp_database)
        result = await pending_artist.resolve()
        
        # Should create Artist object with all albums (including downloaded ones)
        assert result is not None
        assert len(result.albums) == 3
        assert result.artist_id == "artist123"

    @pytest.mark.asyncio
    async def test_label_new_release_detection(self, temp_database):
        """Test that labels correctly detect and process new releases."""
        client = MockClient("qobuz")
        client.get_metadata = AsyncMock(return_value={
            "id": "label456", 
            "name": "Test Label",
            "albums": [{"id": "album4"}, {"id": "album5"}]
        })
        
        # Mark all albums as downloaded - should still return None when all are complete
        temp_database.set_release_downloaded("album4", "album", "qobuz", 10)
        temp_database.set_release_downloaded("album5", "album", "qobuz", 12)
        
        pending_label = PendingLabel("label456", client, Mock(), temp_database)
        result = await pending_label.resolve()
        
        # Should return None when all albums are already downloaded
        assert result is None