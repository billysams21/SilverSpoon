"""Headless checks for DLC container decryption. Run: python test_dlc.py

Builds a DLC the way JDownloader does and answers the key request with a fake
service, so no network is needed.
"""
import base64
import os
import tempfile

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import dlc as D

REAL_KEY = b"0123456789abcdef"


def _aes_enc(key, iv, data):
    pad = 16 - len(data) % 16
    data += bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(data) + enc.finalize()


def _b64(s):
    return base64.b64encode(s.encode()).decode()


def _make_dlc(packages):
    files = "".join(
        f'<package name="{_b64(name)}">'
        + "".join(f"<file><url>{_b64(u)}</url><filename>{_b64('x')}</filename></file>" for u in urls)
        + "</package>"
        for name, urls in packages
    )
    xml = f"<dlc><header/><content>{files}</content></dlc>".encode()
    body = base64.b64encode(_aes_enc(REAL_KEY, REAL_KEY, base64.b64encode(xml))).decode()
    rc = "A" * 88
    return body + rc


def _fake_fetch(rc):
    enc = _aes_enc(D._SERVICE_KEY, D._SERVICE_IV, REAL_KEY)[:16]
    return f"<rc>{base64.b64encode(enc).decode()}</rc>"


def test_roundtrip_multiple_packages():
    text = _make_dlc([
        ("Game", ["https://fuckingfast.co/a#g.part1.rar", "https://fuckingfast.co/b#g.part2.rar"]),
        ("Extras", ["https://example.com/c.zip"]),
    ])
    pk = D.decrypt_dlc_text(text, fetch=_fake_fetch)
    assert pk == [
        ("Game", ["https://fuckingfast.co/a#g.part1.rar", "https://fuckingfast.co/b#g.part2.rar"]),
        ("Extras", ["https://example.com/c.zip"]),
    ], pk


def test_whitespace_and_file_read():
    text = _make_dlc([("P", ["https://example.com/x"])])
    wrapped = "\n".join(text[i:i + 60] for i in range(0, len(text), 60)) + "\n"
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.dlc")
        with open(path, "w") as f:
            f.write(wrapped)
        assert D.read_dlc(path, fetch=_fake_fetch) == [("P", ["https://example.com/x"])]


def test_bad_files_raise():
    for bad in ("", "hello", "!!!" * 50):
        try:
            D.decrypt_dlc_text(bad, fetch=_fake_fetch)
        except D.DLCError:
            pass
        else:
            raise AssertionError(f"no DLCError for {bad!r}")


def test_service_rejection_raises():
    text = _make_dlc([("P", ["https://example.com/x"])])
    try:
        D.decrypt_dlc_text(text, fetch=lambda rc: "<error/>")
    except D.DLCError as e:
        assert "rejected" in str(e)
    else:
        raise AssertionError("no DLCError")


def test_safe_folder_name():
    assert D.safe_folder_name('a/b:c?') == 'a_b_c_'
    assert D.safe_folder_name('  ..  ') == 'DLC Import'
    assert D.safe_folder_name('') == 'DLC Import'


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
