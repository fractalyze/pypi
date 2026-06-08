#!/usr/bin/env python3
# Copyright 2026 The fractalyze Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""PEP 503 index generator for fractalyze private PyPI.

Mirrors wheel assets from source repositories (which may be private) to
public GitHub Releases on the pypi repository, then generates a static
PEP 503-compliant simple repository index deployed via GitHub Pages.

Uses two tokens:
  GH_TOKEN                      — default, writes to pypi repo releases
  FRACTALYZE_REPOS_READ_TOKEN   — reads from private source repos
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import quote

PYPI_REPO = "fractalyze/pypi"
SOURCE_TOKEN_ENV = "FRACTALYZE_REPOS_READ_TOKEN"

# Optional object-store hosting for wheel bytes. When PYPI_WHEELS_BUCKET is set,
# wheels and their .metadata sidecars are uploaded to that S3 bucket and the
# index emits the bucket URL as each href, keeping consumers off the github
# release-asset CDN (which intermittently 504s on the large wheels). When unset,
# hrefs point at the github release assets as before, so the index keeps working
# until the bucket is provisioned. The github release mirror happens either way:
# it is the enumeration source and a durable backup, never hit by consumers once
# S3 hosting is on.
S3_BUCKET = os.environ.get("PYPI_WHEELS_BUCKET")
S3_BASE_URL = (os.environ.get("PYPI_WHEELS_BASE_URL") or "").rstrip("/")

# Recency bound on the PEP 658 sidecar + S3 backfill. The github release mirror
# holds every wheel ever built (thousands; ~100 GB), almost all stale daily-dev
# builds. Re-downloading each to extract METADATA and pushing it to S3 would haul
# the whole ~100 GB on the first run. Bound it: only the newest
# PYPI_WHEELS_BACKFILL_LAST source releases per repo get sidecars + S3 copies.
# Older wheels keep their github-asset href (no sidecar) and resolve as before.
# 0 disables the bound (backfill everything). The github wheel mirror itself is
# never bounded — it stays the complete enumeration source.
BACKFILL_LAST = int(os.environ.get("PYPI_WHEELS_BACKFILL_LAST", "10"))


def _make_env(token_env: str) -> dict[str, str]:
    """Build subprocess environment with GH_TOKEN set from *token_env*."""
    env = os.environ.copy()
    env["GH_TOKEN"] = os.environ[token_env]
    return env


