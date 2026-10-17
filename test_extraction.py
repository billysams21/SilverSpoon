"""Headless checks for the extraction helpers. Run: python test_extraction.py

The listing/progress tests use the bundled 7z.exe on Windows (a 7-Zip on PATH
elsewhere) and are skipped when none is available.
"""
import os
import shutil
import subprocess
import sys
import tempfile

import extraction as X

HERE = os.path.dirname(os.path.abspath(__file__))
# The bundled 7z.exe only runs on Windows (on Linux CI it's "Permission denied").
_CANDIDATES = ([os.path.join(HERE, "7z.exe")] if sys.platform == "win32" else []) + [
    shutil.which(name) for name in ("7z", "7zz", "7za")]
SEVEN_ZIP = next(
    (p for p in _CANDIDATES if p and os.path.isfile(p) and os.access(p, os.X_OK)),
    None,
)

LISTING = """Path = sub
Size = 0
Attributes = D

Path = fg-01.bin
Size = 1200
Attributes = A

Path = sub\\setup.bin
Size = 34
Folder = -

Path = folder-only
Folder = +
"""


def test_parse_listing_skips_folders():
    assert X.parse_listing(LISTING) == [("fg-01.bin", 1200), ("sub\\setup.bin", 34)]
    assert X.parse_listing(LISTING.replace("\n", "\r\n")) == X.parse_listing(LISTING)
    assert X.parse_listing("") == []


def test_is_extracted_compares_sizes():
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "sub"))
        entries = [("a.bin", 5), (os.path.join("sub", "b.bin"), 3)]
        assert X.is_extracted(entries, d) is False  # nothing there yet
        with open(os.path.join(d, "a.bin"), "wb") as f:
            f.write(b"12345")
        with open(os.path.join(d, "sub", "b.bin"), "wb") as f:
            f.write(b"12")
        assert X.is_extracted(entries, d) is False  # b.bin is short (interrupted)
        with open(os.path.join(d, "sub", "b.bin"), "wb") as f:
            f.write(b"123")
        assert X.is_extracted(entries, d) is True
        assert X.is_extracted([], d) is False


def test_list_extract_and_detect_with_7z():
    if not SEVEN_ZIP:
        print("    (skipped: no 7z available)")
        return
    with tempfile.TemporaryDirectory() as d:
        src, out = os.path.join(d, "src"), os.path.join(d, "out")
        os.makedirs(os.path.join(src, "sub"))
        with open(os.path.join(src, "big.bin"), "wb") as f:
            f.write(os.urandom(3_000_000))
        with open(os.path.join(src, "sub", "small.txt"), "wb") as f:
            f.write(b"hello")
        # Multi-volume, like a split repack (Game.7z.001, .002, ...).
        subprocess.run([SEVEN_ZIP, "a", "-mx0", "-v1m", os.path.join(d, "Game.7z"), "."],
                       cwd=src, check=True, capture_output=True)
        first = os.path.join(d, "Game.7z.001")

        entries = X.list_archive(SEVEN_ZIP, first)
        assert sorted(entries) == sorted([("big.bin", 3_000_000), (os.path.join("sub", "small.txt"), 5)]), entries
        assert X.list_archive(SEVEN_ZIP, os.path.join(src, "big.bin")) is None  # not an archive
        assert X.is_extracted(entries, out) is False

        seen = []
        code, _ = X.run_with_progress(
            [SEVEN_ZIP, "x", first, f"-o{out}", "-y", "-bsp1", "-bso0"], seen.append)
        assert code == 0
        assert all(0 <= p <= 100 for p in seen), seen
        assert X.is_extracted(entries, out) is True

        os.remove(os.path.join(out, "sub", "small.txt"))
        assert X.is_extracted(entries, out) is False  # a file went missing -> re-extract


def test_run_with_progress_can_be_cancelled():
    if not SEVEN_ZIP:
        print("    (skipped: no 7z available)")
        return
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "big.bin")
        with open(src, "wb") as f:
            f.write(os.urandom(40_000_000))
        archive = os.path.join(d, "Game.7z")
        subprocess.run([SEVEN_ZIP, "a", "-mx0", archive, src], check=True, capture_output=True)
        out = os.path.join(d, "out")
        try:
            X.run_with_progress([SEVEN_ZIP, "x", archive, f"-o{out}", "-y", "-bsp1", "-bso0"],
                                lambda p: None, should_stop=lambda: True)
        except X.ExtractionCancelled:
            pass
        else:
            raise AssertionError("extraction was not cancelled")
        assert not X.is_extracted([("big.bin", 40_000_000)], out)


def test_run_with_progress_reports_failure():
    if not SEVEN_ZIP:
        print("    (skipped: no 7z available)")
        return
    code, output = X.run_with_progress([SEVEN_ZIP, "x", "does-not-exist.7z", "-y", "-bsp1", "-bso0"],
                                       lambda p: None)
    assert code != 0 and output.strip(), (code, output)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
