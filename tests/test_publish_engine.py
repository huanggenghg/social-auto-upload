import asyncio
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import publish_all
from conf import BASE_DIR
from publish import orchestrator
from publish.errors import EXIT_CONFIG_ERROR


class TitleGuardTests(unittest.TestCase):
    def test_video_mode_empty_title_after_resolution_returns_config_error(self):
        params = {
            "content_type": "video",
            "title": "",
            "desc": "",
            "tags": [],
            "video_file": "videos/demo.mp4",
            "images": [],
            "publish_strategy": "immediate",
            "publish_time": None,
            "enabled_platforms": ["weibo"],
            "platforms": {"weibo_account": "cookies/weibo_1.json"},
            "convert_to_video": False,
            "video_duration": 5,
            "start_from": 1,
        }

        with patch("publish.orchestrator.runtime_preflight", new=AsyncMock(return_value=True)), \
             patch("publish.orchestrator.get_video_files", return_value=["videos/demo.mp4"]), \
             patch("publish.orchestrator.get_video_content", return_value=("", "")), \
             patch("publish.orchestrator.publish_one_item", new=AsyncMock()) as publish_one_item:
            code = asyncio.run(publish_all.run_publish_with_params(params))

        self.assertEqual(code, EXIT_CONFIG_ERROR)
        publish_one_item.assert_not_awaited()

    def test_note_mode_empty_title_returns_config_error(self):
        params = {
            "content_type": "note",
            "title": "",
            "desc": "",
            "tags": [],
            "video_file": "",
            "images": ["videos/demo.png"],
            "publish_strategy": "immediate",
            "publish_time": None,
            "enabled_platforms": ["weibo"],
            "platforms": {"weibo_account": "cookies/weibo_1.json"},
            "convert_to_video": False,
            "video_duration": 5,
            "start_from": 1,
        }

        with patch("publish.orchestrator.runtime_preflight", new=AsyncMock(return_value=True)), \
             patch("publish.orchestrator.fill_empty_content", return_value=("", "")), \
             patch("publish.orchestrator.publish_one_item", new=AsyncMock()) as publish_one_item:
            code = asyncio.run(publish_all.run_publish_with_params(params))

        self.assertEqual(code, EXIT_CONFIG_ERROR)
        publish_one_item.assert_not_awaited()


