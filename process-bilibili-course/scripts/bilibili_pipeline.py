#!/usr/bin/env python3
"""Backward-compatible wrapper for the unified public video pipeline."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from video_pipeline import main


if __name__ == "__main__":
    main()
