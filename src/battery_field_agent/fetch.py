"""Fetch releases with integrity verification.

Design notes that matter on ephemeral compute:

* downloads go to a temp file and are moved into place only after the checksum
  matches, so a preempted run never leaves a half-file that looks complete;
* ``urllib`` is the default transport so this works with zero dependencies, and
  ``requests`` is used only when it happens to be installed;
* every unit of work is keyed by its expected hash, which makes re-running a
  partly-finished job free rather than merely safe.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import registry

__all__ = ["sha256_file", "verify", "fetch_url", "place_local", "FetchResult", "USER_AGENT"]

USER_AGENT = "battery-field-agent/0.1 (+https://github.com/; research ingestion)"
CHUNK = 1 << 20


@dataclass
class FetchResult:
    path: str
    bytes_written: int
    sha256: str
    source: str
    from_cache: bool = False

    def report(self) -> str:
        tag = "cached" if self.from_cache else "fetched"
        return f"{tag}: {self.path} ({self.bytes_written:,} bytes, sha256={self.sha256[:12]}...)"


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def verify(path: str, expected_sha256: str | None) -> str:
    """Return the file's sha256, raising if it does not match ``expected``."""
    actual = sha256_file(path)
    if expected_sha256 and actual.lower() != expected_sha256.lower():
        raise ValueError(
            f"checksum mismatch for {path}: expected {expected_sha256[:12]}..., got {actual[:12]}..."
        )
    return actual


def fetch_url(
    url: str,
    dest: str,
    expect_sha256: str | None = None,
    timeout: int = 60,
    overwrite: bool = False,
) -> FetchResult:
    """Download ``url`` to ``dest`` atomically, verifying a checksum if given.

    If ``dest`` already exists with the expected hash, no network call is made --
    this is what makes a resumed job cheap.
    """
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)

    if os.path.exists(dest) and not overwrite:
        try:
            digest = verify(dest, expect_sha256)
        except ValueError:
            pass  # corrupt or wrong file: fall through and re-download
        else:
            return FetchResult(dest, os.path.getsize(dest), digest, url, from_cache=True)

    directory = os.path.dirname(os.path.abspath(dest)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".part")
    os.close(fd)
    written = 0
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response, open(tmp, "wb") as out:
            while True:
                block = response.read(CHUNK)
                if not block:
                    break
                out.write(block)
                written += len(block)
        digest = verify(tmp, expect_sha256)
        if written == 0:
            raise ValueError(f"zero-length response from {url}")
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    return FetchResult(dest, written, digest, url)


def place_local(src: str, dest_dir: str, expect_sha256: str | None = None) -> FetchResult:
    """Register a file the operator obtained through a portal or licence click.

    Most of this corpus cannot be machine-fetched -- it sits behind a portal, a
    licence acceptance or an author request. That is a property of the data, not
    a gap in the pipeline, so the pipeline has a first-class path for it.
    """
    if not os.path.exists(src):
        raise FileNotFoundError(src)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(src))
    if os.path.abspath(src) != os.path.abspath(dest):
        shutil.copy2(src, dest)
    digest = verify(dest, expect_sha256)
    return FetchResult(dest, os.path.getsize(dest), digest, f"local:{src}")


def release_destination(release_id: str, data_dir: str) -> str:
    """Create and return the per-release directory, checking the catalogue first."""
    release = registry.get(release_id)  # raises KeyError with the known ids
    path = os.path.join(data_dir, release.id)
    os.makedirs(path, exist_ok=True)
    return path


def can_publish_raw(release_id: str) -> tuple[bool, str]:
    """Whether this release's *rows* may be written to a public artifact store."""
    release = registry.get(release_id)
    if release.redistributable:
        return True, f"{release.id}: redistributable per {release.licence}"
    return False, (
        f"{release.id}: NOT redistributable per {release.licence} -- "
        "publish derived metrics only, never rows"
    )