class PublishEngineTests(unittest.TestCase):
    def test_default_params_from_overrides_builds_full_params(self):
        publish_time = publish_all.datetime.strptime("2026-05-30 21:30", "%Y-%m-%d %H:%M")
        overrides = publish_all.PublishOverrides(
            platforms="douyin,weibo",
            video="videos/demo.mp4",
            title="标题",
            desc="描述",
            tags="标签1,标签2",
            schedule=publish_time,
            start_from=3,
            force=True,
        )

        params = publish_all.default_params_from_overrides(overrides)

        self.assertEqual(params["content_type"], "video")
        self.assertEqual(params["enabled_platforms"], ["douyin", "weibo"])
        self.assertEqual(params["video_file"], "videos/demo.mp4")
        self.assertEqual(params["title"], "标题")
        self.assertEqual(params["desc"], "描述")
        self.assertEqual(params["tags"], ["标签1", "标签2"])
        self.assertEqual(params["publish_strategy"], "scheduled")
        self.assertEqual(params["publish_time"], publish_time)
        self.assertEqual(params["start_from"], 3)
        self.assertTrue(params["force"])
        self.assertFalse(params["convert_to_video"])
        self.assertEqual(params["video_duration"], 5)

    def test_default_params_from_overrides_note_mode(self):
        overrides = publish_all.PublishOverrides(
            platforms="xiaohongshu",
            note=True,
            images="images/a.png, images/b.png",
            convert_to_video=True,
            video_duration=8,
        )

        params = publish_all.default_params_from_overrides(overrides)

        self.assertEqual(params["content_type"], "note")
        self.assertEqual(params["images"], ["images/a.png", "images/b.png"])
        self.assertEqual(params["video_file"], "")
        self.assertTrue(params["convert_to_video"])
        self.assertEqual(params["video_duration"], 8)
        self.assertEqual(params["publish_strategy"], "immediate")
        self.assertIsNone(params["publish_time"])

    def test_default_params_from_overrides_without_overrides_uses_defaults(self):
        params = publish_all.default_params_from_overrides()

        self.assertEqual(params["content_type"], "video")
        self.assertEqual(params["enabled_platforms"], [])
        self.assertEqual(params["video_file"], "")
        self.assertEqual(params["start_from"], 1)
        self.assertFalse(params["convert_to_video"])

    def test_run_publish_with_params_passes_force_to_content_generation(self):
        params = {
            "content_type": "video",
            "title": "标题",
            "desc": "描述",
            "tags": [],
            "video_file": "videos/demo.mp4",
            "images": [],
            "publish_strategy": "immediate",
            "publish_time": None,
            "enabled_platforms": ["weibo"],
            "platforms": {},
            "convert_to_video": False,
            "video_duration": 5,
            "start_from": 1,
            "force": True,
        }

        with patch("publish.orchestrator.runtime_preflight", new=AsyncMock(return_value=True)):
            with patch("publish.orchestrator.get_video_files", return_value=["videos/demo.mp4"]):
                with patch("publish.orchestrator.get_video_content", return_value=("标题", "描述")) as get_video_content:
                    with patch("publish.orchestrator.publish_one_item", new=AsyncMock(return_value={"weibo": {"success": True}})) as publish_one_item:
                        code = asyncio.run(publish_all.run_publish_with_params(params))

        self.assertEqual(code, 0)
        get_video_content.assert_called_once_with(
            "videos/demo.mp4",
            "标题",
            "描述",
            force=True,
        )
        publish_one_item.assert_awaited_once()

    def test_run_publish_with_params_skips_preflight_when_video_missing(self):
        params = {
            "content_type": "video",
            "title": "标题",
            "desc": "描述",
            "tags": [],
            "video_file": "videos/missing.mp4",
            "images": [],
            "publish_strategy": "immediate",
            "publish_time": None,
            "enabled_platforms": ["weibo"],
            "platforms": {},
            "convert_to_video": False,
            "video_duration": 5,
            "start_from": 1,
        }

        with patch("publish.orchestrator.runtime_preflight", new=AsyncMock(return_value=True)) as preflight:
            code = asyncio.run(publish_all.run_publish_with_params(params))

        self.assertEqual(code, 10)
        preflight.assert_not_awaited()

    def test_run_publish_with_params_note_mode_without_conversion_publishes_via_images(self):
        params = {
            "content_type": "note",
            "title": "标题",
            "desc": "描述",
            "tags": [],
            "video_file": "",
            "images": ["videos/demo.png"],
            "publish_strategy": "immediate",
            "publish_time": None,
            "enabled_platforms": ["kuaishou"],
            "platforms": {},
            "convert_to_video": False,
            "video_duration": 5,
            "start_from": 1,
        }

        with patch("publish.orchestrator.runtime_preflight", new=AsyncMock(return_value=True)):
            with patch("publish.orchestrator.get_video_files", return_value=[]) as get_video_files:
                with patch("publish.orchestrator.publish_one_item", new=AsyncMock(return_value={"kuaishou": {"success": True, "message": "ok"}})) as publish_one_item:
                    code = asyncio.run(publish_all.run_publish_with_params(params))

        self.assertEqual(code, 0)
        get_video_files.assert_not_called()
        publish_one_item.assert_awaited_once()
        called_params = publish_one_item.call_args.args[0]
        self.assertEqual(called_params["images"], ["videos/demo.png"])
        self.assertEqual(called_params["content_type"], "note")

    def test_run_publish_with_params_note_mode_without_images_returns_error(self):
        params = {
            "content_type": "note",
            "title": "标题",
            "desc": "描述",
            "tags": [],
            "video_file": "",
            "images": [],
            "publish_strategy": "immediate",
            "publish_time": None,
            "enabled_platforms": ["kuaishou"],
            "platforms": {},
            "convert_to_video": False,
            "video_duration": 5,
            "start_from": 1,
        }

        with patch("publish.orchestrator.runtime_preflight", new=AsyncMock(return_value=True)) as preflight:
            code = asyncio.run(publish_all.run_publish_with_params(params))

        self.assertEqual(code, 10)
        preflight.assert_not_awaited()

    def test_publish_to_douyin_marks_restriction_as_account_issue(self):
        params = {
            "account_file": "cookies/douyin_uploader/account.json",
            "title": "标题",
            "tags": [],
            "publish_strategy": "immediate",
            "publish_time": None,
            "content_type": "video",
            "video_file": "videos/demo.mp4",
            "desc": "",
        }

        restriction_result = {
            "success": False,
            "message": "账号被限制发布: 作品发布失败，健康分不足投稿功能受限",
            "account_issue": True,
            "issue_type": "publish_restricted",
        }

        with patch("uploader.douyin_uploader.main.DouYinVideo") as MockDouYinVideo:
            MockDouYinVideo.validate_base_args.return_value = None
            MockDouYinVideo.return_value.upload = AsyncMock(return_value=restriction_result)
            result = publish_all.run_async_for_test(publish_all.publish_to_douyin(params))

        self.assertFalse(result["success"])
        self.assertTrue(result["account_issue"])
        self.assertEqual(result["issue_type"], "publish_restricted")
        self.assertIn("健康分不足", result["message"])


