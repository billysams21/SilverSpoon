"""Helper for the optional "Auto solve CAPTCHAs" browser extension.

The Chrome Web Store extension is downloaded as a ``.crx`` from Google's
update service, saved next to the application as ``auto_solver.crx`` and then
unpacked into ``auto_solver/`` using the :mod:`crx_unpack` package. The
unpacked directory is later handed to Chromium via ``--load-extension``.
"""

import logging
import os
import shutil
import struct
import sys
import tempfile
import threading
import urllib.request

logger = logging.getLogger(__name__)

# Serializes downloads so a startup prefetch and a solver launch never fetch
# the same CRX at the same time.
_ensure_lock = threading.Lock()

# The extension id from https://chromewebstore.google.com/detail/captcha-plugin-turnstile/ijmhjekchmipnifjiahkapifokdffffo
EXTENSION_ID = "ijmhjekchmipnifjiahkapifokdffffo"

CRX_URL = (
    "https://clients2.google.com/service/update2/crx?response=redirect"
    "&prod=chromium&prodversion=143.0.7499.41&acceptformat=crx3"
    "&x=id%3D{extension_id}%26installsource%3Dondemand%26uc"
)

CRX_FILENAME = "auto_solver.crx"
EXTENSION_DIRNAME = "auto_solver"

_CRX_MAGIC = b"Cr24"


def app_directory():
    """Directory the running application (and its runtime files) live in."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def crx_path(app_dir=None):
    return os.path.join(app_dir or app_directory(), CRX_FILENAME)


def extension_dir(app_dir=None):
    return os.path.join(app_dir or app_directory(), EXTENSION_DIRNAME)


def download_crx(dest_path, timeout=120):
    """Download the extension CRX to ``dest_path`` and return that path."""
    url = CRX_URL.format(extension_id=EXTENSION_ID)
    logger.info("AutoSolver: downloading extension from %s", url)
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response, \
            open(dest_path, "wb") as out_file:
        shutil.copyfileobj(response, out_file)
    logger.info("AutoSolver: saved CRX to %s", dest_path)
    return dest_path


def unpack_crx(crx_file, ext_dir, overwrite=True):
    """Unpack a CRX2 or CRX3 archive into ``ext_dir`` using ``crx_unpack``.

    ``crx_unpack`` only understands the legacy CRX2 header. Chrome's update
    service now returns CRX3, whose payload is still a plain ZIP behind a
    protobuf header, so we locate the embedded ZIP and let ``crx_unpack``
    perform the actual extraction.
    """
    import crx_unpack  # Imported lazily so the app runs without it.

    if os.path.isdir(ext_dir):
        if not overwrite:
            return ext_dir
        shutil.rmtree(ext_dir, ignore_errors=True)

    with open(crx_file, "rb") as fin:
        magic = fin.read(4)
        if magic != _CRX_MAGIC:
            raise ValueError("Not a CRX file (bad magic): %s" % crx_file)
        raw_version = fin.read(4)
        if len(raw_version) < 4:
            raise ValueError("Truncated CRX header: %s" % crx_file)
        version = struct.unpack("<I", raw_version)[0]

        if version == 2:
            # Legacy format: crx_unpack handles the header itself.
            return crx_unpack.unpack(
                crx_file, ext_dir, overwrite_if_exists=overwrite
            )

        if version == 3:
            raw_header_len = fin.read(4)
            if len(raw_header_len) < 4:
                raise ValueError("Truncated CRX3 header: %s" % crx_file)
            header_len = struct.unpack("<I", raw_header_len)[0]
            # Header layout: magic (4) + version (4) + header_length (4)
            # followed by header_len bytes of protobuf, then the ZIP payload.
            fin.seek(12 + header_len)
            zip_bytes = fin.read()

            tmp_fd, tmp_zip = tempfile.mkstemp(suffix=".zip")
            try:
                with os.fdopen(tmp_fd, "wb") as zip_out:
                    zip_out.write(zip_bytes)
                crx_unpack.extract_zip(tmp_zip, ext_dir)
            finally:
                try:
                    os.remove(tmp_zip)
                except OSError:
                    pass
            return ext_dir

    raise ValueError("Unsupported CRX version: %s" % version)


def is_extension_ready(ext_dir):
    """True when ``ext_dir`` looks like a usable, unpacked extension."""
    return os.path.isfile(os.path.join(ext_dir, "manifest.json"))


def ensure_extension(app_dir=None, force=False):
    """Download and unpack the extension, returning its directory.

    Returns ``None`` if the extension could not be prepared (for example when
    the machine is offline). A previously unpacked copy is reused unless
    ``force`` is set, so the (large) CRX is only fetched when needed.
    """
    base = app_dir or app_directory()
    ext = extension_dir(base)

    with _ensure_lock:
        # Re-check inside the lock: another caller may have just prepared it.
        if not force and is_extension_ready(ext):
            logger.info("AutoSolver: using existing extension at %s", ext)
            return ext

        crx = crx_path(base)
        try:
            download_crx(crx)
            unpack_crx(crx, ext, overwrite=True)
        except Exception as exc:  # noqa: BLE001 - best effort, solver can run without it
            logger.error("AutoSolver: failed to prepare extension: %s", exc)
            return None

        if is_extension_ready(ext):
            logger.info("AutoSolver: extension ready at %s", ext)
            return ext
        logger.error("AutoSolver: unpacked extension is missing manifest.json")
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = ensure_extension(force="--force" in sys.argv)
    print(result if result else "extension unavailable")
