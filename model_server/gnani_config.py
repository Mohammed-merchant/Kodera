"""Shared configuration for the laptop voice demo."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def setting(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()
