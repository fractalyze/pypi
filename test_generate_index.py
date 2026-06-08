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

"""Unit tests for the pure logic in generate_index.py.

The network-bound mirror/collect helpers are not covered here; these tests
pin the PEP 658 sidecar extraction and the PEP 714 ``data-core-metadata``
href emission, which is the behavior that breaks consumers when wrong.
"""

import tempfile
import unittest
import zipfile
from pathlib import Path

import generate_index as gi


def _make_wheel(path: Path, *, metadata: bytes | None) -> None:
    """Write a minimal wheel zip, optionally carrying a dist-info/METADATA."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("pkg/__init__.py", b"")
        if metadata is not None:
            zf.writestr("pkg-1.0.dist-info/METADATA", metadata)


class ExtractMetadataSidecar(unittest.TestCase):
    def test_extracts_metadata_bytes_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / "pkg-1.0-py3-none-any.whl"
            body = b"Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n"
            _make_wheel(wheel, metadata=body)

            sidecar = gi.extract_metadata_sidecar(wheel)

            self.assertEqual(sidecar.name, "pkg-1.0-py3-none-any.whl.metadata")
            self.assertEqual(sidecar.read_bytes(), body)

    def test_returns_none_when_no_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / "pkg-1.0-py3-none-any.whl"
            _make_wheel(wheel, metadata=None)

            self.assertIsNone(gi.extract_metadata_sidecar(wheel))


class CoreMetadataValue(unittest.TestCase):
    def test_sha256_digest_becomes_hash_attr(self):
        asset = {"digest": "sha256:" + "a" * 64}
        self.assertEqual(
            gi.core_metadata_value(asset), "sha256=" + "a" * 64
        )

    def test_missing_or_blank_digest_falls_back_to_true(self):
        self.assertEqual(gi.core_metadata_value({"digest": ""}), "true")
        self.assertEqual(gi.core_metadata_value({}), "true")
        self.assertEqual(gi.core_metadata_value({"digest": "md5:x"}), "true")

    def test_no_sidecar_is_none(self):
        self.assertIsNone(gi.core_metadata_value(None))


class GeneratePackageIndex(unittest.TestCase):
    def _render(self, wheels):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            gi.generate_package_index("pkg", wheels, out)
            return (out / "pkg" / "index.html").read_text()

    def test_emits_data_core_metadata_when_present(self):
        html = self._render([
            {
                "filename": "pkg-1.0-py3-none-any.whl",
                "url": "https://example/pkg-1.0-py3-none-any.whl",
                "core_metadata": "sha256=" + "b" * 64,
            }
        ])
        self.assertIn(
            '<a href="https://example/pkg-1.0-py3-none-any.whl"'
            ' data-core-metadata="sha256=' + "b" * 64 + '">'
            "pkg-1.0-py3-none-any.whl</a>",
            html,
        )

    def test_omits_attr_when_no_sidecar(self):
        html = self._render([
            {
                "filename": "pkg-1.0-py3-none-any.whl",
                "url": "https://example/pkg-1.0-py3-none-any.whl",
                "core_metadata": None,
            }
        ])
        self.assertNotIn("data-core-metadata", html)
        self.assertIn(
            '<a href="https://example/pkg-1.0-py3-none-any.whl">'
            "pkg-1.0-py3-none-any.whl</a>",
            html,
        )


class S3Key(unittest.TestCase):
    def test_namespaces_by_tag(self):
        self.assertEqual(
            gi.s3_key("jax-jax-dev-20260608", "jaxlib-0.1-cp311.whl"),
            "jax-jax-dev-20260608/jaxlib-0.1-cp311.whl",
        )


class CollectPackageWheelsHref(unittest.TestCase):
    """Href selection: github release asset vs S3 bucket, sidecar unchanged."""

    _RELEASES = [{
        "tag_name": "jax-dev-1",
        "assets": [
            {"name": "jax-1.0-py3-none-any.whl", "digest": "sha256:" + "c" * 64},
            {"name": "jax-1.0-py3-none-any.whl.metadata",
             "digest": "sha256:" + "d" * 64},
        ],
    }]

    def setUp(self):
        self._orig_api = gi.gh_api
        self._orig_base = gi.S3_BASE_URL
        gi.gh_api = lambda endpoint, **kw: self._RELEASES

    def tearDown(self):
        gi.gh_api = self._orig_api
        gi.S3_BASE_URL = self._orig_base

    def test_github_href_when_no_bucket(self):
        gi.S3_BASE_URL = ""
        (w,) = gi.collect_package_wheels("jax")
        self.assertEqual(
            w["url"],
            "https://github.com/fractalyze/pypi/releases/download/"
            "jax-dev-1/jax-1.0-py3-none-any.whl",
        )
        # data-core-metadata comes from the sidecar asset's digest, not the wheel's.
        self.assertEqual(w["core_metadata"], "sha256=" + "d" * 64)

    def test_s3_href_when_bucket_configured(self):
        gi.S3_BASE_URL = "https://b.s3.ap-northeast-2.amazonaws.com"
        (w,) = gi.collect_package_wheels("jax")
        self.assertEqual(
            w["url"],
            "https://b.s3.ap-northeast-2.amazonaws.com/"
            "jax-dev-1/jax-1.0-py3-none-any.whl",
        )
        # Sidecar hash is host-independent; pip derives <href>.metadata on S3.
        self.assertEqual(w["core_metadata"], "sha256=" + "d" * 64)

    def test_github_href_when_bucket_set_but_no_sidecar(self):
        # A wheel outside the recency bound has no sidecar → not backfilled to
        # S3 → must keep its github href even when the bucket is configured,
        # else the S3 href would 404.
        gi.gh_api = lambda endpoint, **kw: [{
            "tag_name": "jax-dev-old",
            "assets": [{"name": "jax-0.1-py3-none-any.whl"}],  # no .metadata
        }]
        gi.S3_BASE_URL = "https://b.s3.ap-northeast-2.amazonaws.com"
        (w,) = gi.collect_package_wheels("jax")
        self.assertEqual(
            w["url"],
            "https://github.com/fractalyze/pypi/releases/download/"
            "jax-dev-old/jax-0.1-py3-none-any.whl",
        )
        self.assertIsNone(w["core_metadata"])


if __name__ == "__main__":
    unittest.main()
