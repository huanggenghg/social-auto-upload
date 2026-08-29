import asyncio
import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import publish_all

from publish.errors import EXIT_AUTH_ERROR, EXIT_ALL_FAIL, EXIT_CONFIG_ERROR, EXIT_OK, EXIT_PARTIAL_FAIL
from publish.orchestrator import exit_code_from_results


class PublishCliPackagingTests(unittest.TestCase):
    def test_pyproject_exposes_opub_pointing_to_publish_all(self):
        pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
        py_modules_line = next(
            line for line in pyproject.splitlines() if line.startswith("py-modules = ")
        )

        self.assertIn('name = "opub"', pyproject)
        self.assertIn('opub = "publish_all:main"', pyproject)
        self.assertNotIn("opub_cli", pyproject)
        self.assertEqual(py_modules_line, 'py-modules = ["conf", "publish_all"]')

    def test_publish_all_exposes_build_parser_and_main(self):
        self.assertTrue(hasattr(publish_all, "build_parser"))
        self.assertTrue(hasattr(publish_all, "main"))

    def test_parser_prog_is_opub(self):
        parser = publish_all.build_parser()
        self.assertEqual(parser.prog, "opub")


class PublishCliParserTests(unittest.TestCase):
    def test_parser_defaults(self):
        parser = publish_all.build_parser()
        args = parser.parse_args([])

        self.assertFalse(hasattr(args, "config"))
        self.assertIsNone(args.platforms)
        self.assertIsNone(args.video)
        self.assertIsNone(args.images)
        self.assertFalse(args.note)
        self.assertFalse(args.convert_to_video)
        self.assertEqual(args.video_duration, 5)

    def test_parser_accepts_overrides(self):
        parser = publish_all.build_parser()
        args = parser.parse_args(
            [
                "--platforms", "douyin,weibo",
                "--video", "videos/demo.mp4",
                "--title", "标题",
                "--desc", "描述",
                "--tags", "标签1,标签2",
                "--schedule", "2026-05-30 21:30",
                "--start-from", "3",
                "--force",
            ]
        )

        self.assertEqual(args.platforms, "douyin,weibo")
        self.assertEqual(args.video, "videos/demo.mp4")
        self.assertEqual(args.title, "标题")
        self.assertEqual(args.desc, "描述")
        self.assertEqual(args.tags, "标签1,标签2")
        self.assertEqual(args.schedule.strftime("%Y-%m-%d %H:%M"), "2026-05-30 21:30")
        self.assertEqual(args.start_from, 3)
        self.assertTrue(args.force)

    def test_parser_rejects_unknown_subcommand(self):
        parser = publish_all.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["douyin", "upload-video"])

    def test_parser_accepts_note_mode_args(self):
        parser = publish_all.build_parser()
        args = parser.parse_args(
            [
                "--platforms", "xiaohongshu",
                "--note",
                "--images", "images/a.png,images/b.png",
                "--convert-to-video",
                "--video-duration", "8",
            ]
        )

        self.assertTrue(args.note)
        self.assertEqual(args.images, "images/a.png,images/b.png")
        self.assertTrue(args.convert_to_video)
        self.assertEqual(args.video_duration, 8)

    def test_parser_rejects_config_flag(self):
        parser = publish_all.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["--config", "my.ini"])

    def test_main_calls_run_publish_with_overrides(self):
        with patch("publish.orchestrator.run_publish", new=AsyncMock(return_value=0)) as run_publish:
            code = publish_all.main(["--platforms", "weibo", "--title", "标题"])

        self.assertEqual(code, 0)
        overrides = run_publish.await_args.args[0]
        self.assertEqual(overrides.platforms, "weibo")
        self.assertEqual(overrides.title, "标题")

    def test_main_wraps_exception_with_run001_and_exit_code_2(self):
        stderr = io.StringIO()
        with patch("publish.orchestrator.run_publish", new=AsyncMock(side_effect=RuntimeError("boom"))):
            with contextlib.redirect_stderr(stderr):
                code = publish_all.main([])

        self.assertEqual(code, EXIT_ALL_FAIL)
        self.assertIn("RUN-001", stderr.getvalue())
        self.assertIn("boom", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


class PublishCliVersionTests(unittest.TestCase):
    def test_version_flag_prints_version_and_exits_zero(self):
        parser = publish_all.build_parser()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as ctx:
                parser.parse_args(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertRegex(stdout.getvalue(), r"^opub \d+\.\d+\.\d+")


class PublishCliConfigErrorTests(unittest.TestCase):
    def _run(self, params):
        coro = publish_all.run_publish_with_params(params)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with contextlib.redirect_stderr(stderr):
                code = asyncio.run(coro)
        return code, stderr.getvalue()

    def test_no_enabled_platforms_returns_config_error(self):
        params = publish_all.default_params_from_overrides()
        code, stderr = self._run(params)
        self.assertEqual(code, EXIT_CONFIG_ERROR)
        self.assertIn("CFG-002", stderr)

    def test_note_mode_without_images_returns_config_error(self):
        params = publish_all.default_params_from_overrides()
        params.update(content_type="note", enabled_platforms=["douyin"], images=[])
        code, stderr = self._run(params)
        self.assertEqual(code, EXIT_CONFIG_ERROR)
        self.assertIn("CFG-004", stderr)

    def test_video_mode_missing_video_returns_config_error(self):
        params = publish_all.default_params_from_overrides()
        params.update(content_type="video", enabled_platforms=["douyin"], video_file="no_such_video.mp4")
        code, stderr = self._run(params)
        self.assertEqual(code, EXIT_CONFIG_ERROR)
        self.assertIn("CFG-003", stderr)

    def test_convert_to_video_without_images_returns_config_error(self):
        params = publish_all.default_params_from_overrides()
        params.update(content_type="note", convert_to_video=True, enabled_platforms=["douyin"], images=[])
        code, stderr = self._run(params)
        self.assertEqual(code, EXIT_CONFIG_ERROR)
        self.assertIn("CFG-004", stderr)


class ExitCodeFromResultsTests(unittest.TestCase):
    def test_all_success(self):
        results = {"v.mp4": {"douyin": {"success": True}}}
        self.assertEqual(exit_code_from_results(results), EXIT_OK)

    def test_partial_fail(self):
        results = {"v.mp4": {"douyin": {"success": True}, "weibo": {"success": False, "message": "x"}}}
        self.assertEqual(exit_code_from_results(results), EXIT_PARTIAL_FAIL)

    def test_all_fail_platform_error(self):
        results = {"v.mp4": {"weibo": {"success": False, "message": "x"}}}
        self.assertEqual(exit_code_from_results(results), EXIT_ALL_FAIL)

    def test_all_fail_account_issues_is_auth_error(self):
        results = {"v.mp4": {"weibo": {"success": False, "message": "登录失败", "account_issue": True}}}
        self.assertEqual(exit_code_from_results(results), EXIT_AUTH_ERROR)


class PublishCliHelpTextTests(unittest.TestCase):
    def test_help_documents_cli_surface(self):
        help_text = publish_all.build_parser().format_help()
        for fragment in ["--platforms", "--video", "--note", "--images", "--convert-to-video", "--video-duration", "--schedule"]:
            self.assertIn(fragment, help_text)
        self.assertNotIn("publish_config.ini", help_text)
        self.assertNotIn("[common]", help_text)

    def test_help_does_not_invite_omitting_user_content(self):
        help_text = publish_all.build_parser().format_help()
        for fragment in ["留空则自动生成", "留空则尝试自动生成"]:
            self.assertNotIn(fragment, help_text, f"--help 不应用 '{fragment}' 诱导 agent 不向用户收集文案")
        self.assertIn("由用户提供", help_text)


class SkillDocBlackboxTests(unittest.TestCase):
    SKILL_PATH = Path("skills/opub-cli/SKILL.md")

    def test_no_repo_references(self):
        text = self.SKILL_PATH.read_text(encoding="utf-8")
        for forbidden in ["-e .", "conf.example.py", "pyproject.toml", "requirements.txt", "publish_all", "uv pip"]:
            self.assertNotIn(forbidden, text, f"SKILL.md 不应包含仓库实现细节: {forbidden}")

    def test_documents_exit_codes_and_install(self):
        text = self.SKILL_PATH.read_text(encoding="utf-8")
        self.assertIn("pip install opub", text)
        for code_doc in ["10", "11", "12", "CFG-", "ENV-", "AUTH-", "PUB-"]:
            self.assertIn(code_doc, text)

    def test_publish_inputs_must_come_from_user(self):
        text = self.SKILL_PATH.read_text(encoding="utf-8")
        self.assertIn("逐项向用户确认", text, "SKILL.md 必须要求 agent 执行前向用户逐项确认发布输入")
        self.assertIn("不要自行检索文件系统", text, "SKILL.md 必须禁止 agent 自行检索文件系统挑素材")
        self.assertIn("仅当用户明确", text, "自动生成只允许在用户明确授权时使用")

    def test_agent_hides_publish_process_logs_and_reports_only_milestones(self):
        text = self.SKILL_PATH.read_text(encoding="utf-8")

        self.assertIn("用户可见反馈仅限以下三类", text)
        for milestone in ["发布环境状态", "发布开始", "发布结果"]:
            self.assertIn(milestone, text)
        self.assertIn("stdout 和 stderr 重定向到 Agent 内部临时日志", text)
        self.assertIn("禁止向用户展示或转述", text)
        self.assertIn("不发送发布进度", text)
        for retained_result in ["错误码", "结果链接", "总体计数"]:
            self.assertIn(retained_result, text)
        self.assertNotIn("--agent-mode", text)


class PublicSingleAccountContractTests(unittest.TestCase):
    DOC_PATHS = [
        Path("README.md"),
        Path("AGENT.md"),
        Path("skills/opub-cli/SKILL.md"),
    ]

    def test_docs_document_one_canonical_account_per_platform(self):
        for path in self.DOC_PATHS:
            with self.subTest(path=path):
                text = path.read_text(encoding="utf-8")
                self.assertIn("每个平台只自动发现一个规范账号文件", text)
                self.assertNotIn("微博多账号", text)
                self.assertNotIn("每个账号各发一遍", text)


if __name__ == "__main__":
    unittest.main()
