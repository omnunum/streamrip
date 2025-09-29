import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from streamrip.media.album import Album
from streamrip.media.playlist import Playlist


class TestErrorHandling:
    """Test error handling in playlist and album downloads."""

    @pytest.mark.asyncio
    async def test_playlist_handles_failed_track(self):
        """Test that a playlist download continues even if one track fails."""
        mock_config = MagicMock()
        mock_client = MagicMock()

        mock_track_success = MagicMock()
        mock_track_success.resolve = AsyncMock(return_value=MagicMock())
        mock_track_success.resolve.return_value.rip = AsyncMock()

        mock_track_failure = MagicMock()
        mock_track_failure.resolve = AsyncMock(
            side_effect=json.JSONDecodeError("Expecting value", "", 0)
        )

        playlist = Playlist(
            name="Test Playlist",
            config=mock_config,
            client=mock_client,
            tracks=[mock_track_success, mock_track_failure],
        )

        await playlist.download()

        mock_track_success.resolve.assert_called_once()
        mock_track_success.resolve.return_value.rip.assert_called_once()
        mock_track_failure.resolve.assert_called_once()

    @pytest.mark.asyncio
    async def test_album_download_is_noop(self):
        """Test that Album.download() is now a no-op (tracks handled by Main queue)."""
        mock_config = MagicMock()
        mock_db = MagicMock()
        mock_meta = MagicMock()

        # Create mock tracks
        mock_track_success = MagicMock()
        mock_track_failure = MagicMock()

        album = Album(
            meta=mock_meta,
            config=mock_config,
            tracks=[mock_track_success, mock_track_failure],
            folder="/test/folder",
            db=mock_db,
        )

        # Album.download() should now be a no-op
        await album.download()

        # Tracks should NOT be processed - they're handled by Main's queue system
        mock_track_success.resolve.assert_not_called()
        mock_track_failure.resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_main_stream_process_pending_handles_failed_pending(self):
        """Test that Main.stream_process_pending handles failed pending items."""
        from streamrip.rip.main import Main

        mock_config = MagicMock()
        mock_config.session.downloads.requests_per_minute = 0
        mock_config.session.downloads.max_connections = 1  # For worker pool
        mock_config.session.database.downloads_enabled = False
        mock_config.session.database.failed_downloads_enabled = False
        mock_config.session.rym.enabled = False  # Disable RYM for test

        with (
            patch("streamrip.rip.main.QobuzClient"),
            patch("streamrip.rip.main.TidalClient"),
            patch("streamrip.rip.main.DeezerClient"),
            patch("streamrip.rip.main.SoundcloudClient"),
        ):
            main = Main(mock_config)

            # Create mock pending items that resolve to media
            mock_pending_success = MagicMock()
            mock_media_success = MagicMock(spec=['rip'])  # Only spec 'rip', not 'tracks'
            mock_media_success.rip = AsyncMock()
            mock_pending_success.resolve = AsyncMock(return_value=mock_media_success)

            mock_pending_failure = MagicMock()
            mock_pending_failure.resolve = AsyncMock(
                side_effect=Exception("Resolve failed")
            )

            # Add pending items (not media) - this is the new architecture
            main.pending = [mock_pending_success, mock_pending_failure]

            await main.stream_process_pending()

            # Verify the successful item was processed
            mock_pending_success.resolve.assert_called_once()
            mock_media_success.rip.assert_called_once()

            # Verify the failed item was attempted
            mock_pending_failure.resolve.assert_called_once()
