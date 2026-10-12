"""Bounded first-party Cookie persistence; no network or credential diagnostics.

One bot process owns the store. Windows files use DPAPI CurrentUser; other
platforms use private files. The environment remains the explicit opt-in seed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
import hashlib
from http.cookies import SimpleCookie
import json
import logging
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.parse import urlparse
import uuid

logger = logging.getLogger("discord_digest_bot")
HOSTS = {"threads": "www.threads.com", "facebook": "www.facebook.com", "instagram": "www.instagram.com"}
LOCKS = {platform: threading.RLock() for platform in HOSTS}
MAX_HEADER = 16 * 1024
MAX_FILE = 128 * 1024
MAX_COOKIES = 128
NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
DEFAULT_DIRECTORY = Path(__file__).resolve().parents[3] / "secrets" / "social-preview-cookies"


def _dpapi(data: bytes, *, decrypt: bool = False) -> bytes:
    """No LOCAL_MACHINE scope and no UI; free Windows-allocated output buffers."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.c_void_p)]

    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.c_void_p))
    output = Blob()
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise OSError("cookie_crypto_failed")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


def _encode(state: dict) -> bytes:
    data = json.dumps(state, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return b"DPAPI1\n" + _dpapi(data) if os.name == "nt" else b"PRIVATE1\n" + data


def _decode(data: bytes) -> dict:
    if os.name == "nt" and data.startswith(b"DPAPI1\n"):
        data = _dpapi(data[7:], decrypt=True)
    elif os.name != "nt" and data.startswith(b"PRIVATE1\n"):
        data = data[9:]
    else:
        raise ValueError("cookie_store_format")
    return json.loads(data)


def _ascii(value: str) -> bool:
    return isinstance(value, str) and all(32 <= ord(c) < 127 for c in value)


def _validate(state: dict, platform: str) -> dict:
    if (not isinstance(state, dict) or state.get("version") != 1 or state.get("platform") != platform
            or not isinstance(state.get("seed_hash"), str) or len(state["seed_hash"]) != 64
            or not isinstance(state.get("revision"), str)):
        raise ValueError("cookie_store_format")
    entries = state.get("cookies")
    if not isinstance(entries, list) or len(entries) > MAX_COOKIES:
        raise ValueError("cookie_store_limit")
    host = HOSTS[platform]
    for entry in entries:
        if (not isinstance(entry, dict) or not isinstance(entry.get("name"), str)
                or not NAME.fullmatch(entry["name"]) or not _ascii(entry.get("value"))
                or ";" in entry["value"] or entry.get("domain") not in (host, host[4:])
                or not _ascii(entry.get("path")) or not entry["path"].startswith("/")
                or type(entry.get("seed")) is not bool):
            raise ValueError("cookie_store_format")
        expiry = entry.get("expires")
        if expiry is not None and (type(expiry) not in (int, float) or not math.isfinite(expiry)):
            raise ValueError("cookie_store_format")
    return state


def _read(path: Path, platform: str) -> dict | None:
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_FILE + 1)
    except FileNotFoundError:
        return None
    if len(data) > MAX_FILE:
        raise ValueError("cookie_store_limit")
    return _validate(_decode(data), platform)


def _write(path: Path, state: dict) -> None:
    data = _encode(state)
    if len(data) > MAX_FILE:
        raise ValueError("cookie_store_limit")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".cookie-", delete=False) as stream:
            temporary = Path(stream.name)
            if os.name != "nt":
                os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _seed_state(platform: str, seed: str, digest: str) -> dict:
    entries = {}
    for pair in seed.split(";"):
        name, separator, value = pair.strip().partition("=")
        if not separator or not NAME.fullmatch(name) or not _ascii(value):
            raise ValueError("cookie_config_invalid")
        entries[name] = dict(name=name, value=value, domain=HOSTS[platform], path="/", expires=None, seed=True)
    return _validate(dict(version=1, platform=platform, seed_hash=digest,
                          revision=uuid.uuid4().hex, cookies=list(entries.values())), platform)


def _header(state: dict, url: str, now: float) -> str:
    path = urlparse(url).path or "/"
    entries = [entry for entry in state["cookies"] if
               (entry["expires"] is None or entry["expires"] > now) and
               (path == entry["path"] or path.startswith(entry["path"].rstrip("/") + "/"))]
    entries.sort(key=lambda entry: len(entry["path"]), reverse=True)
    result = "; ".join(entry["name"] + "=" + entry["value"] for entry in entries)
    if len(result) > MAX_HEADER:
        raise ValueError("cookie_header_limit")
    return result


@dataclass(repr=False)
class CookieSnapshot:
    platform: str
    url: str = field(repr=False)
    header: str = field(repr=False)
    state: dict | None = field(default=None, repr=False)
    path: Path | None = field(default=None, repr=False)
    revision: str | None = field(default=None, repr=False)


