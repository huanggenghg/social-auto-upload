# -*- coding: utf-8 -*-
"""平台分发:登录校验与各平台发布实现"""
import importlib
import os
import sys

from publish.constants import PLATFORM_NAMES, TITLE_LIMITS
from publish.content import resolve_path, truncate_title


_PLATFORM_LOGIN = {
    "douyin":      ("uploader.douyin_uploader.main",      "cookie_auth", "douyin_setup"),
    "xiaohongshu": ("uploader.xiaohongshu_uploader.main", "cookie_auth", "xiaohongshu_setup"),
    "kuaishou":    ("uploader.ks_uploader.main",          "cookie_auth", "ks_setup"),
    "tencent":     ("uploader.tencent_uploader.main",     "cookie_auth", "tencent_setup"),
    "baijiahao":   ("uploader.baijiahao_uploader.main",   "cookie_auth", "baijiahao_setup"),
    "bilibili":    ("uploader.bilibili_uploader.main",    "cookie_auth", "bilibili_setup"),
    "weibo":       ("uploader.weibo_uploader.main",       "cookie_auth", "weibo_setup"),
    "tk":          ("uploader.tk_uploader.main",          "cookie_auth", "tiktok_setup"),
}


async def ensure_login(platform: str, account_file: str, force: bool = False) -> bool:
    """确保平台已登录，未登录则触发登录流程"""
    entry = _PLATFORM_LOGIN.get(platform)
    if entry is None:
        return False

    module_path, check_name, setup_name = entry
    module = importlib.import_module(module_path)

    if not force and os.path.exists(account_file):
        check_func = getattr(module, check_name)
        if await check_func(account_file):
            return True

    # 扫码登录会打开浏览器并阻塞等待用户扫码(最长约 5 分钟),
    # 提前告知调用方,避免 Agent 工具默认超时杀掉进程组导致浏览器一并退出
    print(
        f"[opub] {platform} 未登录,即将打开浏览器等待扫码登录(最长约 5 分钟)。"
        f"若由 Agent 调用,请确保工具超时不低于 360 秒",
        file=sys.stderr,
    )
    setup_func = getattr(module, setup_name)
    return await setup_func(account_file, handle=True)


async def ensure_account_login(platform: str, account_file: str, force: bool = False) -> bool:
    resolved_account = resolve_path(account_file)
    return await ensure_login(platform, resolved_account, force=force)


def platform_requires_account_login(platform: str) -> bool:
    return platform in _PLATFORM_LOGIN


async def publish_to_douyin(params: dict) -> dict:
    """发布到抖音"""
    from uploader.douyin_uploader.main import DouYinVideo, DouYinNote

    account_file = resolve_path(params["account_file"])
    title = truncate_title(params["title"], "douyin")

    if params["content_type"] == "video":
        params = {**params, "video_file": resolve_path(params["video_file"])}
    elif params.get("images"):
        params = {**params, "images": [resolve_path(img) for img in params["images"]]}

    err = DouYinVideo.validate_base_args(params)
    if err:
        return err

    try:
        if params["content_type"] == "video":
            uploader = DouYinVideo(
                title=title, file_path=params["video_file"], tags=params["tags"],
                publish_date=params["publish_time"] or 0, account_file=account_file,
                desc=params["desc"], publish_strategy=params["publish_strategy"],
            )
        else:
            uploader = DouYinNote(
                image_paths=params["images"], note=params["desc"], tags=params["tags"],
                publish_date=params["publish_time"] or 0, account_file=account_file,
                title=title, publish_strategy=params["publish_strategy"],
            )
        return await uploader.upload()
    except Exception as e:
        return {"success": False, "message": str(e)}


async def publish_to_xiaohongshu(params: dict) -> dict:
    """发布到小红书"""
    from uploader.xiaohongshu_uploader.main import XiaoHongShuVideo, XiaoHongShuNote

    account_file = resolve_path(params["account_file"])
    title = truncate_title(params["title"], "xiaohongshu")

    if params["content_type"] == "video":
        params = {**params, "video_file": resolve_path(params["video_file"])}
    elif params.get("images"):
        params = {**params, "images": [resolve_path(img) for img in params["images"]]}

    err = XiaoHongShuVideo.validate_base_args(params)
    if err:
        return err

    try:
        if params["content_type"] == "video":
            uploader = XiaoHongShuVideo(
                title=title, file_path=params["video_file"], tags=params["tags"],
                publish_date=params["publish_time"] or 0, account_file=account_file,
                desc=params["desc"], publish_strategy=params["publish_strategy"],
            )
        else:
            uploader = XiaoHongShuNote(
                image_paths=params["images"], note=params["desc"], tags=params["tags"],
                publish_date=params["publish_time"] or 0, account_file=account_file,
                title=title, desc=params["desc"], publish_strategy=params["publish_strategy"],
            )
        return await uploader.upload()
    except Exception as e:
        return {"success": False, "message": str(e)}


