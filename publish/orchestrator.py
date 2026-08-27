# -*- coding: utf-8 -*-
"""发布编排:单视频发布、整体流程、入口函数"""
import argparse
import asyncio
import os
import sys
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version as pkg_version
from typing import Any, Dict, Optional, Sequence

from publish.config import (
    PublishOverrides,
    default_account_file,
    default_params_from_overrides,
)
from publish.constants import PLATFORM_NAMES
from publish.content import fill_empty_content, get_video_content, get_video_files
from publish.dispatch import (
    ensure_account_login,
    platform_requires_account_login,
    publish_to_platform,
)
from publish.errors import (
    EXIT_ALL_FAIL,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_ENV_ERROR,
    EXIT_OK,
    EXIT_PARTIAL_FAIL,
    print_error,
)
from publish.reporter import print_header, print_results, print_summary
from publish.runtime import runtime_preflight


def exit_code_from_results(all_results: Dict[str, Dict[str, Any]]) -> int:
    results = [r for item_results in all_results.values() for r in item_results.values()]
    if not results:
        return EXIT_ALL_FAIL
    failures = [r for r in results if not r["success"]]
    if not failures:
        return EXIT_OK
    if len(failures) == len(results):
        if all(r.get("account_issue") for r in failures):
            return EXIT_AUTH_ERROR
        return EXIT_ALL_FAIL
    return EXIT_PARTIAL_FAIL


def _auth_failure(platform_name: str, login_error: Optional[str] = None) -> Dict[str, Any]:
    message = f"登录失败: {platform_name}"
    if login_error:
        message += f" - {login_error}"
    return {
        "success": False,
        "message": message,
        "account_issue": True,
        "error_code": "AUTH-001",
    }


def _is_safe_login_expiry(result: Dict[str, Any]) -> bool:
    return (
        result.get("success") is False
        and result.get("account_issue") is True
        and result.get("issue_type") == "login_expired"
        and result.get("safe_to_retry") is True
    )


async def publish_one_item(video_params: Dict[str, Any]) -> Dict[str, Any]:
    enabled_platforms = list(dict.fromkeys(video_params["enabled_platforms"]))
    if enabled_platforms != video_params["enabled_platforms"]:
        video_params = {**video_params, "enabled_platforms": enabled_platforms}

    print_header(video_params)

    results = {}
    total = len(enabled_platforms)

    for i, platform in enumerate(enabled_platforms, 1):
        platform_name = PLATFORM_NAMES.get(platform, platform)

        if platform not in PLATFORM_NAMES:
            result = {"success": False, "message": f"未知平台: {platform}"}
            results[platform] = result
            print(f"[{i}/{total}] 发布到 {platform_name}...")
            print(f"  ❌ 失败: {result['message']}")
            continue

        account_key = f"{platform}_account"
        account_file = str(video_params["platforms"].get(account_key, "") or "").strip()
        if "," in account_file:
            account_file = ""

        if not account_file:
            default_file = default_account_file(platform)
            if default_file is None:
                results[platform] = {
                    "success": False,
                    "message": f"未配置 {platform} 账号",
                    "account_issue": True,
                    "error_code": "AUTH-002",
                }
                print("  ❌ 失败: 未配置账号")
                continue
            print(f"  ℹ️ 未发现 {platform_name} 账号文件，将触发扫码登录: {default_file}")
            account_file = default_file

        print(f"[{i}/{total}] 发布到 {platform_name}...")
        platform_params = {
            **video_params,
            "account_file": account_file,
        }

        if platform_requires_account_login(platform):
            login_error = None
            try:
                login_ok = await ensure_account_login(platform, account_file)
            except Exception as exc:
                login_ok = False
                login_error = str(exc) or exc.__class__.__name__
            if not login_ok:
                result = _auth_failure(platform_name, login_error)
                results[platform] = result
                print_error("AUTH-001", result["message"], f"引导用户在弹出的浏览器中完成 {platform_name} 扫码登录后重试")
                continue

        result = await publish_to_platform(platform, platform_params)
        auth_failure_reported = False
        if _is_safe_login_expiry(result):
            login_error = None
            try:
                login_ok = await ensure_account_login(platform, account_file, force=True)
            except Exception as exc:
                login_ok = False
                login_error = str(exc) or exc.__class__.__name__
            if login_ok:
                result = await publish_to_platform(platform, platform_params)
            else:
                result = _auth_failure(platform_name, login_error)
                print_error("AUTH-001", result["message"], f"引导用户在弹出的浏览器中完成 {platform_name} 扫码登录后重试")
                auth_failure_reported = True

        results[platform] = result
        if auth_failure_reported:
            continue
        if result.get("success"):
            print("  ✅ 成功")
        else:
            print(f"  ❌ 失败: {result['message']}")

    print_results(results)
    return results


