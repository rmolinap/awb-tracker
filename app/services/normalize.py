from __future__ import annotations

import re


CARRIER_ALIASES = {
    "delta": "delta",
    "delta cargo": "delta",
    "united": "united",
    "united cargo": "united",
    "southwest": "southwest",
    "swa": "southwest",
    "southwest cargo": "southwest",
    "american": "american",
    "aa": "american",
    "american airlines cargo": "american",
    "alaska": "alaska",
    "alaska cargo": "alaska",
}


def normalize_carrier(value: str) -> str:
    return CARRIER_ALIASES.get(value.strip().lower(), value.strip().lower())


def normalize_awb(value: str) -> str:
    return re.sub(r"[-\s]+", "", value).strip()
