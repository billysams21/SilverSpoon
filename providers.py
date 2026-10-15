"""Provider/host classification for the downloader.

Kept Qt-free (stdlib only) so it can be unit-tested headlessly. Hosts that hide
the file behind a Cloudflare/Turnstile challenge need the solver to extract a
direct link; every other host is treated as a plain, direct HTTP download.
"""
import re
import urllib.parse

# Hosts handled by the Turnstile/CAPTCHA solver (cf_turnstile). Keep in sync
# with the providers cf_turnstile.py actually supports.
RESOLVER_HOSTS = ("fuckingfast.co", "datanodes.to")

# Characters Windows refuses in file names, plus ASCII control characters.
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def needs_resolution(link):
    """True if the link's host must go through the Turnstile/CAPTCHA solver."""
    try:
        # .hostname drops any port/userinfo and is already lower-cased.
        host = urllib.parse.urlparse(link).hostname or ""
    except Exception:
        return False
    if host.startswith("www."):
        host = host[4:]
    return any(host == h or host.endswith("." + h) for h in RESOLVER_HOSTS)


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
