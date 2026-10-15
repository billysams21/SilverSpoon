"""Headless unit checks for provider/host routing. Run: python test_providers.py"""
import providers as P


def test_resolver_hosts_need_resolution():
    for link in (
        "https://fuckingfast.co/abc123",
        "https://www.fuckingfast.co/abc123#file.rar",
        "https://datanodes.to/xyz",
        "https://cdn.datanodes.to/d/xyz",
        "http://fuckingfast.co/x",
        "https://fuckingfast.co:443/abc123",
        "https://user@datanodes.to/xyz",
        "https://FuckingFast.co/abc123",
    ):
        assert P.needs_resolution(link) is True, link


def test_general_hosts_are_direct():
    for link in (
        "https://example.com/file.zip",
        "https://github.com/o/r/releases/download/v1/x.zip",
        "https://objects.githubusercontent.com/x/SilverSpoon.zip",
        "https://notfuckingfast.co.evil.com/x",
        "https://mydatanodes.to.example.com/x",
    ):
        assert P.needs_resolution(link) is False, link


def test_malformed_never_crashes():
    for bad in ("", "not a url", "ftp://", "://///", "javascript:void"):
        assert P.needs_resolution(bad) is False, bad


def test_filename_from_link():
    for link, expected in (
        ("https://fuckingfast.co/abc123#Game_--_fitgirl-repacks.site_--_.part01.rar",
         "Game_--_fitgirl-repacks.site_--_.part01.rar"),
        ("https://github.com/o/r/releases/download/v1/SilverSpoon-windows.zip",
         "SilverSpoon-windows.zip"),
        ("https://cdn.example.com/files/My%20Game.zip?sig=abc&exp=1", "My Game.zip"),
        ("https://example.com/get?id=5", "get"),
        ("https://example.com/files/", "files"),
        ("https://example.com/", "example.com"),
        ("https://example.com/a%3Ab%2Fc.zip", "a_b_c.zip"),
    ):
        assert P.filename_from_link(link) == expected, (link, P.filename_from_link(link))


def test_filename_never_empty_or_invalid():
    for bad in ("", "https://", "https://x/%3F", "https://x/..."):
        name = P.filename_from_link(bad)
        assert name and not any(c in name for c in '<>:"/\\|?*'), (bad, name)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
