"""Provider/host classification for the downloader.

Kept Qt-free (stdlib only) so it can be unit-tested headlessly. Hosts that hide
the file behind a Cloudflare/Turnstile challenge need the solver to extract a
direct link; form hosts only need a plain HTTP countdown + form post (see
resolve_form_link); every other host is treated as a plain, direct HTTP download.
"""
import html
import http.cookiejar
import re
import time
import urllib.error
import urllib.parse
import urllib.request

# Hosts handled by the Turnstile/CAPTCHA solver (cf_turnstile). Keep in sync
# with the providers cf_turnstile.py actually supports.
RESOLVER_HOSTS = ("fuckingfast.co", "datanodes.to", "bzzhr.to")

# Solver hosts whose resolved link works without the browser's cookies, so the
# file goes through the direct downloader (resume, multiple connections).
DIRECT_AFTER_SOLVE_HOSTS = ("bzzhr.to",)

# Hosts whose download page is a countdown followed by a form post
# (op=download2) that redirects to the file. No browser needed.
FORM_HOSTS = ("filekeeper.net",)

# Characters Windows refuses in file names, plus ASCII control characters.
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


def _host_matches(link, hosts):
    try:
        # .hostname drops any port/userinfo and is already lower-cased.
        host = urllib.parse.urlparse(link).hostname or ""
    except Exception:
        return False
    if host.startswith("www."):
        host = host[4:]
    return any(host == h or host.endswith("." + h) for h in hosts)


def needs_resolution(link):
    """True if the link's host must go through the Turnstile/CAPTCHA solver."""
    return _host_matches(link, RESOLVER_HOSTS)


def download_resolved_directly(link):
    """True if the solver's resolved link can be fetched by the direct downloader."""
    return _host_matches(link, DIRECT_AFTER_SOLVE_HOSTS)


def filename_from_content_disposition(value):
    """File name from a Content-Disposition header (RFC 6266/5987), or None."""
    if not value:
        return None
    m = re.search(r"filename\*\s*=\s*([\w-]+)'[^']*'([^;]+)", value, re.IGNORECASE)
    if m:
        try:
            raw = urllib.parse.unquote(m.group(2).strip().strip('"'), encoding=m.group(1))
        except LookupError:
            raw = urllib.parse.unquote(m.group(2).strip().strip('"'))
    else:
        m = re.search(r'filename\s*=\s*"([^"]*)"|filename\s*=\s*([^;]+)', value, re.IGNORECASE)
        if not m:
            return None
        raw = (m.group(1) if m.group(1) is not None else m.group(2)).strip()
    name = _INVALID_FILENAME_CHARS.sub("_", raw.replace("\\", "/").split("/")[-1]).strip(" .")
    return name or None


def needs_form_resolution(link):
    """True if the link's host needs resolve_form_link before downloading."""
    return _host_matches(link, FORM_HOSTS)


def filename_from_link(link):
    """Local file name for a download link.

    FuckingFast-style links carry the name in the fragment (``.../id#name.rar``);
    otherwise it is the last path segment, ignoring any query string so signed
    or ``?id=`` style URLs don't produce names Windows can't create.
    """
    link = link.strip()
    if "#" in link:
        raw = link.split("#")[-1]
    else:
        try:
            parsed = urllib.parse.urlparse(link)
            raw = parsed.path.rstrip("/").split("/")[-1] or parsed.hostname or ""
        except Exception:
            raw = link.rstrip("/").split("/")[-1]
    name = _INVALID_FILENAME_CHARS.sub("_", urllib.parse.unquote(raw)).strip(" .")
    return name or "download"


class ResolveError(Exception):
    """A form host page couldn't be turned into a direct download link."""


def parse_countdown(page):
    """The data-* attributes of the page's #download-countdown element, e.g.
    {"countdown": "5", "code": "16moao3mygr6", "has-captcha": "false", ...}."""
    m = re.search(r'<[a-z]+\s[^>]*\bid="download-countdown"[^>]*>', page, re.IGNORECASE)
    if not m:
        return {}
    return {
        name.lower(): html.unescape(value)
        for name, value in re.findall(r'\bdata-([\w-]+)="([^"]*)"', m.group(0))
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # The download post answers with a redirect to the file itself; keep it as
    # an HTTPError so the Location can be read without starting the download.
    def redirect_request(self, *args, **kwargs):
        return None


def resolve_form_link(link, sleep=time.sleep, timeout=30):
    """Direct download URL for a FileKeeper-style link: open the page, wait out
    its countdown, then post the same form its "Free download" button posts
    (op=download2) and return the redirect target. sleep(seconds) does the
    waiting, so callers can make it stop early on cancel."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    headers = {"User-Agent": _BROWSER_UA}

    # The file link sets a file_code cookie and redirects to /download.
    with opener.open(urllib.request.Request(link, headers=headers), timeout=timeout) as r:
        page_url = r.geturl()
        page = r.read().decode("utf-8", "replace")

    info = parse_countdown(page)
    if not info.get("code"):
        raise ResolveError(
            "The file page has no download button. The file may have been removed, "
            "or the page layout changed.")
    if info.get("has-captcha") == "true":
        raise ResolveError("The host is asking for a CAPTCHA for this file, which SilverSpoon can't fill in.")
    if info.get("has-password") == "true":
        raise ResolveError("This file is password-protected.")

    try:
        countdown = int(info.get("countdown") or 0)
    except ValueError:
        countdown = 0
    if countdown > 0:
        sleep(countdown + 1)  # the server checks the wait; one spare second

    form = urllib.parse.urlencode({
        "op": "download2",
        "id": info["code"],
        "rand": info.get("rand", ""),
        "referer": info.get("referer", ""),
        "method_free": info.get("method") or "Free download",
        "down_direct": "1",
    }).encode()
    poster = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), _NoRedirect)
    request = urllib.request.Request(page_url, data=form, headers={**headers, "Referer": page_url})
    try:
        with poster.open(request, timeout=timeout) as r:
            location = None
            status = r.status
    except urllib.error.HTTPError as e:
        location = e.headers.get("Location") if e.code in (301, 302, 303, 307, 308) else None
        status = e.code
    if not location:
        raise ResolveError(
            f"The host didn't return a download link (HTTP {status}). "
            "The file may be unavailable or the download limit reached.")
    return urllib.parse.urljoin(page_url, location)
