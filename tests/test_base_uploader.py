from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from uploader.base_video import (
    AccountRestrictedError,
    BaseBrowserUploader,
    BaseCliUploader,
    BasePlatformUploader,
    build_login_expired_result,
    PlatformResult,
    PlatformResultExtras,
    PublishStrategy,
)


class PublishStrategyTests(unittest.TestCase):
    def test_immediate_value(self):
        self.assertEqual(PublishStrategy.IMMEDIATE.value, "immediate")

    def test_scheduled_value(self):
        self.assertEqual(PublishStrategy.SCHEDULED.value, "scheduled")

    def test_str_subclass_for_backward_compat(self):
        self.assertEqual(PublishStrategy.IMMEDIATE, "immediate")
        self.assertEqual(PublishStrategy.SCHEDULED, "scheduled")


class PlatformResultTypedDictTests(unittest.TestCase):
    def test_minimal_result(self):
        r: PlatformResult = {"success": True, "message": "ok"}
        self.assertTrue(r["success"])

    def test_extras_with_all_fields(self):
        r: PlatformResultExtras = {
            "success": False,
            "message": "limited",
            "result_url": "https://example.com/v/1",
            "result_id": "abc123",
            "account_issue": True,
            "issue_type": "publish_restricted",
        }
        self.assertEqual(r["result_url"], "https://example.com/v/1")
        self.assertEqual(r["issue_type"], "publish_restricted")


class AccountRestrictedErrorTests(unittest.TestCase):
    def test_is_exception_subclass(self):
        self.assertTrue(issubclass(AccountRestrictedError, Exception))

    def test_carries_message(self):
        exc = AccountRestrictedError("风控限制")
        self.assertEqual(str(exc), "风控限制")


class LoginExpiredResultTests(unittest.TestCase):
    def test_builds_exact_default_safe_retry_result(self):
        self.assertEqual(
            build_login_expired_result(),
            {
                "success": False,
                "message": "cookie 已失效，请重新扫码登录",
                "account_issue": True,
                "issue_type": "login_expired",
                "safe_to_retry": True,
            },
        )


class ValidateBaseArgsTests(unittest.TestCase):
    def test_video_file_missing_returns_error(self):
        params = {"content_type": "video", "video_file": "/nonexistent/x.mp4"}
        result = BasePlatformUploader.validate_base_args(params)
        self.assertIsNotNone(result)
        self.assertFalse(result["success"])
        self.assertIn("视频文件不存在", result["message"])

    def test_video_file_none_returns_error(self):
        params = {"content_type": "video", "video_file": ""}
        result = BasePlatformUploader.validate_base_args(params)
        self.assertIsNotNone(result)
        self.assertIn("视频文件不存在", result["message"])

    def test_note_without_images_returns_error(self):
        params = {"content_type": "note", "images": []}
        result = BasePlatformUploader.validate_base_args(params)
        self.assertIsNotNone(result)
        self.assertIn("图文模式需要提供图片", result["message"])

    def test_note_with_missing_image_file_returns_error(self):
        params = {"content_type": "note", "images": ["/nonexistent/a.jpg"]}
        result = BasePlatformUploader.validate_base_args(params)
        self.assertIsNotNone(result)
        self.assertIn("图片文件不存在", result["message"])

    def test_valid_video_params_returns_none(self):
        import tempfile
        import os
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b"fake")
            path = f.name
        try:
            params = {"content_type": "video", "video_file": path}
            self.assertIsNone(BasePlatformUploader.validate_base_args(params))
        finally:
            os.unlink(path)

    def test_valid_note_params_returns_none(self):
        import tempfile
        import os
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"fake")
            path = f.name
        try:
            params = {"content_type": "note", "images": [path]}
            self.assertIsNone(BasePlatformUploader.validate_base_args(params))
        finally:
            os.unlink(path)


class ValidateVideoFileTests(unittest.TestCase):
    def test_existing_validation_preserved(self):
        # validate_video_file inherited from BasePlatformUploader, must still work
        import tempfile
        import os
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b"fake")
            path = f.name
        try:
            resolved = BasePlatformUploader.validate_video_file(path)
            self.assertEqual(resolved.suffix, ".mp4")
        finally:
            os.unlink(path)

    def test_unsupported_extension_raises(self):
        import tempfile
        import os
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"fake")
            path = f.name
        try:
            with self.assertRaises(ValueError):
                BasePlatformUploader.validate_video_file(path)
        finally:
            os.unlink(path)


class ValidatePublishDateTests(unittest.TestCase):
    def test_zero_returns_zero(self):
        self.assertEqual(BasePlatformUploader.validate_publish_date(0), 0)

    def test_none_returns_zero(self):
        self.assertEqual(BasePlatformUploader.validate_publish_date(None), 0)

    def test_past_datetime_raises(self):
        past = datetime(2020, 1, 1)
        with self.assertRaises(ValueError):
            BasePlatformUploader.validate_publish_date(past)


if __name__ == "__main__":
    unittest.main()