class RunPublishValidationTests(unittest.TestCase):
    def _stderr_code(self, overrides):
        import contextlib, io
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = publish_all.run_publish_sync(overrides)
        return code, stderr.getvalue()

    def test_missing_platforms_returns_config_error(self):
        code, stderr = self._stderr_code(publish_all.PublishOverrides(video="videos/demo.mp4"))
        self.assertEqual(code, 10)
        self.assertIn("CFG-002", stderr)

    def test_missing_video_and_note_returns_config_error(self):
        code, stderr = self._stderr_code(publish_all.PublishOverrides(platforms="weibo"))
        self.assertEqual(code, 10)
        self.assertIn("CFG-001", stderr)

    def test_note_with_video_returns_config_error(self):
        code, stderr = self._stderr_code(
            publish_all.PublishOverrides(platforms="weibo", video="v.mp4", note=True, images="a.png")
        )
        self.assertEqual(code, 10)
        self.assertIn("CFG-001", stderr)

    def test_run_publish_builds_params_and_calls_engine(self):
        overrides = publish_all.PublishOverrides(
            platforms="weibo", video="videos/demo.mp4", title="标题"
        )
        with patch("publish.orchestrator.run_publish_with_params", new=AsyncMock(return_value=0)) as run_params:
            code = publish_all.run_publish_sync(overrides)

        self.assertEqual(code, 0)
        params = run_params.await_args.args[0]
        self.assertEqual(params["enabled_platforms"], ["weibo"])
        self.assertEqual(params["video_file"], "videos/demo.mp4")
        self.assertEqual(params["title"], "标题")


