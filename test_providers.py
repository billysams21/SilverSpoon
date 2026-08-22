"""Headless unit checks for provider/host routing. Run: python test_providers.py"""
import providers as P


def test_resolver_hosts_need_resolution():
    for link in (
        "https://fuckingfast.co/abc123",
        "https://www.fuckingfast.co/abc123#file.rar",
        "https://datanodes.to/xyz",
        "https://cdn.datanodes.to/d/xyz",
        "http://fuckingfast.co/x",
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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
