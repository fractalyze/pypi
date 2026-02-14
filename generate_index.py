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

Queries GitHub Release assets from configured repositories and generates
a static PEP 503-compliant simple repository index.
"""

import json
import re
import subprocess
from pathlib import Path


def normalize_name(name: str) -> str:
    """PEP 503 name normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()


def gh_api(endpoint: str):
    """Call GitHub API via gh CLI and return parsed JSON."""
    result = subprocess.run(
        ["gh", "api", endpoint, "--paginate"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def get_wheel_assets(repo: str, package_name: str) -> list[dict]:
    """Get .whl assets matching package_name from all releases of a repo.

    Filters by wheel filename prefix to support multiple packages from
    one repo (e.g. jax and jaxlib both come from fractalyze/jax).
    """
    normalized = normalize_name(package_name)
    prefix = normalized.replace("-", "_") + "-"
    releases = gh_api(f"repos/{repo}/releases")
    wheels = []
    for release in releases:
        tag = release["tag_name"]
        for asset in release.get("assets", []):
            name = asset["name"]
            if name.endswith(".whl") and name.startswith(prefix):
                wheels.append({
                    "filename": name,
                    "url": asset["browser_download_url"],
                    "tag": tag,
                })
    return wheels


def generate_package_index(
    package_name: str, wheels: list[dict], output_dir: Path
) -> None:
    """Generate per-package index.html."""
    normalized = normalize_name(package_name)
    pkg_dir = output_dir / normalized
    pkg_dir.mkdir(parents=True, exist_ok=True)

    links = []
    for wheel in wheels:
        href = wheel["url"]
        filename = wheel["filename"]
        links.append(f'    <a href="{href}">{filename}</a>')

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
    output_root = Path("site")
    channel_dir = output_root / "simple"
    package_names = []

    for pkg in config["packages"]:
        wheels = get_wheel_assets(pkg["repo"], pkg["name"])
        if wheels:
            package_names.append(pkg["name"])
            generate_package_index(pkg["name"], wheels, channel_dir)

    generate_root_index(package_names, channel_dir)
    print(f"Generated index for {len(package_names)} packages")


if __name__ == "__main__":
    main()
