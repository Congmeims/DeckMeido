"""双源升级：镜像与 GitHub 各查一次，取更高版本；全部本地假数据，不打网"""
from __future__ import annotations

import io
import os
import ssl
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import updater


def _cfg(**over) -> dict:
    cfg = {
        "mirror": "https://mirror.test/deckmeido",
        "github_repo": "example/deckmeido",
        "github_enable": True,
    }
    cfg.update(over)
    return cfg


def _release(source: str, tag: str) -> dict:
    return {
        "tag": tag,
        "source": source,
        "name": f"DeckMeido {tag}",
        "notes": "",
        "url": "",
        "published_at": "",
        "tarball_url": f"https://{source}.test/deckmeido-{tag}.tar.gz",
        "sha256": "",
    }


def _run(mirror, github, cfg=None):
    """在打好桩的 update_cfg / mirror_release / github_release 下跑 latest_release"""
    cfg = cfg or _cfg()
    with mock.patch.object(updater, "update_cfg", return_value=cfg), \
            mock.patch.object(updater, "mirror_release", side_effect=mirror), \
            mock.patch.object(updater, "github_release", side_effect=github):
        return updater.latest_release()


class DualSourcePickTest(unittest.TestCase):
    def test_higher_github_wins(self):
        info = _run(lambda _m: _release("mirror", "0.6.7"),
                    lambda _r: _release("github", "0.6.10"))
        self.assertEqual(info["source_used"], "github")
        self.assertEqual(info["tag"], "0.6.10")
        self.assertEqual(info["mirror_version"], "0.6.7")
        self.assertEqual(info["github_version"], "0.6.10")
        self.assertTrue(info["github_reachable"])

    def test_tie_prefers_mirror(self):
        info = _run(lambda _m: _release("mirror", "0.6.10"),
                    lambda _r: _release("github", "0.6.10"))
        self.assertEqual(info["source_used"], "mirror")
        self.assertEqual(info["tag"], "0.6.10")

    def test_semver_0_6_7_vs_0_6_10(self):
        info = _run(lambda _m: _release("mirror", "0.6.7"),
                    lambda _r: _release("github", "0.6.10"))
        self.assertEqual(info["tag"], "0.6.10")

    def test_mirror_failure_uses_github(self):
        def fail(_m):
            raise RuntimeError("镜像站查询失败（HTTP 502）")

        info = _run(fail, lambda _r: _release("github", "0.6.10"))
        self.assertEqual(info["source_used"], "github")
        self.assertEqual(info["mirror_version"], "")
        self.assertTrue(info["fallback_from"][0].startswith("镜像站"))

    def test_github_failure_uses_mirror(self):
        def fail(_r):
            raise RuntimeError("连不上 GitHub：timeout")

        info = _run(lambda _m: _release("mirror", "0.6.10"), fail)
        self.assertEqual(info["source_used"], "mirror")
        self.assertFalse(info["github_reachable"])
        self.assertEqual(info["github_version"], "")

    def test_both_fail_raises_without_value(self):
        def fail_m(_m):
            raise RuntimeError("镜像失败")

        def fail_g(_r):
            raise RuntimeError("GitHub 失败")

        with self.assertRaises(RuntimeError) as ctx:
            _run(fail_m, fail_g)
        text = str(ctx.exception)
        self.assertIn("镜像失败", text)
        self.assertIn("GitHub 失败", text)

    def test_github_disabled_only_mirror(self):
        cfg = _cfg(github_enable=False)

        def never(_r):
            raise AssertionError("github_enable=false 时不应探测 GitHub")

        info = _run(lambda _m: _release("mirror", "0.6.10"), never, cfg)
        self.assertEqual(info["source_used"], "mirror")
        self.assertFalse(info["github_reachable"])


