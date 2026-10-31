"""Headless unit checks for version comparison in update_logic. Run: python test_update_logic.py

Tests version_key and is_newer_version without requiring a GUI or display hardware.
"""
import sys
import types

# update_logic imports PyQt6 at module level. In headless CI (where PyQt6 is
# not installed), provide minimal standard-library stubs so pure logic tests
# can run without external dependencies.
try:
    import PyQt6
    import update_logic as U
except ImportError:
    pyqt6 = types.ModuleType("PyQt6")
    core = types.ModuleType("PyQt6.QtCore")
    widgets = types.ModuleType("PyQt6.QtWidgets")

    class _Dummy:
        def __init__(self, *args, **kwargs):
            pass

    core.QThread = _Dummy
    core.pyqtSignal = lambda *args, **kwargs: None
    core.QTimer = _Dummy
    core.QMetaObject = _Dummy
    core.Qt = _Dummy
    core.Q_ARG = lambda *args, **kwargs: None

    widgets.QDialog = _Dummy
    widgets.QVBoxLayout = _Dummy
    widgets.QLabel = _Dummy
    widgets.QProgressBar = _Dummy
    widgets.QMessageBox = _Dummy

    pyqt6.QtCore = core
    pyqt6.QtWidgets = widgets

    sys.modules["PyQt6"] = pyqt6
    sys.modules["PyQt6.QtCore"] = core
    sys.modules["PyQt6.QtWidgets"] = widgets

    import update_logic as U


def test_version_key_valid_formats():
    assert U.version_key("1.2.3") == (1, 2, 3, False, "")
    assert U.version_key("v1.2.3") == (1, 2, 3, False, "")
    assert U.version_key("V1.2.3") == (1, 2, 3, False, "")
    assert U.version_key("  v1.2.3  \n") == (1, 2, 3, False, "")
    assert U.version_key("v01.02.03") == (1, 2, 3, False, "")
    assert U.version_key("0.0.0") == (0, 0, 0, False, "")


def test_version_key_suffixes():
    assert U.version_key("v1.6.0-beta") == (1, 6, 0, True, "-beta")
    assert U.version_key("v1.6.0-BETA") == (1, 6, 0, True, "-beta")
    assert U.version_key("1.6.0.1") == (1, 6, 0, True, ".1")
    assert U.version_key("v1.6.0rc2") == (1, 6, 0, True, "rc2")
    assert U.version_key("v1.6.0-alpha.1") == (1, 6, 0, True, "-alpha.1")


def test_version_key_rejects_malformed():
    for bad in ("", "   ", "v", "v1", "1.6", "v2.0", "1.a.0", "v..0", "latest", "build-123"):
        assert U.version_key(bad) is None, f"expected None for {bad!r}"


def test_is_newer_version_numeric_increments():
    # Major, minor, patch increments
    assert U.is_newer_version("v2.0.0", "v1.9.9") is True
    assert U.is_newer_version("v1.7.0", "v1.6.9") is True
    assert U.is_newer_version("v1.6.1", "v1.6.0") is True
    # Multi-digit comparison (10 > 9 numerically)
    assert U.is_newer_version("v1.10.0", "v1.9.0") is True


def test_is_newer_version_prefix_tolerance():
    assert U.is_newer_version("v1.6.1", "1.6.0") is True
    assert U.is_newer_version("1.6.1", "v1.6.0") is True
    assert U.is_newer_version("V1.6.1", "v1.6.0") is True
    assert U.is_newer_version("  v1.6.1  ", "1.6.0") is True
    # Identical versions with different prefixes are not newer
    assert U.is_newer_version("v1.6.0", "1.6.0") is False
    assert U.is_newer_version("1.6.0", "v1.6.0") is False


def test_is_newer_version_suffix_rules():
    # Implementation rule: patch + suffix > patch
    assert U.is_newer_version("v1.6.0-beta", "v1.6.0") is True
    assert U.is_newer_version("v1.6.0", "v1.6.0-beta") is False
    # Alphabetical suffix comparison
    assert U.is_newer_version("v1.6.0-rc2", "v1.6.0-rc1") is True
    assert U.is_newer_version("v1.6.0-rc1", "v1.6.0-rc2") is False
    # Case-insensitive suffix comparison
    assert U.is_newer_version("v1.6.0-BETA", "v1.6.0-beta") is False
    assert U.is_newer_version("v1.6.0-beta", "v1.6.0-BETA") is False
    # Patch bump beats any suffix on older patch
    assert U.is_newer_version("v1.6.1", "v1.6.0-beta") is True
    assert U.is_newer_version("v1.6.1-beta", "v1.6.0") is True


def test_is_newer_version_same_or_older():
    assert U.is_newer_version("v1.6.0", "v1.6.0") is False
    assert U.is_newer_version("v1.5.0", "v1.6.0") is False
    assert U.is_newer_version("v1.5.9", "v2.0.0") is False


def test_is_newer_version_handles_malformed_gracefully():
    # If either or both inputs are invalid, must return False without raising
    assert U.is_newer_version("invalid", "v1.6.0") is False
    assert U.is_newer_version("v1.6.1", "invalid") is False
    assert U.is_newer_version("invalid", "invalid") is False
    assert U.is_newer_version("", "v1.6.0") is False
    assert U.is_newer_version("v1.6.0", "") is False
    assert U.is_newer_version("1.6", "1.5") is False


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
