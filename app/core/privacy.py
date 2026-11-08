"""Helpers that keep personal data out of logs."""


def mask_number(number: str) -> str:
    """Keep only the last 4 digits so phone numbers never land in logs in full."""
    return f"***{number[-4:]}" if len(number) > 4 else "***"
