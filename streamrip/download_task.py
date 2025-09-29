"""Download task dataclass for queue-based processing."""

from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .media import Media, PendingSingle


@dataclass
class DownloadTask:
    """Represents a download task for the global queue."""
    track: 'PendingSingle'  # The track to download
    album_ref: Optional['Media'] = None  # Reference to parent album if part of album
    retry_count: int = 0  # Number of times this task has been retried
    task_type: str = "download"  # Type of task: "download", "validate", etc.