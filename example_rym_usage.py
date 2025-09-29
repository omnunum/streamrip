#!/usr/bin/env python3
"""
Example script demonstrating RYM metadata integration with streamrip.

This script shows how to:
1. Configure RYM metadata enrichment
2. Create album metadata
3. Enrich it with RateYourMusic data
4. See the results
"""

import asyncio
import os
import tempfile
from streamrip.config import RymConfig
from streamrip.metadata.rym_service import RymMetadataService
from streamrip.metadata.album import AlbumMetadata, AlbumInfo
from streamrip.metadata.covers import Covers


async def main():
    print("🎵 RYM Metadata Integration Example\n")

    # Create RYM configuration
    rym_config = RymConfig(
        enabled=True,
        genre_mode="replace",  # "replace" or "append"
        proxy_enabled=False,
        proxy_host="",
        proxy_port_start=8080,
        proxy_port_end=8090,
        proxy_username="",
        proxy_password="",
        proxy_rotation_method="port",
        session_type="sticky",
        auto_rotate_on_failure=True
    )

    # Create RYM service
    with tempfile.TemporaryDirectory() as cache_dir:
        rym_service = RymMetadataService(rym_config, cache_dir)

        # Create example album metadata
        info = AlbumInfo(
            id="test_album",
            quality=2,
            container="FLAC"
        )

        album = AlbumMetadata(
            info=info,
            album="OK Computer",
            albumartist="Radiohead",
            year="1997",
            genre=["Alternative Rock"],  # Original genre from streaming service
            covers=Covers(),
            tracktotal=12
        )

        print("📀 Original Album Metadata:")
        print(f"   Artist: {album.albumartist}")
        print(f"   Album: {album.album}")
        print(f"   Year: {album.year}")
        print(f"   Original Genres: {album.genre}")
        print(f"   RYM Descriptors: {album.rym_descriptors}")
        print()

        # Enrich with RYM data
        print("🌐 Enriching with RateYourMusic metadata...")
        await album.enrich_with_rym(rym_service)

        print("✨ Enriched Album Metadata:")
        print(f"   Artist: {album.albumartist}")
        print(f"   Album: {album.album}")
        print(f"   Year: {album.year}")
        print(f"   Enriched Genres: {album.genre}")
        print(f"   RYM Descriptors: {album.rym_descriptors}")
        print()

        # Show how genres are handled based on configuration
        print("🔧 Genre Mode Configuration:")
        print(f"   Current mode: {rym_config.genre_mode}")
        if rym_config.genre_mode == "replace":
            print("   → RYM genres replace original genres")
        elif rym_config.genre_mode == "append":
            print("   → RYM genres are added to original genres")
        print()

        # Cleanup
        await rym_service.close()

        print("💾 The enriched metadata would be tagged to audio files with:")
        print("   FLAC: RYM_DESCRIPTORS tag")
        print("   MP3: TXXX:RYM_DESCRIPTORS tag")
        print("   MP4: ----:com.apple.iTunes:RYM_DESCRIPTORS tag")


if __name__ == "__main__":
    print("Note: This example requires internet access to fetch RYM data.")
    print("Set RYM proxy configuration in the script if needed for your setup.\n")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n❌ Interrupted by user")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        print("\nThis might happen if:")
        print("- No internet connection")
        print("- RateYourMusic is blocking requests")
        print("- Album not found on RYM")
        print("- Proxy configuration needed")