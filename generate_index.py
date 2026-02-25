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
from pathlib import Path
from urllib.parse import quote

PYPI_REPO = "fractalyze/pypi"
SOURCE_TOKEN_ENV = "FRACTALYZE_REPOS_READ_TOKEN"


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

    for release in source_releases:
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

        to_upload = [w for w in wheels if w["name"] not in existing_names]
        if not to_upload:
            print(f"  {pypi_tag}: all {len(wheels)} wheels already mirrored")
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
            for asset in to_upload:
                name = asset["name"]
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
                subprocess.run(
                    [
                        "gh", "release", "upload", pypi_tag,
                        str(Path(tmp) / name),
                        "--repo", PYPI_REPO,
                        "--clobber",
                    ],
                    check=True,
                )


def collect_package_wheels(package_name: str) -> list[dict]:
    """Collect wheel download URLs for a package from pypi repo releases."""
    normalized = normalize_name(package_name)
    whl_prefix = normalized.replace("-", "_") + "-"

    releases = gh_api(f"repos/{PYPI_REPO}/releases")
    wheels = []

    for release in releases:
        tag = release["tag_name"]
        for asset in release.get("assets", []):
            name = asset["name"]
            if name.endswith(".whl") and name.startswith(whl_prefix):
                url = (
                    f"https://github.com/{PYPI_REPO}"
                    f"/releases/download/{tag}/{quote(name)}"
                )
                wheels.append({"filename": name, "url": url})

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
        links.append(f'    <a href="{wheel["url"]}">{wheel["filename"]}</a>')

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
