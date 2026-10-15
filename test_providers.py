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


def test_form_hosts_are_routed_separately():
    link = "https://filekeeper.net/16moao3mygr6/Game_--_fitgirl-repacks.site_--_.part01.rar"
    assert P.needs_form_resolution(link) is True
    assert P.needs_form_resolution("https://www.filekeeper.net:443/x") is True
    assert P.needs_resolution(link) is False  # no browser solver for it
    assert P.needs_form_resolution("https://notfilekeeper.net/x") is False
    assert P.filename_from_link(link) == "Game_--_fitgirl-repacks.site_--_.part01.rar"


def test_parse_countdown():
    page = ('<div id="download-countdown" class="w-full"\n data-countdown="5"\n'
            ' data-code="16moao3mygr6" data-rand="" data-method="Free &amp; fast"\n'
            ' data-has-captcha="false">')
    info = P.parse_countdown(page)
    assert info["countdown"] == "5" and info["code"] == "16moao3mygr6"
    assert info["rand"] == "" and info["method"] == "Free & fast"
    assert P.parse_countdown("<html>no countdown</html>") == {}


def _mock_form_host(captcha="false", password="false", code="abc123", answer=302):
    """Local stand-in for FileKeeper: /<code>/<name> sets a cookie and redirects
    to /download; POST op=download2 (with the cookie) redirects to the file."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    posts = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/download"):
                body = (f'<div id="download-countdown" data-countdown="2" data-code="{code}" '
                        f'data-referer="" data-rand="r1" data-method="" '
                        f'data-has-password="{password}" data-has-captcha="{captcha}"></div>').encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(302)
                self.send_header("Set-Cookie", f"file_code={code}; path=/")
                self.send_header("Location", "/download")
                self.end_headers()

        def do_POST(self):
            data = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            posts.append((dict(urllib.parse.parse_qsl(data, keep_blank_values=True)),
                          self.headers.get("Cookie", "")))
            self.send_response(answer)
            if answer == 302:
                self.send_header("Location", "https://tunnel1.dlproxy.uk/download/XYZ?sig=1")
            self.send_header("Content-Length", "0")
            self.end_headers()

    import urllib.parse
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/{code}/Game.part01.rar", posts


def test_resolve_form_link_posts_the_download_form():
    server, link, posts = _mock_form_host()
    waits = []
    try:
        url = P.resolve_form_link(link, sleep=waits.append)
    finally:
        server.shutdown()
    assert url == "https://tunnel1.dlproxy.uk/download/XYZ?sig=1", url
    assert waits == [3]  # countdown 2 + one spare second
    form, cookie = posts[0]
    assert form == {"op": "download2", "id": "abc123", "rand": "r1", "referer": "",
                    "method_free": "Free download", "down_direct": "1"}, form
    assert "file_code=abc123" in cookie, cookie


def test_resolve_form_link_reports_blockers():
    for kwargs, expected in (
        ({"captcha": "true"}, "CAPTCHA"),
        ({"password": "true"}, "password"),
        ({"code": ""}, "no download button"),
        ({"answer": 200}, "didn't return a download link"),
    ):
        server, link, _ = _mock_form_host(**kwargs)
        try:
            P.resolve_form_link(link, sleep=lambda s: None)
        except P.ResolveError as e:
            assert expected in str(e), (kwargs, str(e))
        else:
            raise AssertionError(f"no ResolveError for {kwargs}")
        finally:
            server.shutdown()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
