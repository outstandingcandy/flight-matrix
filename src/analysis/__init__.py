"""
Analysis package - handles AI-powered flight analysis.

This package is responsible ONLY for running analysis.
It does NOT handle email sending or content building.
"""

from .service import FlightAnalysisService

__all__ = ["FlightAnalysisService"]
