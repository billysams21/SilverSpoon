"""Multi-connection (segmented) HTTP downloads.

Kept Qt-free (stdlib only) so it can be unit-tested headlessly. A file is split
into byte ranges fetched in parallel, each written straight into its place in
one file. On Windows that file is made sparse first, so writing at a far offset
doesn't make NTFS zero-fill everything before it. Progress lives in a small
"<file>.segments.json" next to the download, so pause and app restarts resume
where every segment stopped.
"""
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request

STATE_SUFFIX = ".segments.json"
MIN_SEGMENT = 4 * 1024 * 1024  # never split work into pieces smaller than this
CHUNK = 64 * 1024
# Statuses a host uses to say "too many connections"; the download carries on
# with fewer instead of failing.
THROTTLE_CODES = (403, 429, 503)
# The link itself is gone (expired/removed): stop now instead of retrying.
FATAL_CODES = (401, 404, 410)
MAX_WORKER_FAILURES = 5


class RangeNotSupported(Exception):
    """The server ignored a Range request, so the file can't be segmented."""


def state_path(path):
    return path + STATE_SUFFIX


def probe(url, headers=None, timeout=30):
    """(total_size, accepts_ranges) from a 1-byte Range request. total_size is
    0 when the server doesn't say."""
    req = urllib.request.Request(url, headers={**(headers or {}), "Range": "bytes=0-0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        status = getattr(r, "status", 200) or 200
        content_range = r.headers.get("Content-Range") or ""
        m = re.search(r"/(\d+)\s*$", content_range)
        if status == 206 and m:
            return int(m.group(1)), True
        try:
            return int(r.headers.get("Content-Length") or 0), False
        except ValueError:
            return 0, False


def _make_sparse(f):
    """Mark an open file sparse on Windows (no-op elsewhere, or where the
    filesystem can't, e.g. FAT/exFAT). Other OSes leave holes on their own."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.DeviceIoControl.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        k32.DeviceIoControl.restype = wintypes.BOOL
        returned = wintypes.DWORD()
        FSCTL_SET_SPARSE = 0x000900C4
        k32.DeviceIoControl(msvcrt.get_osfhandle(f.fileno()), FSCTL_SET_SPARSE,
                            None, 0, None, 0, ctypes.byref(returned), None)
    except Exception:
        pass


class _Throttle:
    """Shared bytes-per-second budget across a download's connections."""

    def __init__(self, bytes_per_second):
        self.rate = bytes_per_second
        self.lock = threading.Lock()
        self.next_time = time.monotonic()

    def consume(self, n, stop):
        if self.rate <= 0:
            return
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next_time)
            self.next_time = start + n / self.rate
            wait = start - now
        if wait > 0:
            stop.wait(wait)


class _Segment:
    __slots__ = ("start", "end", "pos", "owner")

    def __init__(self, start, end, pos=None):
        self.start, self.end = start, end
        self.pos = start if pos is None else pos
        self.owner = None

    @property
    def remaining(self):
        return max(0, self.end - self.pos)


class SegmentedDownload:
    """Download url into path over up to `connections` parallel Range requests.

    run() blocks until the file is complete (returns True) or should_stop()
    turns true (returns False, with progress saved for the next run). It raises
    if the download can't continue. on_progress(downloaded_bytes) is called a
    few times a second from the thread calling run()."""

    def __init__(self, url, path, total, connections=4, headers=None,
                 should_stop=None, on_progress=None, bytes_per_second=0, timeout=30):
        self.url, self.path, self.total = url, path, total
        self.connections = max(1, int(connections))
        self.headers = dict(headers or {})
        self.should_stop = should_stop or (lambda: False)
        self.on_progress = on_progress or (lambda n: None)
        self.timeout = timeout
        self.throttle = _Throttle(bytes_per_second)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.segments = []
        self.max_active = self.connections
        self.active = 0
        self.errors = []

    # -- state ---------------------------------------------------------------
    def _load_state(self):
        """Segments from a previous run, or a fresh plan. A partial file left by
        a plain single-connection download counts as its first bytes done."""
        size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
        try:
            with open(state_path(self.path), encoding="utf-8") as f:
                data = json.load(f)
            if data.get("total") == self.total and size <= self.total and os.path.exists(self.path):
                self.segments = [_Segment(s, e, p) for s, e, p in data["segments"]]
                return
        except (OSError, ValueError, KeyError, TypeError):
            pass
        done = size if 0 < size <= self.total else 0
        if size > self.total:
            os.remove(self.path)  # stale/different file; start over
        self.segments = [_Segment(0, self.total, done)] if self.total else []

    def _save_state(self):
        with self.lock:
            data = {"total": self.total,
                    "segments": [[s.start, s.end, s.pos] for s in self.segments]}
        tmp = state_path(self.path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, state_path(self.path))

    def downloaded(self):
        with self.lock:
            return sum(s.pos - s.start for s in self.segments)

    # -- planning --------------------------------------------------------------
    def _split(self, seg):
        """Give the second half of seg's remaining bytes to a new segment, or
        None if what's left is too small to be worth another connection."""
        if seg.remaining < 2 * MIN_SEGMENT:
            return None
        mid = seg.pos + seg.remaining // 2
        new = _Segment(mid, seg.end)
        seg.end = mid
        self.segments.append(new)
        return new

    def _claim(self, worker):
        """Next piece of work for a worker: an unowned unfinished segment, or
        half of the busiest one (work stealing). Called with the lock held."""
        if self.active > self.max_active:
            return None
        free = [s for s in self.segments if s.owner is None and s.remaining > 0]
        if free:
            seg = max(free, key=lambda s: s.remaining)
        else:
            busy = [s for s in self.segments if s.remaining > 0]
            seg = self._split(max(busy, key=lambda s: s.remaining)) if busy else None
        if seg is not None:
            seg.owner = worker
        return seg

    # -- workers ---------------------------------------------------------------
    def _fetch(self, seg, worker, fh):
        req = urllib.request.Request(
            self.url, headers={**self.headers, "Range": f"bytes={seg.pos}-{seg.end - 1}"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            status = getattr(r, "status", 200) or 200
            m = re.match(r"bytes (\d+)-", r.headers.get("Content-Range") or "")
            if status != 206 or not m or int(m.group(1)) != seg.pos:
                raise RangeNotSupported(f"server answered {status} to a range request")
            while not self.stop.is_set():
                with self.lock:
                    want = min(CHUNK, seg.remaining)
                if want <= 0:
                    return  # done, or the tail was handed to another worker
                data = r.read(want)
                if not data:
                    if seg.remaining > 0:
                        raise ConnectionError("connection closed before the range was complete")
                    return
                self.throttle.consume(len(data), self.stop)
                with self.lock:
                    # end may have shrunk meanwhile (split); never write past it.
                    n = min(len(data), seg.end - seg.pos)
                    offset = seg.pos
                if n > 0:
                    fh.seek(offset)
                    fh.write(data[:n])
                    with self.lock:
                        seg.pos += n

    def _worker(self, worker):
        failures = 0
        with open(self.path, "r+b") as fh:
            while not self.stop.is_set():
                with self.lock:
                    seg = self._claim(worker)
                    if seg is None:
                        self.active -= 1
                        return
                try:
                    self._fetch(seg, worker, fh)
                    failures = 0
                except Exception as e:
                    code = getattr(e, "code", None)
                    if isinstance(e, RangeNotSupported) or code in FATAL_CODES:
                        with self.lock:
                            seg.owner = None
                            self.errors.append(e)
                            self.active -= 1
                        self.stop.set()
                        return
                    with self.lock:
                        seg.owner = None
                        self.errors.append(e)
                        if code in THROTTLE_CODES and self.max_active > 1 and self.active > 1:
                            # The host wants fewer connections: this worker bows
                            # out (only while another worker is left to finish
                            # its segment; otherwise it retries below).
                            self.max_active -= 1
                            self.active -= 1
                            return
                    failures += 1
                    if failures >= MAX_WORKER_FAILURES:
                        with self.lock:
                            self.active -= 1
                        return
                    self.stop.wait(min(2 ** failures, 15))
                    continue
                with self.lock:
                    if seg.owner == worker:
                        seg.owner = None

    def run(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
        self._load_state()
        # Sparse also for a partial file a single-connection run left behind.
        with open(self.path, "r+b" if os.path.exists(self.path) else "wb") as f:
            _make_sparse(f)
        if not any(s.remaining for s in self.segments):
            return self._finish()

        threads = []
        with self.lock:
            unfinished = sum(1 for s in self.segments if s.remaining)
            count = max(1, min(self.connections,
                               max(unfinished, sum(s.remaining for s in self.segments) // MIN_SEGMENT)))
            # Split evenly up front, so every connection starts with its own
            # range; work stealing then only balances the tail end.
            while sum(1 for s in self.segments if s.remaining) < count:
                if self._split(max(self.segments, key=lambda s: s.remaining)) is None:
                    break
            self.active = count
        for i in range(count):
            t = threading.Thread(target=self._worker, args=(i,), daemon=True)
            t.start()
            threads.append(t)

        last_save = time.monotonic()
        while any(t.is_alive() for t in threads):
            if self.should_stop():
                self.stop.set()
            time.sleep(0.25)
            self.on_progress(self.downloaded())
            if time.monotonic() - last_save > 1:
                self._save_state()
                last_save = time.monotonic()
        for t in threads:
            t.join()
        self.on_progress(self.downloaded())

        if any(s.remaining for s in self.segments):
            self._save_state()
            if self.should_stop():
                return False
            # Every worker gave up: report what went wrong.
            raise self.errors[-1] if self.errors else ConnectionError("download stopped unexpectedly")
        return self._finish()

    def _finish(self):
        try:
            os.remove(state_path(self.path))
        except OSError:
            pass
        if os.path.getsize(self.path) != self.total:
            raise IOError(f"downloaded file is {os.path.getsize(self.path)} bytes, expected {self.total}")
        return True
