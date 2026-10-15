"""Archive helpers for the auto-extractor.

Kept Qt-free (stdlib only) so it can be unit-tested headlessly. Listing uses
7-Zip's technical listing (-slt), which reads only the archive headers, so it
is quick even for multi-GB multipart RARs.
"""
import os
import re
import subprocess
import sys
import threading
import time

# Hide the console window the extractor would otherwise open on Windows.
_CREATIONFLAGS = 0x08000000 if sys.platform == "win32" else 0
_PERCENT_RE = re.compile(rb"(\d{1,3})%")


class ExtractionCancelled(Exception):
    """The extractor was stopped because the user cancelled."""


def list_archive(seven_zip, archive):
    """[(relative_path, size)] for every file in the archive, or None if 7-Zip
    can't read it (not an archive, missing volume, password, ...)."""
    try:
        result = subprocess.run(
            [seven_zip, "l", "-slt", "-ba", "-sccUTF-8", archive],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            creationflags=_CREATIONFLAGS,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return parse_listing(result.stdout.decode("utf-8", "replace"))


def parse_listing(text):
    """Files (not folders) from `7z l -slt -ba` output, as [(path, size)]."""
    entries = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        fields = dict(
            line.split(" = ", 1) for line in block.split("\n") if " = " in line
        )
        path = fields.get("Path")
        if not path:
            continue
        if fields.get("Folder") == "+" or fields.get("Attributes", "").startswith("D"):
            continue
        try:
            size = int(fields.get("Size") or 0)
        except ValueError:
            size = 0
        entries.append((path, size))
    return entries


def is_extracted(entries, dest_dir):
    """True if every file in the listing already exists in dest_dir with the
    same size. A partial or interrupted extraction therefore doesn't count."""
    if not entries:
        return False
    for path, size in entries:
        try:
            if os.path.getsize(os.path.join(dest_dir, path)) != size:
                return False
        except OSError:
            return False
    return True


def run_with_progress(cmd, on_progress, should_stop=None):
    """Run an extractor, calling on_progress(percent) whenever its output
    reports one (7-Zip with -bsp1, unrar). Returns (returncode, output tail).

    If should_stop() turns true the extractor is killed (checked every 0.2 s,
    so it works even while the extractor prints nothing) and
    ExtractionCancelled is raised."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=_CREATIONFLAGS,
    )
    stopped = threading.Event()
    if should_stop:
        def watch():
            while proc.poll() is None:
                if should_stop():
                    stopped.set()
                    proc.kill()
                    return
                time.sleep(0.2)
        threading.Thread(target=watch, daemon=True).start()
    tail = b""
    while True:
        chunk = proc.stdout.read1(4096)
        if not chunk:
            break
        tail = (tail + chunk)[-2000:]
        found = _PERCENT_RE.findall(chunk)
        if found:
            on_progress(min(100, int(found[-1])))
    returncode = proc.wait()
    if stopped.is_set():
        raise ExtractionCancelled()
    return returncode, tail.decode("utf-8", "replace")
