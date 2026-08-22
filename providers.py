"""Provider/host classification for the downloader.

Kept Qt-free (stdlib only) so it can be unit-tested headlessly. Hosts that hide
the file behind a Cloudflare/Turnstile challenge need the solver to extract a
direct link; every other host is treated as a plain, direct HTTP download.
"""
import urllib.parse

# Hosts handled by the Turnstile/CAPTCHA solver (cf_turnstile). Keep in sync
# with the providers cf_turnstile.py actually supports.
RESOLVER_HOSTS = ("fuckingfast.co", "datanodes.to")


def needs_resolution(link):
    """True if the link's host must go through the Turnstile/CAPTCHA solver."""
    try:
        host = urllib.parse.urlparse(link).netloc.lower()
    except Exception:
        return False
    if host.startswith("www."):
        host = host[4:]
    return any(host == h or host.endswith("." + h) for h in RESOLVER_HOSTS)
