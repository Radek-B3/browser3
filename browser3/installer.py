# SPDX-License-Identifier: MPL-2.0

"""Signed Browser3 runtime installer and versioned local cache."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.resources
import json
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import DEFAULT_RUNTIME_VERSION
from .openpgp import OpenPGPError, VerificationResult, verify_detached


REPOSITORY = "Radek-B3/browser3"
RELEASE_COMMIT = "d59a9c5c9b4acfa6d249d299566be51b4b36c39c"
RELEASE_ID = 373993070
PINNED_RELEASE_KEY_FINGERPRINT = "138AE85373688ADFFDD005A27439B75BE8645184"
# This is the exact case used by the immutable r3 GitHub Release asset.  GitHub
# release URLs are case-sensitive on some mirrors, so do not normalize it.
ASSET_TEMPLATE = "Browser3-{version_base}-windows-x64.zip"
VERSION_RE = re.compile(r"^(?P<base>\d+\.\d+\.\d+\.\d+)(?:-r(?P<revision>\d+))?$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_SIGNATURE_BYTES = 1024 * 1024
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
CRITICAL_RUNTIME_FILES = (
    "launcher.py",
    "browser3_paths.py",
    "generate_profiles.py",
    "resources/gpu_templates.json",
    # These files are imported or executed by launcher.py.  Hashing only the
    # four policy files above would allow a damaged cache to run altered proxy,
    # desktop, CDP, or host-probe code while still passing the cache check.
    "proxy_forwarder.py",
    "socks5_forwarder.py",
    "windows_desktop.py",
    "browser3_agent.py",
    "browser3.cmd",
    "browser3.bat",
    "scripts/probe_host.py",
    "scripts/preflight_codecs.py",
)


class Browser3Error(RuntimeError):
    """Base error shown by the CLI without leaking local secrets."""


class UnsupportedPlatform(Browser3Error):
    pass


class VersionError(Browser3Error):
    pass


class VerificationError(Browser3Error):
    pass


class ArchiveError(Browser3Error):
    pass


class InstallError(Browser3Error):
    pass


@dataclass(frozen=True)
class ReleaseSpec:
    """Immutable metadata for one explicitly supported release."""

    version: str
    tag: str
    asset_name: str
    release_commit: str = RELEASE_COMMIT
    release_id: int = RELEASE_ID
    base_url: str = ""

    def __post_init__(self):
        if not self.base_url:
            object.__setattr__(self, "base_url",
                               f"https://github.com/{REPOSITORY}/releases/download/{self.tag}")

    @property
    def manifest_url(self):
        return f"{self.base_url}/SHA256SUMS.txt"

    @property
    def signature_url(self):
        return f"{self.base_url}/SHA256SUMS.txt.asc"

    @property
    def asset_url(self):
        return f"{self.base_url}/{self.asset_name}"


def _default_spec() -> ReleaseSpec:
    base = DEFAULT_RUNTIME_VERSION.rsplit("-r", 1)[0]
    return ReleaseSpec(DEFAULT_RUNTIME_VERSION, f"v{DEFAULT_RUNTIME_VERSION}",
                       ASSET_TEMPLATE.format(version_base=base))


SUPPORTED_RELEASES = {DEFAULT_RUNTIME_VERSION: _default_spec(),
                      _default_spec().version.rsplit("-r", 1)[0]: _default_spec()}


def resolve_version(cli_value: str | None = None, catalog=None) -> str:
    """Resolve CLI > environment > immutable package pin."""
    raw = cli_value if cli_value is not None else os.environ.get("BROWSER3_VERSION")
    if raw is None or not raw.strip():
        return DEFAULT_RUNTIME_VERSION
    value = raw.strip()
    catalog = SUPPORTED_RELEASES if catalog is None else catalog
    if value not in catalog:
        raise VersionError("Unsupported Browser3 version pin: %s" % value)
    return catalog[value].version


def release_spec(version: str, catalog=None) -> ReleaseSpec:
    catalog = SUPPORTED_RELEASES if catalog is None else catalog
    if version not in catalog:
        raise VersionError("Unsupported Browser3 version pin: %s" % version)
    spec = catalog[version]
    if not isinstance(spec, ReleaseSpec):
        raise VersionError("Invalid release metadata for version: %s" % version)
    if not VERSION_RE.fullmatch(spec.version):
        raise VersionError("Invalid release version metadata")
    if not spec.asset_name.endswith("-windows-x64.zip") or "/" in spec.asset_name or "\\" in spec.asset_name:
        raise VersionError("Invalid release asset metadata")
    return spec


def data_root_from_environment() -> Path:
    override = os.environ.get("BROWSER3_DATA_DIR", "").strip()
    if override:
        path = Path(os.path.abspath(os.path.expandvars(os.path.expanduser(override))))
    else:
        local = os.environ.get("LOCALAPPDATA", "").strip()
        if not local:
            raise UnsupportedPlatform("LOCALAPPDATA is not set; set BROWSER3_DATA_DIR for a development/test root")
        path = Path(os.path.abspath(local)) / "Browser3"
    return path


def platform_supported() -> bool:
    return sys.platform == "win32" and platform.machine().lower() in ("amd64", "x86_64") and struct.calcsize("P") == 8


def require_supported_platform():
    if not platform_supported():
        raise UnsupportedPlatform("Browser3 runtime distribution supports Windows x64 only")


def validate_binary_override(value: str | None) -> Path | None:
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        raise Browser3Error("BROWSER3_BINARY_PATH must not be empty")
    expanded = os.path.abspath(os.path.expandvars(os.path.expanduser(raw)))
    # ntpath catches drive-letter paths when tests inspect Windows values on POSIX.
    import ntpath
    if not os.path.isabs(raw) and not ntpath.isabs(raw):
        raise Browser3Error("BROWSER3_BINARY_PATH must be an absolute path")
    path = Path(expanded)
    if not path.is_file():
        raise Browser3Error("BROWSER3_BINARY_PATH does not point to a file")
    return path


def _key_bytes() -> bytes:
    try:
        return importlib.resources.files("browser3").joinpath("data/browser3-release-key.asc").read_bytes()
    except AttributeError:  # Python 3.8 compatibility
        import pkgutil
        value = pkgutil.get_data("browser3", "data/browser3-release-key.asc")
        if value is None:
            raise VerificationError("Bundled Browser3 release key is missing")
        return value
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise VerificationError("Bundled Browser3 release key is missing") from exc


def _safe_commonpath(root: Path, target: Path) -> bool:
    try:
        return os.path.commonpath((str(root), str(target))) == str(root)
    except ValueError:
        return False


def _is_reparse_or_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    if os.name == "nt":
        try:
            import ctypes
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
            if attrs not in (-1, 0xFFFFFFFF) and attrs & 0x400:
                return True
        except (AttributeError, OSError):
            pass
    return False


def _validate_member_name(name: str) -> str:
    if not name or "\x00" in name:
        raise ArchiveError("Archive contains an invalid member name")
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ArchiveError("Archive contains an absolute member path")
    parts = normalized.split("/")
    if any(part in ("", ".", "..") for part in parts[:-1]) or parts[-1] in (".", ".."):
        raise ArchiveError("Archive contains path traversal")
    for part in parts:
        if not part:
            continue  # a trailing slash denotes a directory entry
        if any(char in part for char in ':*?<>|"'):
            raise ArchiveError("Archive contains a Windows-invalid member name")
        if part[-1] in (".", " "):
            raise ArchiveError("Archive contains a Windows-ambiguous member name")
        if part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
            raise ArchiveError("Archive contains a reserved Windows member name")
    return "/".join(parts)


def safe_extract(zip_path: Path, destination: Path):
    """Extract a ZIP into a fresh directory without link/traversal escapes."""
    destination.mkdir(parents=True, exist_ok=False)
    seen = set()
    total = 0
    try:
        with zipfile.ZipFile(zip_path) as archive:
            if archive.testzip() is not None:
                raise ArchiveError("Archive CRC verification failed")
            infos = archive.infolist()
            if not infos:
                raise ArchiveError("Archive is empty")
            for info in infos:
                member = _validate_member_name(info.filename)
                key = member.casefold().rstrip("/")
                if key in seen:
                    raise ArchiveError("Archive contains duplicate member names")
                seen.add(key)
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode):
                    raise ArchiveError("Archive contains a link or special file")
                is_dir = info.is_dir() or info.filename.endswith(("/", "\\"))
                if info.file_size < 0 or info.file_size > MAX_UNCOMPRESSED_BYTES:
                    raise ArchiveError("Archive member is too large")
                total += info.file_size
                if total > MAX_UNCOMPRESSED_BYTES:
                    raise ArchiveError("Archive expands beyond the safety limit")
                target = destination.joinpath(*member.split("/"))
                if not _safe_commonpath(destination, target):
                    raise ArchiveError("Archive member escapes extraction directory")
                parent = target.parent
                parent.mkdir(parents=True, exist_ok=True)
                current = parent
                while True:
                    if _is_reparse_or_link(current):
                        raise ArchiveError("Archive extraction encountered a reparse/link path")
                    if current == destination:
                        break
                    current = current.parent
                if is_dir:
                    target.mkdir(exist_ok=False)
                    continue
                if target.exists() or _is_reparse_or_link(target):
                    raise ArchiveError("Archive member collides with an existing path")
                with archive.open(info, "r") as source, open(target, "xb") as out:
                    copied = 0
                    while True:
                        chunk = source.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > info.file_size or copied > MAX_UNCOMPRESSED_BYTES:
                            raise ArchiveError("Archive member expanded beyond its declared size")
                        out.write(chunk)
                    out.flush()
                    os.fsync(out.fileno())
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        if isinstance(exc, ArchiveError):
            raise
        raise ArchiveError("Could not safely extract Browser3 archive") from exc
    for root, dirs, files in os.walk(destination):
        for name in dirs + files:
            if _is_reparse_or_link(Path(root) / name):
                raise ArchiveError("Extracted archive contains a link or reparse point")


def validate_runtime_layout(root: Path):
    required = ("runtime/chrome.exe", "launcher.py", "browser3_paths.py",
                "generate_profiles.py", "profiles.json", "resources/gpu_templates.json")
    for relative in required:
        path = root / Path(relative)
        if not path.is_file() or _is_reparse_or_link(path):
            raise InstallError("Release runtime is missing required layout: %s" % relative)
    if not (root / "MANIFEST.txt").is_file() or not (root / "SBOM.spdx.json").is_file():
        raise InstallError("Release runtime is missing its manifest/SBOM")


@contextlib.contextmanager
def _process_lock(path: Path, timeout: float = 300.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    acquired = False
    deadline = time.monotonic() + timeout
    try:
        if os.name == "nt":
            import msvcrt
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise InstallError("Timed out waiting for Browser3 installation lock")
                    time.sleep(0.1)
        else:
            import fcntl
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise InstallError("Timed out waiting for Browser3 installation lock")
                    time.sleep(0.1)
        acquired = True
        yield
    finally:
        try:
            if acquired:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _download(url: str, target: Path, *, max_bytes: int, timeout: float):
    request = urllib.request.Request(url, headers={"User-Agent": "browser3-python-client/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                raise InstallError("Downloaded release object exceeds the safety limit")
            count = 0
            with open(target, "xb") as out:
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > max_bytes:
                        raise InstallError("Downloaded release object exceeds the safety limit")
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        if isinstance(exc, InstallError):
            raise
        raise InstallError("Could not download the signed Browser3 release asset") from exc


def _download_bytes(url: str, *, max_bytes: int, timeout: float) -> bytes:
    fd, name = tempfile.mkstemp(prefix="browser3-object-")
    os.close(fd)
    target = Path(name)
    target.unlink()
    try:
        _download(url, target, max_bytes=max_bytes, timeout=timeout)
        return target.read_bytes()
    finally:
        try:
            target.unlink()
        except OSError:
            pass


def _manifest_hash(manifest: bytes, asset_name: str) -> str:
    if len(manifest) > MAX_MANIFEST_BYTES:
        raise VerificationError("Signed release manifest is too large")
    found = None
    try:
        lines = manifest.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise VerificationError("Signed release manifest is not UTF-8") from exc
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or not SHA256_RE.fullmatch(parts[0]):
            raise VerificationError("Signed release manifest contains an invalid row")
        name = parts[1].lstrip("*").strip()
        normalized_name = name.replace("\\", "/")
        if (not name or "\x00" in name or normalized_name != name
                or normalized_name.startswith("/") or normalized_name in (".", "..")
                or "/" in normalized_name):
            raise VerificationError("Signed release manifest contains an invalid asset path")
        if name == asset_name:
            if found is not None:
                raise VerificationError("Signed release manifest contains duplicate asset rows")
            found = parts[0].lower()
    if found is None:
        raise VerificationError("Signed release manifest does not contain the pinned Browser3 asset")
    return found


@dataclass(frozen=True)
class InstallResult:
    version: str
    root: Path
    archive_sha256: str
    cache_hit: bool
    signature: VerificationResult | None = None


class Installer:
    def __init__(self, data_root: Path | str | None = None, *, catalog=None,
                 timeout: float = 30.0, enforce_platform: bool = True):
        self.data_root = Path(data_root) if data_root is not None else data_root_from_environment()
        self.browsers_root = self.data_root / "browsers"
        self.catalog = SUPPORTED_RELEASES if catalog is None else catalog
        self.timeout = timeout
        self.enforce_platform = enforce_platform

    def _check_platform(self):
        if self.enforce_platform:
            require_supported_platform()

    def _target(self, version: str) -> Path:
        if not VERSION_RE.fullmatch(version):
            raise VersionError("Invalid Browser3 runtime version")
        return self.browsers_root / version

    @staticmethod
    def _marker(root: Path) -> Path:
        return root / ".browser3-install.json"

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            while True:
                chunk = source.read(CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def _valid_existing(self, root: Path, spec: ReleaseSpec) -> bool:
        if not root.is_dir() or _is_reparse_or_link(root):
            return False
        marker = self._marker(root)
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        binary = root / "runtime/chrome.exe"
        if (value.get("version") != spec.version or value.get("asset") != spec.asset_name
                or value.get("release_commit") != spec.release_commit
                or value.get("release_tag") != spec.tag
                or value.get("signature_fingerprint") != PINNED_RELEASE_KEY_FINGERPRINT
                or not binary.is_file() or _is_reparse_or_link(binary)):
            return False
        if (not isinstance(value.get("asset_sha256"), str)
                or not SHA256_RE.fullmatch(value["asset_sha256"])
                or not isinstance(value.get("archive_sha256"), str)
                or not SHA256_RE.fullmatch(value["archive_sha256"])):
            return False
        runtime_hash = value.get("runtime_sha256")
        if not isinstance(runtime_hash, str) or not SHA256_RE.fullmatch(runtime_hash):
            return False
        orchestration_hashes = value.get("orchestration_sha256")
        if (not isinstance(orchestration_hashes, dict)
                or set(orchestration_hashes) != set(CRITICAL_RUNTIME_FILES)
                or any(not isinstance(orchestration_hashes.get(relative), str)
                       or not SHA256_RE.fullmatch(orchestration_hashes[relative])
                       for relative in CRITICAL_RUNTIME_FILES)):
            return False
        try:
            validate_runtime_layout(root)
            critical_paths = [root / Path(relative) for relative in CRITICAL_RUNTIME_FILES]
            if any(not path.is_file() or _is_reparse_or_link(path)
                   for path in critical_paths):
                return False
            if self._sha256_file(binary) != runtime_hash.lower():
                return False
            return all(
                self._sha256_file(root / Path(relative))
                == orchestration_hashes[relative].lower()
                for relative in CRITICAL_RUNTIME_FILES
            )
        except (Browser3Error, OSError):
            return False

    def install(self, version: str | None = None) -> InstallResult:
        self._check_platform()
        selected = resolve_version(version, self.catalog)
        spec = release_spec(selected, self.catalog)
        target = self._target(spec.version)
        try:
            self.browsers_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise InstallError("Browser3 runtime cache is not writable") from exc
        # A valid immutable cache hit does not need to contend on the installer
        # lock. This also avoids a Windows msvcrt same-process re-lock while a
        # caller launches the same pinned version repeatedly.
        if self._valid_existing(target, spec):
            marker = json.loads(self._marker(target).read_text(encoding="utf-8"))
            return InstallResult(spec.version, target, marker["archive_sha256"], True, None)
        lock = self.browsers_root / (spec.version + ".lock")
        with _process_lock(lock):
            if self._valid_existing(target, spec):
                marker = json.loads(self._marker(target).read_text(encoding="utf-8"))
                return InstallResult(spec.version, target, marker["archive_sha256"], True, None)
            if target.exists():
                raise InstallError("A partial or unrecognized runtime already occupies the pinned cache path")
            archive_tmp = self.browsers_root / (".%s.%s.zip" % (spec.version, uuid.uuid4().hex))
            extract_tmp = self.browsers_root / (".%s.%s.extract" % (spec.version, uuid.uuid4().hex))
            try:
                manifest = _download_bytes(spec.manifest_url, max_bytes=MAX_MANIFEST_BYTES,
                                           timeout=self.timeout)
                signature = _download_bytes(spec.signature_url, max_bytes=MAX_SIGNATURE_BYTES,
                                            timeout=self.timeout)
                try:
                    verification = verify_detached(manifest, signature, _key_bytes())
                except OpenPGPError as exc:
                    raise VerificationError("Release manifest OpenPGP verification failed") from exc
                if verification.fingerprint != PINNED_RELEASE_KEY_FINGERPRINT:
                    raise VerificationError("Release manifest key fingerprint is not pinned")
                expected = _manifest_hash(manifest, spec.asset_name)
                _download(spec.asset_url, archive_tmp, max_bytes=MAX_ARCHIVE_BYTES,
                          timeout=self.timeout)
                archive_hash = self._sha256_file(archive_tmp)
                if archive_hash != expected:
                    raise VerificationError("Release archive SHA-256 does not match the signed manifest")
                safe_extract(archive_tmp, extract_tmp)
                validate_runtime_layout(extract_tmp)
                marker = {
                    "version": spec.version,
                    "asset": spec.asset_name,
                    "asset_sha256": expected,
                    "archive_sha256": archive_hash,
                    "runtime_sha256": self._sha256_file(extract_tmp / "runtime/chrome.exe"),
                    "orchestration_sha256": {
                        relative: self._sha256_file(extract_tmp / Path(relative))
                        for relative in CRITICAL_RUNTIME_FILES
                    },
                    "release_tag": spec.tag,
                    "release_commit": spec.release_commit,
                    "release_id": spec.release_id,
                    "signature_fingerprint": verification.fingerprint,
                    "installed_at": int(time.time()),
                }
                marker_path = self._marker(extract_tmp)
                marker_path.write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                with open(marker_path, "r+b") as handle:
                    os.fsync(handle.fileno())
                os.replace(extract_tmp, target)
                return InstallResult(spec.version, target, archive_hash, False, verification)
            except (Browser3Error, OpenPGPError):
                raise
            except Exception as exc:
                raise InstallError("Browser3 runtime installation failed before activation") from exc
            finally:
                for path in (archive_tmp, extract_tmp):
                    try:
                        if path.is_dir():
                            shutil.rmtree(path)
                        else:
                            path.unlink()
                    except OSError:
                        pass

    def installed(self):
        if not self.browsers_root.is_dir():
            return []
        rows = []
        for child in sorted(self.browsers_root.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            marker = self._marker(child)
            try:
                value = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if value.get("version") == child.name and (child / "runtime/chrome.exe").is_file():
                rows.append(value)
        return rows

    def doctor(self, version: str | None = None):
        selected = resolve_version(version, self.catalog)
        spec = release_spec(selected, self.catalog)
        result = {"platform_supported": platform_supported(), "data_root_writable": False,
                  "selected_version": spec.version, "installed": False, "binary_sha256": None,
                  "runtime_root_required": True}
        try:
            self.data_root.mkdir(parents=True, exist_ok=True)
            probe = self.data_root / ".browser3-doctor-probe"
            probe.write_bytes(b"ok")
            probe.unlink()
            result["data_root_writable"] = True
        except OSError:
            pass
        target = self._target(spec.version)
        if self._valid_existing(target, spec):
            result["installed"] = True
            result["binary_sha256"] = hashlib.sha256((target / "runtime/chrome.exe").read_bytes()).hexdigest()
        override = os.environ.get("BROWSER3_BINARY_PATH")
        if override:
            try:
                path = validate_binary_override(override)
                result["binary_override"] = True
                result["binary_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            except Browser3Error:
                result["binary_override"] = False
        else:
            result["binary_override"] = False
        # BROWSER3_BINARY_PATH only replaces chrome.exe.  The signed runtime
        # root remains mandatory because launcher.py and its orchestration
        # modules are loaded from that verified installation.
        result["healthy"] = bool(result["platform_supported"] and result["data_root_writable"]
                                  and result["installed"]
                                  and (not override or result["binary_override"]))
        return result

    def launch(self, args: list[str], version: str | None = None) -> int:
        result = self.install(version)
        root = result.root
        launcher = root / "launcher.py"
        env = os.environ.copy()
        override = validate_binary_override(env.get("BROWSER3_BINARY_PATH"))
        if override:
            env["FP_CHROME_EXE"] = str(override)
            print("[browser3] BROWSER3_BINARY_PATH is an unverified user-managed binary", file=sys.stderr)
        command = [sys.executable, str(launcher)] + list(args)
        try:
            completed = subprocess.run(command, cwd=str(root), env=env)
        except OSError as exc:
            raise InstallError("Could not start the Browser3 runtime launcher") from exc
        return completed.returncode