class GithubReleaseParseTest(unittest.TestCase):
    def test_missing_latest_json_and_sha256(self):
        api = {
            "tag_name": "v0.6.10",
            "name": "DeckMeido 0.6.10",
            "body": "notes",
            "html_url": "https://github.test/release",
            "published_at": "2026-11-01T00:00:00Z",
            "assets": [{
                "name": "deckmeido-0.6.10.tar.gz",
                "browser_download_url": "https://gh.test/tar",
            }],
        }

        def fake_json(url, timeout=10.0, context=None):
            if url.endswith("/releases/latest"):
                return api
            raise AssertionError(f"不该请求 {url}")

        with mock.patch.object(updater, "_http_json", side_effect=fake_json):
            info = updater.github_release("example/deckmeido")
        self.assertEqual(info["tag"], "0.6.10")
        self.assertEqual(info["tarball_url"], "https://gh.test/tar")
        self.assertEqual(info["sha256"], "")

    def test_latest_json_asset_supplies_sha(self):
        latest = {"files": {"tarball": "deckmeido-0.6.10.tar.gz",
                            "sha256": "a" * 64}}
        api = {
            "tag_name": "0.6.10",
            "assets": [
                {"name": "latest.json", "browser_download_url": "https://gh.test/latest.json"},
                {"name": "deckmeido-0.6.10.tar.gz",
                 "browser_download_url": "https://gh.test/tar"},
            ],
        }

        def fake_json(url, timeout=10.0, context=None):
            if url.endswith("/releases/latest"):
                return api
            if url.endswith("latest.json"):
                return latest
            raise AssertionError(url)

        with mock.patch.object(updater, "_http_json", side_effect=fake_json):
            info = updater.github_release("example/deckmeido")
        self.assertEqual(info["sha256"], "a" * 64)
        self.assertEqual(info["tarball_url"], "https://gh.test/tar")

    def test_sha256_asset_is_parsed(self):
        api = {
            "tag_name": "0.6.10",
            "assets": [
                {"name": "deckmeido-0.6.10.tar.gz",
                 "browser_download_url": "https://gh.test/tar"},
                {"name": "deckmeido-0.6.10.tar.gz.sha256",
                 "browser_download_url": "https://gh.test/tar.sha256"},
            ],
        }

        def fake_json(url, timeout=10.0, context=None):
            if url.endswith("/releases/latest"):
                return api
            raise AssertionError(url)

        def fake_text(url, timeout=10.0, context=None):
            self.assertTrue(url.endswith(".sha256"))
            return "B" * 64 + "  deckmeido-0.6.10.tar.gz\n"

        with mock.patch.object(updater, "_http_json", side_effect=fake_json), \
                mock.patch.object(updater, "_http_text", side_effect=fake_text):
            info = updater.github_release("example/deckmeido")
        self.assertEqual(info["sha256"], "b" * 64)


class MirrorTlsTest(unittest.TestCase):
    def test_default_is_strict(self):
        with mock.patch.dict(os.environ, {updater.INSECURE_ENV: ""}):
            self.assertIsNone(updater._mirror_ssl_context("mirror.test"))
            self.assertFalse(updater._insecure_tls_allowed())

    def test_env_relaxes_only_when_set(self):
        with mock.patch.dict(os.environ, {updater.INSECURE_ENV: "1"}):
            ctx = updater._mirror_ssl_context("mirror.test")
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)
        self.assertFalse(ctx.check_hostname)


class ApplyFallbackTest(unittest.TestCase):
    def test_other_source_allowed_when_version_not_lower(self):
        probe = {"mirror": _release("mirror", "0.6.7"),
                 "github": _release("github", "0.6.7")}
        order = updater.apply_order("mirror", probe, "0.6.7", _cfg())
        self.assertEqual(order, ["mirror", "github"])

    def test_other_source_rejected_when_older(self):
        probe = {"mirror": _release("mirror", "0.6.10"),
                 "github": _release("github", "0.6.7")}
        order = updater.apply_order("mirror", probe, "0.6.10", _cfg())
        self.assertEqual(order, ["mirror"])

    def test_disabled_github_not_in_fallback(self):
        probe = {"mirror": _release("mirror", "0.6.10"),
                 "github": _release("github", "0.6.10")}
        order = updater.apply_order("mirror", probe, "0.6.10",
                                    _cfg(github_enable=False))
        self.assertEqual(order, ["mirror"])

    def test_sha_mismatch_aborts_download(self):
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b"not-a-tarball"

        info = {"tag": "0.6.10", "tarball_url": "https://mirror.test/x.tar.gz",
                "sha256": "0" * 64}
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(updater.urllib.request, "urlopen", return_value=_Resp()):
            with self.assertRaises(RuntimeError) as ctx:
                updater._apply_mirror(info, Path(td) / "update.log")
        self.assertIn("校验不通过", str(ctx.exception))

    def test_internal_sha256_sums(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            body = ("0" * 64 + "  deckmeido-0.6.10.tar.gz\n").encode()
            ti = tarfile.TarInfo("deckmeido-0.6.10/SHA256SUMS")
            ti.size = len(body)
            tf.addfile(ti, io.BytesIO(body))
        buf.seek(0)
        with tarfile.open(fileobj=buf, mode="r:gz") as tf:
            self.assertTrue(updater._sums_verified(tf, "0" * 64))
        buf.seek(0)
        with tarfile.open(fileobj=buf, mode="r:gz") as tf:
            with self.assertRaises(RuntimeError):
                updater._sums_verified(tf, "1" * 64)


if __name__ == "__main__":
    unittest.main()
