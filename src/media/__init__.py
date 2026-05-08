"""
Media package - handles map generation and aircraft images.

This package is responsible ONLY for generating/retrieving media files.
It does NOT handle email sending or content building.
"""

from .service import MediaService

__all__ = ["MediaService"]