class RuntimePreflightTests(unittest.TestCase):
    def test_playwright_browser_cache_dirs_include_windows_default(self):
        fake_home = Path("/Users/example")
        with patch.dict("publish_all.os.environ", {}, clear=True), \
             patch("publish_all.Path.home", return_value=fake_home):
            cache_dirs = publish_all.playwright_browser_cache_dirs()

        self.assertIn(fake_home / "AppData" / "Local" / "ms-playwright", cache_dirs)

    def test_runtime_preflight_installs_missing_chromium(self):
        with patch("publish.runtime.patchright_available", return_value=True), \
             patch("publish.runtime.patchright_chromium_installed", return_value=False), \
             patch("publish.runtime.install_patchright_chromium", return_value=True) as install, \
             patch("publish.runtime.sync_python_dependencies", return_value=True):
            ok = publish_all.run_async_for_test(publish_all.runtime_preflight())

        self.assertTrue(ok)
        install.assert_called_once()

    def test_runtime_preflight_fails_when_chromium_install_fails(self):
        with patch("publish.runtime.patchright_available", return_value=True), \
             patch("publish.runtime.patchright_chromium_installed", return_value=False), \
             patch("publish.runtime.install_patchright_chromium", return_value=False), \
             patch("publish.runtime.sync_python_dependencies", return_value=True):
            ok = publish_all.run_async_for_test(publish_all.runtime_preflight())

        self.assertFalse(ok)

    def test_runtime_preflight_fails_without_patchright_and_does_not_install(self):
        with patch("publish.runtime.patchright_available", return_value=False), \
             patch("publish.runtime.install_patchright_chromium", return_value=True) as install, \
             patch("publish.runtime.sync_python_dependencies", return_value=True) as sync:
            ok = publish_all.run_async_for_test(publish_all.runtime_preflight())

        self.assertFalse(ok)
        install.assert_not_called()
        sync.assert_not_called()

    def test_runtime_preflight_fails_when_dep_sync_fails(self):
        with patch("publish.runtime.patchright_available", return_value=True), \
             patch("publish.runtime.sync_python_dependencies", return_value=False) as sync, \
             patch("publish.runtime.patchright_chromium_installed", return_value=True) as chromium_check:
            ok = publish_all.run_async_for_test(publish_all.runtime_preflight())

        self.assertFalse(ok)
        sync.assert_called_once()
        chromium_check.assert_not_called()

    def test_sync_python_dependencies_calls_pip_install_with_requirements_path(self):
        with patch("publish.runtime.subprocess.run") as run, \
             patch("publish.runtime.Path.exists", return_value=True):
            run.return_value.returncode = 0

            ok = publish_all.sync_python_dependencies()

        self.assertTrue(ok)
        args = run.call_args.args[0]
        self.assertEqual(args[0:3], [sys.executable, "-m", "pip"])
        self.assertIn("install", args)
        self.assertTrue(any("requirements.txt" in str(a) for a in args))

    def test_sync_python_dependencies_returns_true_when_requirements_missing(self):
        with patch("publish.runtime.Path.exists", return_value=False), \
             patch("publish.runtime.subprocess.run") as run:
            ok = publish_all.sync_python_dependencies()

        self.assertTrue(ok)
        run.assert_not_called()

    def test_install_patchright_chromium_defaults_to_playwright_cdn_for_chromium(self):
        with patch.dict("publish_all.os.environ", {}, clear=True):
            with patch("publish_all.subprocess.run") as run:
                run.return_value.returncode = 0

                ok = publish_all.install_patchright_chromium()

        self.assertTrue(ok)
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST"], "https://cdn.playwright.dev")
        self.assertNotIn("PLAYWRIGHT_DOWNLOAD_HOST", env)

    def test_install_patchright_chromium_preserves_user_download_host(self):
        with patch.dict("publish_all.os.environ", {"PLAYWRIGHT_DOWNLOAD_HOST": "https://mirror.example/playwright"}, clear=True):
            with patch("publish_all.subprocess.run") as run:
                run.return_value.returncode = 0

                ok = publish_all.install_patchright_chromium()

        self.assertTrue(ok)
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["PLAYWRIGHT_DOWNLOAD_HOST"], "https://mirror.example/playwright")
        self.assertNotIn("PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST", env)


