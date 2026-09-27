#!/usr/bin/env python3
"""Entry point: ./scout.py --help"""
import sys

from job_scout.cli import main

if __name__ == "__main__":
    sys.exit(main())