def prepare_cookie(platform: str, url: str, seed: str, *, persist: bool = True) -> CookieSnapshot:
    """No seed means no authentication, even when a previous cache exists."""
    host = HOSTS[platform]
    target = urlparse(url)
    if target.scheme != "https" or target.netloc != host:
        raise ValueError("cookie_target_invalid")
    seed = seed.strip()
    snapshot = CookieSnapshot(platform, url, seed)
    if not seed or not persist or os.environ.get("SOCIAL_PREVIEW_COOKIE_AUTO_SAVE", "1").strip().lower() in ("0", "false", "off", "no"):
        return snapshot
    if len(seed) > MAX_HEADER or not _ascii(seed):
        raise ValueError("cookie_config_invalid")
    digest = hashlib.sha256(seed.encode("ascii")).hexdigest()
    directory = os.environ.get("SOCIAL_PREVIEW_COOKIE_STORE_DIR")
    path = (Path(directory).expanduser() if directory else DEFAULT_DIRECTORY) / (platform + ".cookies")
    try:
        with LOCKS[platform]:
            state = _read(path, platform)
            if state is None or state["seed_hash"] != digest:
                state = _seed_state(platform, seed, digest)
                # Establish a generation before dispatch, including seed changes.
                # This lets a late response from the old generation be ignored.
                _write(path, state)
            snapshot.header = _header(state, url, time.time())
            snapshot.state, snapshot.path, snapshot.revision = state, path, state["revision"]
    except Exception:
        # Keep the configured seed usable; never overwrite an unreadable cache.
        logger.warning("%s cookie persistence unavailable; using configured seed", platform)
    return snapshot


def response_cookies(headers) -> list[str]:
    """Pass requests raw.headers or aiohttp response.headers, preserving repeats."""
    for method in ("getlist", "getall"):
        getter = getattr(headers, method, None)
        if callable(getter):
            try:
                values = getter("Set-Cookie", []) if method == "getall" else getter("Set-Cookie")
            except (KeyError, TypeError):
                values = []
            if isinstance(values, (list, tuple)):
                return [value for value in values[:MAX_COOKIES] if isinstance(value, str)]
    value = headers.get("Set-Cookie", "")
    return [value] if isinstance(value, str) and value else []


def _merge(state: dict, url: str, headers: list[str], now: float) -> dict:
    host = HOSTS[state["platform"]]
    path = urlparse(url).path or "/"
    default_path = path.rsplit("/", 1)[0] or "/"
    entries = [dict(entry) for entry in state["cookies"]]
    for raw in headers[:MAX_COOKIES]:
        if len(raw) > MAX_HEADER or not _ascii(raw):
            continue
        parsed = SimpleCookie()
        try:
            parsed.load(raw)
        except Exception:
            continue
        for name, morsel in parsed.items():
            domain = morsel["domain"].lstrip(".").lower() or host
            cookie_path = morsel["path"] if morsel["path"].startswith("/") else default_path
            value = morsel.coded_value
            if domain not in (host, host[4:]) or not NAME.fullmatch(name) or not _ascii(value) or ";" in value:
                continue
            if name.startswith("__Secure-") and not morsel["secure"]:
                continue
            if name.startswith("__Host-") and (not morsel["secure"] or morsel["domain"] or morsel["path"] != "/"):
                continue
            expiry = None
            try:
                if morsel["max-age"] and re.fullmatch(r"-?\d+", morsel["max-age"]):
                    expiry = now + min(int(morsel["max-age"]), 400 * 86400)
                elif morsel["expires"]:
                    expiry = parsedate_to_datetime(morsel["expires"]).timestamp()
            except (ValueError, TypeError, OverflowError):
                # Invalid expiration attributes are ignored, as with a session cookie.
                pass
            # A copied Cookie header has no original scopes. Replace its seed
            # entry by the first authoritative same-name Set-Cookie, even deletion.
            entries = [entry for entry in entries if not (entry["name"] == name and
                       (entry["seed"] or (entry["domain"] == domain and entry["path"] == cookie_path)))]
            if expiry is None or expiry > now:
                entries.append(dict(name=name, value=value, domain=domain, path=cookie_path, expires=expiry, seed=False))
    updated = {**state, "cookies": entries}
    _validate(updated, state["platform"])
    _header(updated, url, now)
    return updated


def save_response_cookies(snapshot: CookieSnapshot, headers: list[str]) -> str:
    """Call only after a successful, allowed target preview. Return fixed status."""
    if snapshot.state is None or snapshot.path is None:
        return "disabled"
    if not headers:
        return "unchanged"
    try:
        with LOCKS[snapshot.platform]:
            current = _read(snapshot.path, snapshot.platform)
            if current is None or current["revision"] != snapshot.revision:
                return "stale_response"
            updated = _merge(current, snapshot.url, headers, time.time())
            if updated["cookies"] == current["cookies"]:
                return "unchanged"
            updated["revision"] = uuid.uuid4().hex
            _write(snapshot.path, updated)
        logger.debug("%s cookie updates saved", snapshot.platform)
        return "saved"
    except Exception:
        logger.warning("%s cookie updates could not be saved", snapshot.platform)
        return "save_failed"