class AccountLoginFlowTests(unittest.TestCase):
    @staticmethod
    def _weibo_params(account_file="cookies/weibo.json"):
        return {
            "enabled_platforms": ["weibo"],
            "platforms": {"weibo_account": account_file},
            "content_type": "video",
            "video_file": "videos/demo.mp4",
            "title": "标题",
            "desc": "描述",
            "tags": [],
            "publish_strategy": "immediate",
            "publish_time": None,
            "convert_to_video": False,
        }

    def test_legacy_comma_account_uses_default_once_without_account_numbering(self):
        params = self._weibo_params("cookies/weibo_1.json, cookies/weibo_2.json")
        expected_default = str(Path(BASE_DIR) / "cookies" / "weibo_uploader" / "account.json")

        with patch("publish.orchestrator.ensure_account_login", new=AsyncMock(return_value=True)) as ensure_login, \
             patch("publish.orchestrator.publish_to_platform", new=AsyncMock(return_value={"success": True, "message": "发布成功"})) as publish, \
             contextlib.redirect_stdout(io.StringIO()) as stdout:
            results = publish_all.run_async_for_test(publish_all.publish_one_item(params))

        ensure_login.assert_awaited_once_with("weibo", expected_default)
        publish.assert_awaited_once()
        self.assertEqual(set(results), {"weibo"})
        self.assertNotIn("账号 1/", stdout.getvalue())

    def test_duplicate_enabled_platform_publishes_once_with_unique_header_total(self):
        params = self._weibo_params()
        params["enabled_platforms"] = ["weibo", "weibo"]

        with patch("publish.orchestrator.ensure_account_login", new=AsyncMock(return_value=True)) as ensure_login, \
             patch("publish.orchestrator.publish_to_platform", new=AsyncMock(return_value={"success": True, "message": "发布成功"})) as publish, \
             contextlib.redirect_stdout(io.StringIO()) as stdout:
            results = publish_all.run_async_for_test(publish_all.publish_one_item(params))

        ensure_login.assert_awaited_once_with("weibo", "cookies/weibo.json")
        publish.assert_awaited_once()
        self.assertEqual(set(results), {"weibo"})
        self.assertIn("启用平台: weibo", stdout.getvalue())
        self.assertIn("[1/1] 发布到 微博", stdout.getvalue())
        self.assertNotIn("[2/2]", stdout.getvalue())

    def test_safe_login_expiry_forces_one_login_and_republishes_successfully(self):
        params = self._weibo_params()
        first_result = {
            "success": False,
            "message": "登录已过期",
            "account_issue": True,
            "issue_type": "login_expired",
            "safe_to_retry": True,
        }
        final_result = {"success": True, "message": "发布成功"}

        with patch("publish.orchestrator.ensure_account_login", new=AsyncMock(side_effect=[True, True])) as ensure_login, \
             patch("publish.orchestrator.publish_to_platform", new=AsyncMock(side_effect=[first_result, final_result])) as publish:
            results = publish_all.run_async_for_test(publish_all.publish_one_item(params))

        self.assertEqual(ensure_login.await_count, 2)
        self.assertEqual(ensure_login.await_args_list[0].args, ("weibo", "cookies/weibo.json"))
        self.assertEqual(ensure_login.await_args_list[1].args, ("weibo", "cookies/weibo.json"))
        self.assertEqual(ensure_login.await_args_list[1].kwargs, {"force": True})
        self.assertEqual(publish.await_count, 2)
        self.assertEqual(publish.await_args_list[0].args, publish.await_args_list[1].args)
        self.assertTrue(results["weibo"]["success"])

    def test_forced_login_failure_replaces_expiry_result_with_auth_failure(self):
        params = self._weibo_params()
        expiry = {
            "success": False,
            "message": "登录已过期",
            "account_issue": True,
            "issue_type": "login_expired",
            "safe_to_retry": True,
        }

        with patch("publish.orchestrator.ensure_account_login", new=AsyncMock(side_effect=[True, False])) as ensure_login, \
             patch("publish.orchestrator.publish_to_platform", new=AsyncMock(return_value=expiry)) as publish, \
             contextlib.redirect_stdout(io.StringIO()) as stdout:
            results = publish_all.run_async_for_test(publish_all.publish_one_item(params))

        self.assertEqual(ensure_login.await_count, 2)
        self.assertEqual(ensure_login.await_args_list[1].kwargs, {"force": True})
        publish.assert_awaited_once()
        self.assertFalse(results["weibo"]["success"])
        self.assertEqual(results["weibo"]["error_code"], "AUTH-001")
        self.assertNotIn("❌ 失败: 登录失败: 微博", stdout.getvalue())

    def test_second_safe_login_expiry_does_not_trigger_a_third_publish_or_login(self):
        params = self._weibo_params()
        expiry = {
            "success": False,
            "message": "登录已过期",
            "account_issue": True,
            "issue_type": "login_expired",
            "safe_to_retry": True,
        }

        with patch("publish.orchestrator.ensure_account_login", new=AsyncMock(side_effect=[True, True])) as ensure_login, \
             patch("publish.orchestrator.publish_to_platform", new=AsyncMock(side_effect=[expiry, expiry])) as publish:
            results = publish_all.run_async_for_test(publish_all.publish_one_item(params))

        self.assertEqual(ensure_login.await_count, 2)
        self.assertEqual(publish.await_count, 2)
        self.assertEqual(results["weibo"], expiry)

    def test_non_retryable_login_expiry_does_not_force_login(self):
        params = self._weibo_params()
        expiry = {
            "success": False,
            "message": "登录已过期",
            "account_issue": True,
            "issue_type": "login_expired",
            "safe_to_retry": False,
        }

        with patch("publish.orchestrator.ensure_account_login", new=AsyncMock(return_value=True)) as ensure_login, \
             patch("publish.orchestrator.publish_to_platform", new=AsyncMock(return_value=expiry)) as publish:
            results = publish_all.run_async_for_test(publish_all.publish_one_item(params))

        ensure_login.assert_awaited_once_with("weibo", "cookies/weibo.json")
        publish.assert_awaited_once()
        self.assertEqual(results["weibo"], expiry)

    def test_safe_login_expiry_requires_explicit_false_success(self):
        common_fields = {
            "account_issue": True,
            "issue_type": "login_expired",
            "safe_to_retry": True,
        }

        self.assertFalse(orchestrator._is_safe_login_expiry(common_fields))
        self.assertFalse(orchestrator._is_safe_login_expiry({**common_fields, "success": None}))

    def test_publish_one_item_triggers_login_before_publish(self):
        params = {
            "enabled_platforms": ["douyin"],
            "platforms": {"douyin_account": "cookies/douyin.json"},
            "content_type": "video",
            "video_file": "videos/demo.mp4",
            "title": "标题",
            "desc": "描述",
            "tags": [],
            "publish_strategy": "immediate",
            "publish_time": None,
            "convert_to_video": False,
        }

        with patch("publish.orchestrator.ensure_account_login", new=AsyncMock(return_value=True)) as ensure_login, \
             patch("publish.orchestrator.publish_to_platform", new=AsyncMock(return_value={"success": True, "message": "发布成功"})) as publish:
            results = publish_all.run_async_for_test(publish_all.publish_one_item(params))

        ensure_login.assert_awaited_once_with("douyin", "cookies/douyin.json")
        publish.assert_awaited_once()
        self.assertTrue(results["douyin"]["success"])

    def test_publish_one_item_skips_publish_when_login_fails(self):
        params = {
            "enabled_platforms": ["douyin"],
            "platforms": {"douyin_account": "cookies/douyin.json"},
            "content_type": "video",
            "video_file": "videos/demo.mp4",
            "title": "标题",
            "desc": "描述",
            "tags": [],
            "publish_strategy": "immediate",
            "publish_time": None,
            "convert_to_video": False,
        }

        with patch("publish.orchestrator.ensure_account_login", new=AsyncMock(return_value=False)), \
             patch("publish.orchestrator.publish_to_platform", new=AsyncMock(return_value={"success": True, "message": "发布成功"})) as publish:
            results = publish_all.run_async_for_test(publish_all.publish_one_item(params))

        publish.assert_not_awaited()
        self.assertFalse(results["douyin"]["success"])
        self.assertIn("登录失败", results["douyin"]["message"])

    def test_publish_one_item_missing_account_file_triggers_login_with_default_path(self):
        params = {
            "enabled_platforms": ["douyin"],
            "platforms": {},
            "content_type": "video",
            "video_file": "videos/demo.mp4",
            "title": "标题",
            "desc": "描述",
            "tags": [],
            "publish_strategy": "immediate",
            "publish_time": None,
            "convert_to_video": False,
        }

        expected_default = str(Path(BASE_DIR) / "cookies" / "douyin_uploader" / "account.json")

        with patch("publish.orchestrator.ensure_account_login", new=AsyncMock(return_value=True)) as ensure_login, \
             patch("publish.orchestrator.publish_to_platform", new=AsyncMock(return_value={"success": True, "message": "发布成功"})) as publish:
            results = publish_all.run_async_for_test(publish_all.publish_one_item(params))

        ensure_login.assert_awaited_once_with("douyin", expected_default)
        publish.assert_awaited_once()
        self.assertTrue(results["douyin"]["success"])

    def test_publish_one_item_skips_login_for_unsupported_platforms(self):
        params = {
            "enabled_platforms": ["fake_platform"],
            "platforms": {},
            "content_type": "video",
            "video_file": "videos/demo.mp4",
            "title": "标题",
            "desc": "描述",
            "tags": [],
            "publish_strategy": "immediate",
            "publish_time": None,
            "convert_to_video": False,
        }

        with patch("publish.orchestrator.ensure_account_login", new=AsyncMock()) as ensure_login, \
             patch("publish.orchestrator.default_account_file") as default_account, \
             patch("publish.orchestrator.publish_to_platform", new=AsyncMock()) as publish:
            results = publish_all.run_async_for_test(publish_all.publish_one_item(params))

        ensure_login.assert_not_awaited()
        default_account.assert_not_called()
        publish.assert_not_awaited()
        self.assertFalse(results["fake_platform"]["success"])
        self.assertEqual(results["fake_platform"]["message"], "未知平台: fake_platform")


