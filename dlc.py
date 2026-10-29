"""JDownloader DLC container support.

Kept Qt-free (stdlib + `cryptography`) so it can be unit-tested headlessly.

A .dlc file is base64 text: the last 88 characters are a key request, the rest
is the AES-encrypted link list. The request is sent to JDownloader's public
DLC service, which answers with the real AES key (itself encrypted with a
well-known static key). So opening a DLC needs an internet connection.

The decrypted payload is XML: <package name="b64"><file><url>b64</url></file>
</package>, with every name and URL base64-encoded.
"""
import base64
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# Static key/IV that protect the service's reply (public, used by every DLC client).
_SERVICE_KEY = b"cb99b5cbc24db398"
_SERVICE_IV = b"9bc24cb995cb8db3"

_SERVICE_URLS = (
    "http://service.jdownloader.org/dlcrypt/service.php?srcType=dlc&destType=pylo&data=",
    "https://service.jdownloader.org/dlcrypt/service.php?srcType=dlc&destType=pylo&data=",
)
_RC_LEN = 88
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"


_BAD_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_folder_name(name, fallback="DLC Import"):
    """Package name -> a folder name every OS accepts."""
    name = _BAD_NAME_CHARS.sub("_", name or "").strip(" .")
    return name or fallback


class DLCError(Exception):
    """The file isn't a usable DLC (or its key couldn't be fetched)."""


def _aes_cbc_decrypt(key, iv, data):
    # Callers hand in whole 16-byte blocks; drop any ragged tail rather than fail.
    data = data[: len(data) - (len(data) % 16)]
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return dec.update(data) + dec.finalize()


def _strip_padding(raw):
    # Base64 text only has bytes >= 0x2B, so trailing control bytes are padding.
    return raw.rstrip(bytes(range(0x20)))


def fetch_service_reply(rc, timeout=20):
    """Ask JDownloader's DLC service to unlock `rc` (the file's last 88 chars)."""
    last_err = None
    for base in _SERVICE_URLS:
        url = base + urllib.parse.quote(rc, safe="")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as e:
            last_err = e
    raise DLCError(f"Could not reach the DLC key service: {last_err}")


def _key_from_reply(reply):
    m = re.search(r"<rc>(.+?)</rc>", reply, re.S)
    if not m:
        raise DLCError("The DLC key service rejected this file.")
    try:
        enc_key = base64.b64decode(m.group(1).strip())[:16]
    except Exception:
        raise DLCError("The DLC key service sent an unreadable reply.")
    if len(enc_key) != 16:
        raise DLCError("The DLC key service sent an unreadable reply.")
    return _aes_cbc_decrypt(_SERVICE_KEY, _SERVICE_IV, enc_key)


def _b64(text):
    try:
        return base64.b64decode((text or "").strip()).decode("utf-8", "replace").strip()
    except Exception:
        return ""


def parse_payload(xml_bytes):
    """Decrypted XML -> [(package_name, [urls])]. Empty packages are dropped."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        raise DLCError("Decryption produced invalid data (corrupt DLC?).")
    content = root if root.tag == "content" else root.find("content")
    if content is None:
        raise DLCError("No link list found in this DLC.")
    packages = []
    for pkg in content.iter("package"):
        urls = [u for u in (_b64(f.findtext("url")) for f in pkg.iter("file")) if u]
        if urls:
            packages.append((_b64(pkg.get("name")), urls))
    return packages


def decrypt_dlc_text(text, fetch=fetch_service_reply):
    """Decrypt DLC file text. `fetch` is injectable for offline tests."""
    text = re.sub(r"\s+", "", text or "")
    if len(text) <= _RC_LEN:
        raise DLCError("This doesn't look like a DLC file.")
    data_b64, rc = text[:-_RC_LEN], text[-_RC_LEN:]
    try:
        data = base64.b64decode(data_b64)
    except Exception:
        raise DLCError("This doesn't look like a DLC file.")
    key = _key_from_reply(fetch(rc))
    # The AES key doubles as the IV.
    inner = _strip_padding(_aes_cbc_decrypt(key, key, data))
    try:
        xml_bytes = base64.b64decode(inner)
    except Exception:
        raise DLCError("Decryption failed (corrupt DLC?).")
    return parse_payload(xml_bytes)


def read_dlc(path, fetch=fetch_service_reply):
    """Read a .dlc file -> [(package_name, [urls])]."""
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return decrypt_dlc_text(f.read(), fetch=fetch)
