"""Integration test for release optimization with user favorites."""

import tempfile
import os
from unittest.mock import Mock
import pytest

from streamrip.db import Database, Downloads, Failed, DownloadedReleases
from streamrip.media.user_favorites import PendingUserFavorites


class MockDeezerClient:
    def __init__(self):
        self.source = "deezer"
        self.session = Mock()
    
    async def get_user_favorites(self, user_id, media_type):
        """Mock user favorites response."""
        _ = user_id  # Unused parameter
        if media_type == "albums":
            return {
                "data": [
                    {"id": "album1", "title": "Test Album 1"},
                    {"id": "album2", "title": "Test Album 2"},
                    {"id": "album3", "title": "Test Album 3"}
                ]
            }
        return {"data": []}


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


class TestUserFavoritesOptimization:
    """Test that user favorites benefit from release-level optimization."""

    @pytest.mark.asyncio
    async def test_user_favorites_skips_downloaded_albums(self, temp_database, mock_config):
        """Test user favorites skips already downloaded albums."""
        client = MockDeezerClient()
        user_id = "test-user-123"
        
        # Mark album2 as already downloaded
        temp_database.set_release_downloaded("album2", "album", "deezer", 8)
        
        # Create pending user favorites
        pending_favorites = PendingUserFavorites(
            user_id=user_id,
            media_type="albums", 
            client=client,
            config=mock_config,
            db=temp_database
        )
        
        # Resolve favorites
        user_favorites = await pending_favorites.resolve()
        
        assert user_favorites is not None
        assert len(user_favorites.items) == 3  # All albums returned
        assert user_favorites.media_type == "albums"
        assert user_favorites.user_id == user_id

    def test_favorites_with_mixed_download_states(self, temp_database):
        """Test database correctly handles mixed download states for user favorites."""
        # Simulate user with some albums already downloaded
        temp_database.set_release_downloaded("fav-album-1", "album", "deezer", 10)
        temp_database.set_release_downloaded("fav-album-3", "album", "deezer", 12)
        
        # Check states
        assert temp_database.release_downloaded("fav-album-1", "album", "deezer")  # Downloaded
        assert not temp_database.release_downloaded("fav-album-2", "album", "deezer")  # Not downloaded
        assert temp_database.release_downloaded("fav-album-3", "album", "deezer")  # Downloaded
        
        # Different source should not be marked
        assert not temp_database.release_downloaded("fav-album-1", "album", "qobuz")

    def test_cross_media_type_tracking(self, temp_database):
        """Test that same ID can be tracked across different media types."""
        same_id = "shared-id-123"
        
        # Mark as album and artist for same ID (could happen with Deezer IDs)
        temp_database.set_release_downloaded(same_id, "album", "deezer", 10)
        temp_database.set_release_downloaded(same_id, "artist", "qobuz", 5)
        
        # Should track independently
        assert temp_database.release_downloaded(same_id, "album", "deezer")
        assert temp_database.release_downloaded(same_id, "artist", "qobuz") 
        assert not temp_database.release_downloaded(same_id, "album", "qobuz")
        assert not temp_database.release_downloaded(same_id, "artist", "deezer")