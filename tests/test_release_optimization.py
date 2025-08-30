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


class MockClient:
    def __init__(self, source="deezer"):
        self.source = source
        self.session = Mock()
    
    async def get_metadata(self, id, media_type):
        # Return minimal mock data to avoid actual API calls
        return {"id": id, "title": f"Test {media_type.title()}", "tracks": []}


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


@pytest.fixture
def mock_config():
    """Create a mock config object."""
    return Mock()


class TestReleaseSkipOptimization:
    """Test release-level skip optimization for albums, artists, and labels."""

    @pytest.mark.asyncio
    async def test_album_not_downloaded_proceeds(self, temp_database, mock_config):
        """Test that albums not in database proceed with resolution."""
        client = MockClient("deezer")
        album_id = "test-album-123"
        
        # Mock get_metadata to prevent real API calls
        client.get_metadata = AsyncMock(return_value={"id": album_id})
        
        pending_album = PendingAlbum(album_id, client, mock_config, temp_database)
        
        # Should not be marked as downloaded yet
        assert not temp_database.release_downloaded(album_id, "album", "deezer")
        
        # Resolve will fail due to mock data but won't skip due to optimization
        result = await pending_album.resolve()
        
        # Verify get_metadata was called (not skipped)
        client.get_metadata.assert_called_once_with(album_id, "album")

    @pytest.mark.asyncio  
    async def test_album_already_downloaded_skips(self, temp_database, mock_config):
        """Test that already downloaded albums are skipped."""
        client = MockClient("deezer")
        album_id = "test-album-456"
        
        # Mark album as already downloaded
        temp_database.set_release_downloaded(album_id, "album", "deezer", 10)
        assert temp_database.release_downloaded(album_id, "album", "deezer")
        
        # Mock get_metadata - this should NOT be called
        client.get_metadata = AsyncMock()
        
        pending_album = PendingAlbum(album_id, client, mock_config, temp_database)
        result = await pending_album.resolve()
        
        # Should return None (skipped)
        assert result is None
        
        # Verify get_metadata was NOT called (optimization worked)
        client.get_metadata.assert_not_called()

    @pytest.mark.asyncio
    async def test_album_different_source_not_skipped(self, temp_database, mock_config):
        """Test that same album from different source is not skipped."""
        album_id = "test-album-789"
        
        # Mark album as downloaded for Deezer
        temp_database.set_release_downloaded(album_id, "album", "deezer", 10)
        
        # Create Qobuz client for same album ID
        qobuz_client = MockClient("qobuz")
        qobuz_client.get_metadata = AsyncMock(return_value={"id": album_id})
        
        pending_album = PendingAlbum(album_id, qobuz_client, mock_config, temp_database)
        
        # Should not be marked as downloaded for Qobuz source
        assert not temp_database.release_downloaded(album_id, "album", "qobuz")
        
        # Should proceed with resolution (not skip)
        result = await pending_album.resolve()
        
        # Verify get_metadata was called for Qobuz (not skipped)
        qobuz_client.get_metadata.assert_called_once_with(album_id, "album")

    @pytest.mark.asyncio
    async def test_artist_skip_optimization(self, temp_database, mock_config):
        """Test artist skip optimization works."""
        client = MockClient("deezer")
        artist_id = "test-artist-123"
        
        # Mark artist as downloaded
        temp_database.set_release_downloaded(artist_id, "artist", "deezer", 5)
        
        client.get_metadata = AsyncMock()
        pending_artist = PendingArtist(artist_id, client, mock_config, temp_database)
        
        result = await pending_artist.resolve()
        
        # Should be skipped
        assert result is None
        client.get_metadata.assert_not_called()

    @pytest.mark.asyncio 
    async def test_label_skip_optimization(self, temp_database, mock_config):
        """Test label skip optimization works."""
        client = MockClient("qobuz")
        label_id = "test-label-456"
        
        # Mark label as downloaded
        temp_database.set_release_downloaded(label_id, "label", "qobuz", 8)
        
        client.get_metadata = AsyncMock()
        pending_label = PendingLabel(label_id, client, mock_config, temp_database)
        
        result = await pending_label.resolve()
        
        # Should be skipped  
        assert result is None
        client.get_metadata.assert_not_called()

    def test_database_release_tracking(self, temp_database):
        """Test basic database release tracking functionality."""
        # Initially not downloaded
        assert not temp_database.release_downloaded("test-id", "album", "deezer")
        
        # Mark as downloaded
        temp_database.set_release_downloaded("test-id", "album", "deezer", 12)
        
        # Should now be marked as downloaded
        assert temp_database.release_downloaded("test-id", "album", "deezer")
        
        # Different source should not be marked
        assert not temp_database.release_downloaded("test-id", "album", "qobuz")
        
        # Different type should not be marked
        assert not temp_database.release_downloaded("test-id", "artist", "deezer")

    def test_database_source_and_type_specificity(self, temp_database):
        """Test that release tracking is specific to source and media type."""
        release_id = "multi-test-123"
        
        # Mark album from Deezer as downloaded
        temp_database.set_release_downloaded(release_id, "album", "deezer", 10)
        
        # Test all combinations
        assert temp_database.release_downloaded(release_id, "album", "deezer")  # True
        assert not temp_database.release_downloaded(release_id, "album", "qobuz")  # Different source
        assert not temp_database.release_downloaded(release_id, "artist", "deezer")  # Different type
        assert not temp_database.release_downloaded(release_id, "playlist", "deezer")  # Different type
        
        # Mark same ID as artist from Qobuz
        temp_database.set_release_downloaded(release_id, "artist", "qobuz", 5)
        
        # Both should now be tracked independently
        assert temp_database.release_downloaded(release_id, "album", "deezer")
        assert temp_database.release_downloaded(release_id, "artist", "qobuz")
        assert not temp_database.release_downloaded(release_id, "artist", "deezer")

    def test_edge_case_pre_optimization_downloads(self, temp_database):
        """Test edge case where tracks were downloaded before optimization existed."""
        # Simulate pre-optimization state: individual tracks downloaded, album not marked
        temp_database.set_downloaded("track1")
        temp_database.set_downloaded("track2") 
        temp_database.set_downloaded("track3")
        
        # Album should not be marked as complete yet
        assert not temp_database.release_downloaded("album123", "album", "deezer")
        
        # But individual tracks should be downloaded
        assert temp_database.downloaded("track1")
        assert temp_database.downloaded("track2")
        assert temp_database.downloaded("track3")
        
        # This simulates the edge case scenario that will be handled by
        # the enhanced resolve() methods in Album/Artist/Label classes