class EnsureLoginWarningTests(unittest.TestCase):
    def test_ensure_login_missing_account_prints_timeout_warning_before_setup(self):
        import contextlib
        import io

        from publish import dispatch

        fake_setup = AsyncMock(return_value=True)
        with tempfile.TemporaryDirectory() as tmp:
            account_file = str(Path(tmp) / "account.json")
            with patch.dict(
                "publish.dispatch._PLATFORM_LOGIN",
                {"fake_plat": ("fake_mod", "cookie_auth", "fake_setup")},
            ), patch("importlib.import_module") as import_module:
                import_module.return_value.cookie_auth = AsyncMock(return_value=True)
                import_module.return_value.fake_setup = fake_setup
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    ok = asyncio.run(dispatch.ensure_login("fake_plat", account_file))

        self.assertTrue(ok)
        fake_setup.assert_awaited_once_with(account_file, handle=True)
        self.assertIn("工具超时", stderr.getvalue())


class PublishFailurePolicyTests(unittest.TestCase):
    def test_publish_one_item_continues_after_platform_failure(self):
        params = {
            "enabled_platforms": ["douyin", "weibo"],
            "platforms": {
                "douyin_account": "cookies/douyin.json",
                "weibo_account": "cookies/weibo.json",
            },
            "content_type": "video",
            "video_file": "videos/demo.mp4",
            "title": "标题",
            "desc": "描述",
            "tags": [],
            "publish_strategy": "immediate",
            "publish_time": None,
            "convert_to_video": False,
        }

        async def fake_publish(platform, publish_params):
            if platform == "douyin":
                return {"success": False, "message": "发布失败"}
            return {"success": True, "message": "发布成功"}

        with patch("publish.orchestrator.ensure_account_login", new=AsyncMock(return_value=True)), \
             patch("publish.orchestrator.publish_to_platform", new=AsyncMock(side_effect=fake_publish)):
            results = publish_all.run_async_for_test(publish_all.publish_one_item(params))

        self.assertFalse(results["douyin"]["success"])
        self.assertTrue(results["weibo"]["success"])

    def test_run_publish_with_params_returns_all_fail_when_all_publish_fail(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            video_path = Path(tmp_dir) / "demo.mp4"
            video_path.write_bytes(b"video")
            params = {
                "enabled_platforms": ["douyin"],
                "platforms": {"douyin_account": "cookies/douyin.json"},
                "content_type": "video",
                "video_file": str(video_path),
                "images": [],
                "title": "标题",
                "desc": "描述",
                "tags": [],
                "publish_strategy": "immediate",
                "publish_time": None,
                "convert_to_video": False,
                "video_duration": 5,
                "start_from": 1,
            }

            with patch("publish.orchestrator.runtime_preflight", new=AsyncMock(return_value=True)), \
                 patch("publish.orchestrator.get_video_content", return_value=("标题", "描述")), \
                 patch("publish.orchestrator.publish_one_item", new=AsyncMock(return_value={"douyin": {"success": False, "message": "发布失败"}})) as publish_one_item:
                code = publish_all.run_async_for_test(publish_all.run_publish_with_params(params))

        publish_one_item.assert_awaited_once()
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
