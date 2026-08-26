import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from publish.config import _discover_account_files


class AccountDiscoveryTests(unittest.TestCase):
    def test_canonical_account_wins_over_legacy_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            cookies_dir = base_dir / "cookies"
            canonical = cookies_dir / "douyin_uploader" / "account.json"
            canonical.parent.mkdir(parents=True)
            canonical.touch()
            (cookies_dir / "douyin_legacy.json").touch()
            (canonical.parent / "old-account.json").touch()
            archive = canonical.parent / "archive"
            archive.mkdir()
            (archive / "archived-account.json").touch()

            with patch("publish.config.BASE_DIR", base_dir):
                accounts = _discover_account_files()

        self.assertEqual(accounts, {"douyin_account": "cookies/douyin_uploader/account.json"})

    def test_uses_only_legacy_file_when_canonical_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            legacy = base_dir / "cookies" / "weibo_uploader" / "legacy.json"
            legacy.parent.mkdir(parents=True)
            legacy.touch()

            with patch("publish.config.BASE_DIR", base_dir):
                accounts = _discover_account_files()

        self.assertEqual(accounts, {"weibo_account": "cookies/weibo_uploader/legacy.json"})

    def test_omits_platform_when_multiple_legacy_files_exist(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            cookies_dir = base_dir / "cookies"
            cookies_dir.mkdir()
            (cookies_dir / "xiaohongshu_flat.json").touch()
            uploader_dir = cookies_dir / "xiaohongshu_uploader"
            uploader_dir.mkdir()
            (uploader_dir / "legacy.json").touch()

            with patch("publish.config.BASE_DIR", base_dir):
                accounts = _discover_account_files()

        self.assertNotIn("xiaohongshu_account", accounts)
        self.assertNotIn(",", "".join(accounts.values()))


if __name__ == "__main__":
    unittest.main()