async def publish_to_kuaishou(params: dict) -> dict:
    """发布到快手"""
    from uploader.ks_uploader.main import KSVideo, KSNote

    account_file = resolve_path(params["account_file"])
    title = truncate_title(params["title"], "kuaishou")

    if params["content_type"] == "video":
        params = {**params, "video_file": resolve_path(params["video_file"])}
    elif params.get("images"):
        params = {**params, "images": [resolve_path(img) for img in params["images"]]}

    err = KSVideo.validate_base_args(params)
    if err:
        return err

    try:
        if params["content_type"] == "video":
            uploader = KSVideo(
                title=title, file_path=params["video_file"], tags=params["tags"],
                publish_date=params["publish_time"] or 0, account_file=account_file,
                desc=params["desc"], publish_strategy=params["publish_strategy"],
            )
        else:
            uploader = KSNote(
                image_paths=params["images"], note=params["desc"], tags=params["tags"],
                publish_date=params["publish_time"] or 0, account_file=account_file,
                title=title, publish_strategy=params["publish_strategy"],
            )
        return await uploader.upload()
    except Exception as e:
        return {"success": False, "message": str(e)}


async def publish_to_tencent(params: dict) -> dict:
    """发布到微信视频号"""
    from uploader.tencent_uploader.main import TencentVideo
    from utils.excel_writer import write_video_link

    account_file = resolve_path(params["account_file"])
    title = truncate_title(params["title"], "tencent")

    if params["content_type"] == "video":
        params = {**params, "video_file": resolve_path(params["video_file"])}
    else:
        return {"success": False, "message": "微信视频号不支持图文发布，请使用 convert_to_video=true 转为视频发布"}

    err = TencentVideo.validate_base_args(params)
    if err:
        return err

    try:
        uploader = TencentVideo(
            title=title, file_path=params["video_file"], tags=params["tags"],
            publish_date=params["publish_time"] or 0, account_file=account_file,
            desc=params["desc"], publish_strategy=params["publish_strategy"],
        )
        result = await uploader.upload()
        if result["success"] and result.get("result_url"):
            try:
                write_result = write_video_link(result["result_url"])
                if write_result["success"]:
                    print(f"  📝 视频链接已写入 Excel: {result['result_url']}")
                else:
                    print(f"  ⚠️ 写入 Excel 失败: {write_result['message']}")
            except Exception as e:
                print(f"  ⚠️ 写入 Excel 异常: {e}")
        return result
    except Exception as e:
        return {"success": False, "message": str(e)}


async def publish_to_baijiahao(params: dict) -> dict:
    """发布到百家号"""
    from uploader.baijiahao_uploader.main import BaiJiaHaoVideo
    from utils.excel_writer import write_video_link

    account_file = resolve_path(params["account_file"])
    title = truncate_title(params["title"], "baijiahao")

    if params["content_type"] == "video":
        params = {**params, "video_file": resolve_path(params["video_file"])}
    else:
        return {"success": False, "message": "百家号不支持图文发布，请使用 convert_to_video=true 转为视频发布"}

    err = BaiJiaHaoVideo.validate_base_args(params)
    if err:
        return err

    try:
        uploader = BaiJiaHaoVideo(
            title=title, file_path=params["video_file"], tags=params["tags"],
            publish_date=params["publish_time"] or 0, account_file=account_file,
            publish_strategy=params["publish_strategy"],
        )
        result = await uploader.upload()
        if result["success"] and result.get("result_url"):
            try:
                write_result = write_video_link(result["result_url"])
                if write_result["success"]:
                    print(f"  📝 视频链接已写入 Excel: {result['result_url']}")
                else:
                    print(f"  ⚠️ 写入 Excel 失败: {write_result['message']}")
            except Exception as e:
                print(f"  ⚠️ 写入 Excel 异常: {e}")
        return result
    except Exception as e:
        return {"success": False, "message": str(e)}


