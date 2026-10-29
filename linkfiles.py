"""Pull download links out of whatever file the user hands SilverSpoon.

Kept Qt-free so it can be unit-tested headlessly. The format is decided from
the file's contents (the extension is only a hint):

  * JDownloader DLC containers (decrypted via dlc.py, needs internet)
  * anything text-like: .txt, .html, .json, .csv, .url, .md, ... -- every
    http(s) link found in the text is returned

Result shape everywhere: [(package_name, [urls])]. package_name is "" when the
file doesn't name its links (plain text); the caller picks a folder name.
"""
import html
import os
import re

import dlc

# Plenty for a link list; stops a stray multi-GB file being read into memory.
MAX_BYTES = 20 * 1024 * 1024

_URL_RE = re.compile(r"""https?://[^\s<>"'`\\^{}|]+""", re.IGNORECASE)
_TRAILING_JUNK = ".,;:!?)]}"
_B64_RE = re.compile(r"^[A-Za-z0-9+/=]+$")

# Container formats SilverSpoon can't open (yet) -- say so instead of guessing.
_UNSUPPORTED_CONTAINERS = {".ccf": "CCF", ".rsdf": "RSDF"}


class LinkFileError(Exception):
    """The file couldn't be read, or holds no links. Message is user-facing."""


def _decode_text(data):
    """Bytes -> str, or None when the data is binary rather than text."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if b"\x00" in data[:8192]:
        return None
    return data.decode("utf-8-sig", errors="replace")


def looks_like_dlc(text, ext=""):
    """A DLC is one long base64 blob (plus an 88-char key request at the end)."""
    squashed = re.sub(r"\s+", "", text)
    if len(squashed) <= 88 or not _B64_RE.match(squashed):
        return False
    # An explicit .dlc is trusted; otherwise demand a clearly blob-shaped file.
    return ext == ".dlc" or len(squashed) >= 200


def extract_urls(text):
    """All http(s) links in `text`, in order, without duplicates."""
    text = html.unescape(text).replace("\\/", "/")
    seen, urls = set(), []
    for raw in _URL_RE.findall(text):
        url = raw.rstrip(_TRAILING_JUNK)
        if len(url) > len("https://") and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def extract_from_file(path, fetch=dlc.fetch_service_reply):
    """Detect the format of `path` and return [(package_name, [urls])].

    Raises LinkFileError, with a message fit to show the user, for anything
    unreadable, unsupported or link-free (DLC problems are wrapped too).
    """
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    if not os.path.isfile(path):
        raise LinkFileError(f"{name}: not a file.")
    if ext in _UNSUPPORTED_CONTAINERS:
        raise LinkFileError(
            f"{name}: {_UNSUPPORTED_CONTAINERS[ext]} containers aren't supported "
            "(DLC and plain text/HTML link lists are).")
    try:
        if os.path.getsize(path) > MAX_BYTES:
            raise LinkFileError(f"{name}: file is too large to be a link list.")
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise LinkFileError(f"{name}: couldn't read the file ({e}).")

    text = _decode_text(data)
    if text is None:
        raise LinkFileError(f"{name}: not a text or DLC file, so no links could be read.")

    if looks_like_dlc(text, ext):
        try:
            packages = dlc.decrypt_dlc_text(text, fetch=fetch)
        except dlc.DLCError as e:
            raise LinkFileError(f"{name}: {e}")
        if not packages:
            raise LinkFileError(f"{name}: no links found in this DLC.")
        return packages

    urls = extract_urls(text)
    if not urls:
        raise LinkFileError(f"{name}: no links found.")
    return [("", urls)]