async def run_publish_with_params(params: Dict[str, Any]) -> int:
    if not params["enabled_platforms"]:
        print_error("CFG-002", "未配置启用平台", "提供 --platforms，逗号分隔平台标识")
        return EXIT_CONFIG_ERROR

    # 注意：标题为空时，会在视频处理流程中尝试自动生成；
    # 解析后仍为空则 CFG-001 报错（除 bilibili 外各平台都强制要求标题）

    # 处理图文转视频
    if params["content_type"] == "note" and params["convert_to_video"]:
        if not params["images"]:
            print_error("CFG-004", "图文转视频需要提供图片", "提供 --images 设置图片路径（英文逗号分隔）")
            return EXIT_CONFIG_ERROR

        print("正在将图片转换为视频...")
        try:
            from utils.image_to_video import convert_images_to_video_for_publish

            video_path = convert_images_to_video_for_publish(
                image_paths=params["images"],
                title=params["title"],
                duration=params["video_duration"],
            )
            # 更新参数，切换为视频模式
            params["content_type"] = "video"
            params["video_file"] = video_path
            print(f"[OK] 视频已生成: {video_path}\n")
        except Exception as e:
            print_error("ENV-005", f"图片转视频失败: {e}", "安装 ffmpeg 后重试（macOS: brew install ffmpeg; Ubuntu: sudo apt-get install ffmpeg）")
            return EXIT_ENV_ERROR

    # 图文模式(不转视频):不依赖 video_file,直接以 images 发布
    if params["content_type"] == "note":
        if not params["images"]:
            print_error("CFG-004", "图文模式需要提供图片", "提供 --images 设置图片路径（英文逗号分隔）")
            return EXIT_CONFIG_ERROR

        if not await runtime_preflight():
            return EXIT_ENV_ERROR

        title, desc = fill_empty_content(params["title"], params["desc"])
        if not (title and str(title).strip()):
            print_error("CFG-001", "图文发布缺少标题", "提供 --title（自动填充未生效时标题为必填）")
            return EXIT_CONFIG_ERROR
        note_params = {**params, "title": title, "desc": desc}
        print(f"\n========== 图文发布 ==========")
        print(f"标题: {title}")
        if params["tags"]:
            print(f"标签: {params['tags']}")
        print(f"图片数: {len(params['images'])}")
        print(f"启用平台: {', '.join(params['enabled_platforms'])}\n")

        all_results = {"note": await publish_one_item(note_params)}
        print_summary(all_results)
        return exit_code_from_results(all_results)

    # 获取视频文件列表
    video_files = get_video_files(params["video_file"])
    if not video_files:
        print_error("CFG-003", f"未找到视频文件: {params['video_file']}", "检查 --video 路径是否正确")
        return EXIT_CONFIG_ERROR

    if not await runtime_preflight():
        return EXIT_ENV_ERROR

    print(f"找到 {len(video_files)} 个视频文件:")
    for vf in video_files:
        print(f"  - {os.path.basename(vf)}")
    print()

    # 遍历每个视频文件进行发布
    all_results = {}
    start_from = params.get("start_from", 1)
    if start_from > 1:
        print(f"\n[SKIP] 从第 {start_from} 个视频开始发布（跳过前 {start_from - 1} 个）\n")

    for video_idx, video_file in enumerate(video_files, 1):
        # 跳过已发布的视频
        if video_idx < start_from:
            continue

        print(f"\n========== 视频 [{video_idx}/{len(video_files)}] ==========")
        print(f"文件: {os.path.basename(video_file)}")

        # 使用视频配置文件或默认配置/模板填充
        title, desc = get_video_content(
            video_file,
            params["title"],
            params["desc"],
            force=params.get("force", False),
        )

        if not (title and str(title).strip()):
            print_error(
                "CFG-001",
                f"视频 {os.path.basename(video_file)} 标题解析后为空",
                "提供 --title，或配置视频同名 JSON / ZHIPU_API_KEY 供自动生成",
            )
            return EXIT_CONFIG_ERROR

        # 更新参数
        video_params = {
            **params,
            "video_file": video_file,
            "title": title,
            "desc": desc,
        }

        all_results[video_file] = await publish_one_item(video_params)

    # 打印总体汇总
    print_summary(all_results)
    return exit_code_from_results(all_results)