async def publish_to_bilibili(params: dict) -> dict:
    """发布到 B站 (via biliup CLI)"""
    from uploader.bilibili_uploader.main import BilibiliUploader
    from utils.excel_writer import write_video_link

    account_file = resolve_path(params["account_file"])
    title = truncate_title(params["title"], "bilibili")

    if params["content_type"] != "video":
        return {"success": False, "message": "B站暂只支持视频发布"}

    params = {**params, "video_file": resolve_path(params["video_file"])}

    err = BilibiliUploader.validate_base_args(params)
    if err:
        return err

    try:
        uploader = BilibiliUploader(
            title=title, file_path=params["video_file"], tags=params["tags"],
            account_file=account_file, desc=params["desc"],
            publish_strategy=params["publish_strategy"],
        )
        result = await uploader.upload()
        if result["success"] and result.get("result_url"):
            try:
                write_result = write_video_link(result["result_url"])
                if write_result["success"]:
                    print(f"  📝 视频链接已写入 Excel: {result['result_url']}")
                else:
                    print(f"  ⚠️ 写入 Excel 失败: {write_result['message']}")
            except Exception as e:
                print(f"  ⚠️ 写入 Excel 异常: {e}")
        return result
    except Exception as e:
        return {"success": False, "message": str(e)}


async def publish_to_weibo(params: dict) -> dict:
    """发布到微博"""
    from uploader.weibo_uploader.main import WeiboVideo, WeiboNote
    from utils.excel_writer import write_video_link

    account_file = resolve_path(params["account_file"])
    title = truncate_title(params["title"], "weibo")

    if params["content_type"] == "video":
        params = {**params, "video_file": resolve_path(params["video_file"])}
    elif params.get("images"):
        params = {**params, "images": [resolve_path(img) for img in params["images"]]}

    err = WeiboVideo.validate_base_args(params)
    if err:
        return err

    try:
        if params["content_type"] == "video":
            uploader = WeiboVideo(
                title=title, file_path=params["video_file"], tags=params["tags"],
                publish_date=params["publish_time"] or 0, account_file=account_file,
                desc=params["desc"], publish_strategy=params["publish_strategy"],
            )
        else:
            uploader = WeiboNote(
                image_paths=params["images"], note=params["desc"], tags=params["tags"],
                publish_date=params["publish_time"] or 0, account_file=account_file,
                title=title, publish_strategy=params["publish_strategy"],
            )
        result = await uploader.upload()
        if result["success"] and result.get("result_url"):
            try:
                write_result = write_video_link(result["result_url"])
                if write_result["success"]:
                    print(f"  📝 视频链接已写入 Excel: {result['result_url']}")
                else:
                    print(f"  ⚠️ 写入 Excel 失败: {write_result['message']}")
            except Exception as e:
                print(f"  ⚠️ 写入 Excel 异常: {e}")
        return result
    except Exception as e:
        return {"success": False, "message": str(e)}


async def publish_to_tk(params: dict) -> dict:
    """发布到 TikTok"""
    from uploader.tk_uploader.main import TiktokVideo

    account_file = resolve_path(params["account_file"])
    title = truncate_title(params["title"], "tk")

    if params["content_type"] != "video":
        return {"success": False, "message": "TikTok 暂只支持视频发布"}

    params = {**params, "video_file": resolve_path(params["video_file"])}

    err = TiktokVideo.validate_base_args(params)
    if err:
        return err

    try:
        uploader = TiktokVideo(
            title=title, file_path=params["video_file"], tags=params["tags"],
            publish_date=params["publish_time"] or 0, account_file=account_file,
            desc=params.get("desc", ""), publish_strategy=params["publish_strategy"],
        )
        return await uploader.upload()
    except Exception as e:
        return {"success": False, "message": str(e)}


_PUBLISH_DISPATCH = {
    "douyin":      publish_to_douyin,
    "xiaohongshu": publish_to_xiaohongshu,
    "kuaishou":    publish_to_kuaishou,
    "tencent":     publish_to_tencent,
    "baijiahao":   publish_to_baijiahao,
    "bilibili":    publish_to_bilibili,
    "weibo":       publish_to_weibo,
    "tk":          publish_to_tk,
}


async def publish_to_platform(platform: str, params: dict) -> dict:
    """发布到指定平台"""
    handler = _PUBLISH_DISPATCH.get(platform)
    if handler is not None:
        return await handler(params)
    return {"success": False, "message": f"未知平台: {platform}"}
