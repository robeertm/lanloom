"""Free text from the LAN may carry credentials: a camera puts `user:password@` into a stream URL, a device
names itself after an access token. Such parts are replaced before the text is stored or shown.

The patterns stay narrow on purpose so device names survive:
* the password in `scheme://user:password@host` (the user name stays),
* runs of 32 or more token characters that contain both a letter and a digit (UUIDs in their usual
  8-4-4-4-12 form, pure letters, pure digits and names whose pieces between "_" / "-" are all shorter
  than 16 characters are kept).
"""
from __future__ import annotations

import re

HIDDEN = "[hidden]"
_URL_PASSWORD = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,15}://[^\s/:@]+:)[^\s/@]+(@)")
_UUID = re.compile(r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
_RUN = re.compile(r"[A-Za-z0-9_\-+/]{32,}={0,2}")   # "=" only as base64 padding, so "key=..." keeps its name


def _token(m: re.Match) -> str:
    run = m.group(0)
    if _UUID.match(run) or not (re.search(r"[A-Za-z]", run) and re.search(r"\d", run)):
        return run
    if max(len(piece) for piece in re.split(r"[_-]", run.rstrip("="))) < 16:
        return run               # a long name made of words, e.g. kitchen_light_2024_ceiling_lamp
    return HIDDEN


def scrub(text):
    """Text with secret-looking parts replaced by HIDDEN (None and "" pass through)."""
    if not text:
        return text
    text = _URL_PASSWORD.sub(lambda m: m.group(1) + HIDDEN + m.group(2), str(text))
    return _RUN.sub(_token, text)