async def run_publish(overrides: Optional[PublishOverrides] = None) -> int:
    overrides = overrides or PublishOverrides()

    if not overrides.platforms:
        print_error("CFG-002", "未指定启用平台", "提供 --platforms，逗号分隔平台标识（见 opub --help）")
        return EXIT_CONFIG_ERROR
    if overrides.note and overrides.video:
        print_error("CFG-001", "--note 与 --video 互斥", "二选一：图文用 --note --images，视频用 --video")
        return EXIT_CONFIG_ERROR
    if not overrides.note and not overrides.video:
        print_error("CFG-001", "缺少发布素材", "提供 --video（视频发布）或 --note --images（图文发布）")
        return EXIT_CONFIG_ERROR

    params = default_params_from_overrides(overrides)
    return await run_publish_with_params(params)


def run_publish_sync(overrides: Optional[PublishOverrides] = None) -> int:
    return asyncio.run(run_publish(overrides))


SCHEDULE_FORMAT = "%Y-%m-%d %H:%M"


def _schedule_value(value: str) -> datetime:
    try:
        return datetime.strptime(value, SCHEDULE_FORMAT)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid schedule '{value}'. Expected format: {SCHEDULE_FORMAT}"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    schedule_help = SCHEDULE_FORMAT.replace("%", "%%")
    parser = argparse.ArgumentParser(
        prog="opub",
        description="把视频/图文一键发布到抖音/小红书/快手/微博/B站/视频号/百家号。必填 --platforms，素材提供 --video（视频）或 --note --images（图文）。",
    )
    try:
        _version = pkg_version("opub")
    except PackageNotFoundError:
        _version = "0.0.0.dev0"
    parser.add_argument("--version", action="version", version=f"opub {_version}")
    parser.add_argument("--platforms", default=None, help="启用的平台，逗号分隔（必填）")
    parser.add_argument("--video", default=None, help="视频文件或目录路径")
    parser.add_argument("--note", action="store_true", help="图文模式：以 --images 的图片发布图文")
    parser.add_argument("--images", default=None, help="图文图片路径，逗号分隔（图文模式必填）")
    parser.add_argument("--convert-to-video", action="store_true", help="图文转视频后发布（仅 --note 模式生效）")
    parser.add_argument("--video-duration", type=float, default=5, help="图转视频每张图片时长（秒，默认 5）")
    parser.add_argument("--title", default=None, help="标题（由用户提供；仅用户明确同意自动生成时可留空，失败报 CFG-001）")
    parser.add_argument("--desc", default=None, help="描述（由用户提供；仅用户明确同意自动生成时可留空）")
    parser.add_argument("--tags", default=None, help="话题标签，逗号分隔")
    parser.add_argument("--schedule", type=_schedule_value, default=None, help=f"定时发布时间，格式 {schedule_help}")
    parser.add_argument("--start-from", type=int, default=None, help="断点续传起始序号，1 起")
    parser.add_argument("--force", action="store_true", help="强制重新生成视频配置")
    return parser


def _build_overrides(args: argparse.Namespace) -> PublishOverrides:
    return PublishOverrides(
        platforms=args.platforms,
        video=args.video,
        title=args.title,
        desc=args.desc,
        tags=args.tags,
        schedule=args.schedule,
        start_from=args.start_from,
        force=args.force,
        note=args.note,
        images=args.images,
        convert_to_video=args.convert_to_video,
        video_duration=args.video_duration,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return asyncio.run(run_publish(_build_overrides(args)))
    except Exception as exc:
        print_error("RUN-001", f"运行时异常: {exc}", "将以上错误信息反馈给用户；重试前请先检查配置与环境")
        return EXIT_ALL_FAIL