def normalize_name(name: str) -> str:
    """PEP 503 name normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()


def gh_api(endpoint: str, *, token_env: str = "GH_TOKEN"):
    """Call GitHub API via gh CLI and return parsed JSON (with pagination)."""
    result = subprocess.run(
        ["gh", "api", endpoint, "--paginate"],
        capture_output=True,
        text=True,
        check=True,
        env=_make_env(token_env),
    )
    return json.loads(result.stdout)


def gh_api_get(endpoint: str, *, token_env: str = "GH_TOKEN"):
    """Call GitHub API for a single resource. Returns None on failure."""
    result = subprocess.run(
        ["gh", "api", endpoint],
        capture_output=True,
        text=True,
        env=_make_env(token_env),
    )
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)


def s3_key(pypi_tag: str, name: str) -> str:
    """Bucket key for an asset, namespaced by mirror tag to avoid collisions."""
    return f"{pypi_tag}/{name}"


def s3_object_exists(key: str) -> bool:
    """True if *key* already exists in the wheels bucket (idempotency check)."""
    result = subprocess.run(
        ["aws", "s3api", "head-object", "--bucket", S3_BUCKET, "--key", key],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def s3_upload(local_path: Path, key: str) -> None:
    """Upload *local_path* to the wheels bucket at *key*."""
    subprocess.run(
        ["aws", "s3", "cp", str(local_path), f"s3://{S3_BUCKET}/{key}"],
        check=True,
    )


_METADATA_MEMBER = re.compile(r"^[^/]+\.dist-info/METADATA$")


def extract_metadata_sidecar(wheel_path: Path) -> Path | None:
    """Write a PEP 658 ``<wheel>.metadata`` sidecar next to *wheel_path*.

    Reads ``*.dist-info/METADATA`` from the wheel zip and writes those bytes
    verbatim as ``<wheel>.metadata``. Returns the sidecar path, or None if
    the wheel carries no top-level ``dist-info/METADATA`` member.
    """
    with zipfile.ZipFile(wheel_path) as zf:
        members = [n for n in zf.namelist() if _METADATA_MEMBER.match(n)]
        if not members:
            return None
        data = zf.read(members[0])
    sidecar = wheel_path.with_name(wheel_path.name + ".metadata")
    sidecar.write_bytes(data)
    return sidecar


def core_metadata_value(sidecar_asset: dict | None) -> str | None:
    """PEP 714 ``data-core-metadata`` value for a ``.metadata`` sidecar asset.

    Returns ``sha256=<hex>`` when GitHub reports a sha256 digest on the asset,
    ``true`` when the sidecar exists without a usable digest, or None when no
    sidecar asset is present.
    """
    if sidecar_asset is None:
        return None
    digest = sidecar_asset.get("digest") or ""
    if digest.startswith("sha256:"):
        return f"sha256={digest.split(':', 1)[1]}"
    return "true"


def mirror_repo_wheels(repo: str) -> None:
    """Mirror all wheel assets from a source repo to pypi repo releases.

    For each source release, creates a corresponding release on the pypi
    repo with tag ``<repo-name>-<source-tag>`` and uploads all .whl
    assets.  Already-mirrored wheels are skipped (idempotent).

    Uses FRACTALYZE_REPOS_READ_TOKEN for source repo access and
    GH_TOKEN (GITHUB_TOKEN) for pypi repo writes.
    """
    repo_name = repo.split("/")[1]
    prefix = normalize_name(repo_name)

    source_releases = gh_api(
        f"repos/{repo}/releases", token_env=SOURCE_TOKEN_ENV
    )

    # gh returns releases newest-first, so the first BACKFILL_LAST are the most
    # recent — the only ones that get sidecars + S3 copies (see BACKFILL_LAST).
    for idx, release in enumerate(source_releases):
        recent = BACKFILL_LAST == 0 or idx < BACKFILL_LAST
        source_tag = release["tag_name"]
        pypi_tag = f"{prefix}-{source_tag}"

        # Collect all .whl assets from this source release.
        wheels = [
            a for a in release.get("assets", [])
            if a["name"].endswith(".whl")
        ]
        if not wheels:
            continue

        # Check which wheels are already mirrored (query pypi repo).
        existing_names: set[str] = set()
        pypi_release = gh_api_get(
            f"repos/{PYPI_REPO}/releases/tags/{pypi_tag}"
        )
        if pypi_release is not None:
            existing_names = {
                a["name"] for a in pypi_release.get("assets", [])
            }

        # Each mirrored wheel needs two assets: the wheel itself and its
        # PEP 658 ``<wheel>.metadata`` sidecar. They are tracked
        # independently because a wheel mirrored before sidecars existed has
        # the wheel but no sidecar — that case backfills the sidecar alone.
        # The github wheel mirror is never bounded — it is the enumeration
        # source, so every release keeps its wheel asset.
        upload_wheel = {
            w["name"] for w in wheels if w["name"] not in existing_names
        }
        # Sidecars and S3 copies are bounded to recent releases. Older releases
        # keep their plain github-asset href; phase 2 emits an S3 href only where
        # a sidecar exists, so the two stay consistent without a bucket listing.
        upload_meta: set[str] = set()
        s3_wheel: set[str] = set()
        s3_meta: set[str] = set()
        if recent:
            upload_meta = {
                w["name"] for w in wheels
                if f"{w['name']}.metadata" not in existing_names
            }
            # When S3 hosting is on, the bucket needs the same two objects. A
            # wheel already on github may still be absent from S3 (e.g. first
            # rollout), so check the bucket independently of the asset listing.
            if S3_BUCKET:
                s3_wheel = {
                    w["name"] for w in wheels
                    if not s3_object_exists(s3_key(pypi_tag, w["name"]))
                }
                s3_meta = {
                    w["name"] for w in wheels
                    if not s3_object_exists(
                        s3_key(pypi_tag, f"{w['name']}.metadata")
                    )
                }
        # Extracting METADATA needs the wheel bytes, so any wheel missing any
        # target (github or S3, wheel or sidecar) is downloaded once and reused.
        to_download = sorted(upload_wheel | upload_meta | s3_wheel | s3_meta)
        if not to_download:
            print(
                f"  {pypi_tag}: all {len(wheels)} wheels + sidecars present"
            )
            continue

        # Create the pypi release if it does not exist yet.
        if pypi_release is None:
            print(f"  Creating release {pypi_tag}")
            subprocess.run(
                [
                    "gh", "release", "create", pypi_tag,
                    "--repo", PYPI_REPO,
                    "--title", pypi_tag,
                    "--notes", f"Mirrored from {repo} {source_tag}",
                ],
                check=True,
            )

        # Download from source (read token) and upload to pypi (write token).
        source_env = _make_env(SOURCE_TOKEN_ENV)
        with tempfile.TemporaryDirectory() as tmp:
            for name in to_download:
                print(f"  Mirroring: {name} -> {pypi_tag}")

                subprocess.run(
                    [
                        "gh", "release", "download", source_tag,
                        "--pattern", name,
                        "--dir", tmp,
                        "--repo", repo,
                    ],
                    check=True,
                    env=source_env,
                )
                wheel_path = Path(tmp) / name

                if name in upload_wheel:
                    subprocess.run(
                        [
                            "gh", "release", "upload", pypi_tag,
                            str(wheel_path),
                            "--repo", PYPI_REPO,
                            "--clobber",
                        ],
                        check=True,
                    )
                if name in s3_wheel:
                    s3_upload(wheel_path, s3_key(pypi_tag, name))

                if name in upload_meta or name in s3_meta:
                    sidecar = extract_metadata_sidecar(wheel_path)
                    if sidecar is None:
                        print(
                            f"    WARNING: no dist-info/METADATA in {name}; "
                            "skipping PEP 658 sidecar"
                        )
                        continue
                    if name in upload_meta:
                        subprocess.run(
                            [
                                "gh", "release", "upload", pypi_tag,
                                str(sidecar),
                                "--repo", PYPI_REPO,
                                "--clobber",
                            ],
                            check=True,
                        )
                    if name in s3_meta:
                        s3_upload(
                            sidecar, s3_key(pypi_tag, f"{name}.metadata")
                        )


def collect_package_wheels(package_name: str) -> list[dict]:
    """Collect wheel download URLs for a package from pypi repo releases.

    Each returned dict carries ``filename`` and ``url``, plus
    ``core_metadata`` — the PEP 714 ``data-core-metadata`` attribute value
    for the wheel's ``.metadata`` sidecar, or None when no sidecar exists.
    """
    normalized = normalize_name(package_name)
    whl_prefix = normalized.replace("-", "_") + "-"

    releases = gh_api(f"repos/{PYPI_REPO}/releases")
    wheels = []

    for release in releases:
        tag = release["tag_name"]
        assets = {a["name"]: a for a in release.get("assets", [])}
        for name, asset in assets.items():
            if not (name.endswith(".whl") and name.startswith(whl_prefix)):
                continue
            sidecar = assets.get(f"{name}.metadata")
            # A sidecar is uploaded together with the S3 copy (same recency
            # bound), so its presence means the wheel was backfilled to S3.
            # Emit the S3 href only then; otherwise the bytes are not in the
            # bucket and the wheel keeps its github-asset href.
            if S3_BASE_URL and sidecar is not None:
                # pip derives the PEP 658 metadata URL as <href>.metadata, so
                # the sidecar must live at the matching key in the bucket.
                url = f"{S3_BASE_URL}/{quote(s3_key(tag, name))}"
            else:
                url = (
                    f"https://github.com/{PYPI_REPO}"
                    f"/releases/download/{tag}/{quote(name)}"
                )
            wheels.append({
                "filename": name,
                "url": url,
                "core_metadata": core_metadata_value(sidecar),
            })

    return wheels


def generate_package_index(
    package_name: str, wheels: list[dict], output_dir: Path
) -> None:
    """Generate per-package index.html with links to pypi repo releases."""
    normalized = normalize_name(package_name)
    pkg_dir = output_dir / normalized
    pkg_dir.mkdir(parents=True, exist_ok=True)

    links = []
    for wheel in sorted(wheels, key=lambda w: w["filename"]):
        attr = ""
        if wheel.get("core_metadata"):
            attr = f' data-core-metadata="{wheel["core_metadata"]}"'
        links.append(
            f'    <a href="{wheel["url"]}"{attr}>{wheel["filename"]}</a>'
        )

    html = (
        "<!DOCTYPE html>\n"
        "<html>\n"
        "  <body>\n"
        + "\n".join(links)
        + "\n"
        "  </body>\n"
        "</html>\n"
    )
    (pkg_dir / "index.html").write_text(html)


def generate_root_index(
    package_names: list[str], output_dir: Path
) -> None:
    """Generate root index.html listing all packages."""
    links = []
    for name in sorted(package_names):
        normalized = normalize_name(name)
        links.append(f'    <a href="{normalized}/">{normalized}</a>')

    html = (
        "<!DOCTYPE html>\n"
        "<html>\n"
        "  <body>\n"
        + "\n".join(links)
        + "\n"
        "  </body>\n"
        "</html>\n"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "index.html").write_text(html)


def main():
    config = json.loads(Path("config.json").read_text())
    site_dir = Path("site")
    channel_dir = site_dir / "simple"

    # Phase 1: Mirror wheels from all source repos to pypi releases.
    mirrored_repos: set[str] = set()
    for pkg in config["packages"]:
        repo = pkg["repo"]
        if repo not in mirrored_repos:
            print(f"Mirroring wheels from {repo}...")
            mirror_repo_wheels(repo)
            mirrored_repos.add(repo)

    # Phase 2: Generate PEP 503 index from pypi repo releases.
    package_names = []
    for pkg in config["packages"]:
        wheels = collect_package_wheels(pkg["name"])
        if wheels:
            package_names.append(pkg["name"])
            generate_package_index(pkg["name"], wheels, channel_dir)

    generate_root_index(package_names, channel_dir)
    shutil.copy("static/index.html", site_dir / "index.html")
    (site_dir / ".nojekyll").touch()
    print(f"Generated index for {len(package_names)} packages")


if __name__ == "__main__":
    main()
