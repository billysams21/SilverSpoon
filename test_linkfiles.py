"""Headless checks for drop-any-file link extraction. Run: python test_linkfiles.py"""
import os
import tempfile

import linkfiles as L
import test_dlc as T


def _write(d, name, data):
    path = os.path.join(d, name)
    with open(path, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode("utf-8"))
    return path


def test_plain_text_list():
    with tempfile.TemporaryDirectory() as d:
        p = _write(d, "links.txt",
                   "- https://fuckingfast.co/a#g.part1.rar\r\nhttps://fuckingfast.co/b#g.part2.rar\n"
                   "junk line\nhttps://fuckingfast.co/a#g.part1.rar\n")
        assert L.extract_from_file(p) == [("", [
            "https://fuckingfast.co/a#g.part1.rar", "https://fuckingfast.co/b#g.part2.rar"])]


def test_html_entities_and_punctuation():
    with tempfile.TemporaryDirectory() as d:
        p = _write(d, "page.html",
                   '<a href="https://x.com/f?a=1&amp;b=2">dl</a> see (https://y.com/z.zip), ok.')
        assert L.extract_from_file(p)[0][1] == ["https://x.com/f?a=1&b=2", "https://y.com/z.zip"]


def test_json_escaped_slashes_and_bom_and_utf16():
    with tempfile.TemporaryDirectory() as d:
        p = _write(d, "a.json", '{"u": "https:\\/\\/x.com\\/a.rar"}')
        assert L.extract_from_file(p)[0][1] == ["https://x.com/a.rar"]
        p = _write(d, "b.txt", b"\xef\xbb\xbfhttps://x.com/b.rar")
        assert L.extract_from_file(p)[0][1] == ["https://x.com/b.rar"]
        p = _write(d, "c.txt", "https://x.com/c.rar".encode("utf-16"))
        assert L.extract_from_file(p)[0][1] == ["https://x.com/c.rar"]


def test_dlc_detected_by_content_not_extension():
    text = T._make_dlc([("Game", ["https://example.com/a.rar"])])
    with tempfile.TemporaryDirectory() as d:
        for name in ("c.dlc", "renamed.txt", "noext"):
            p = _write(d, name, text)
            got = L.extract_from_file(p, fetch=T._fake_fetch)
            assert got == [("Game", ["https://example.com/a.rar"])], (name, got)


def test_errors_are_user_facing():
    with tempfile.TemporaryDirectory() as d:
        cases = {
            "empty.txt": b"",
            "nolinks.txt": b"just some words",
            "bin.zip": b"PK\x03\x04\x00\x00\x00binary",
            "old.rsdf": b"abc",
        }
        for name, data in cases.items():
            p = _write(d, name, data)
            try:
                L.extract_from_file(p)
            except L.LinkFileError as e:
                assert name in str(e), str(e)
            else:
                raise AssertionError(f"no LinkFileError for {name}")
        try:
            L.extract_from_file(d)
        except L.LinkFileError:
            pass
        else:
            raise AssertionError("directory accepted")


def test_broken_dlc_reports_error():
    with tempfile.TemporaryDirectory() as d:
        p = _write(d, "bad.dlc", "A" * 300)
        try:
            L.extract_from_file(p, fetch=lambda rc: "<error/>")
        except L.LinkFileError as e:
            assert "bad.dlc" in str(e)
        else:
            raise AssertionError("no LinkFileError")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
