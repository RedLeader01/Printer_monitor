#!/usr/bin/env python3
"""Nyomtatófigyelő indítása:  python run.py   (teszt egy nyomtatóra: python run.py test IP)"""
import sys

from printer_monitor.cli import main

if __name__ == "__main__":
    sys.exit(main())
