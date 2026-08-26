# -*- coding: utf-8 -*-
"""发布参数构建:PublishOverrides 是唯一参数源,cookies/ 账号自动发现"""
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from conf import BASE_DIR


@dataclass
class PublishOverrides:
    platforms: Optional[str] = None
    video: Optional[str] = None
    title: Optional[str] = None
    desc: Optional[str] = None
    tags: Optional[str] = None
    schedule: Optional[datetime] = None
    start_from: Optional[int] = None
    force: bool = False
    note: bool = False
    images: Optional[str] = None
    convert_to_video: bool = False
    video_duration: float = 5.0


def _split_csv(value: Optional[str]) -> list:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


PLATFORM_ACCOUNT_SUBDIRS = {
    "douyin": "douyin_uploader",
    "kuaishou": "ks_uploader",
    "xiaohongshu": "xiaohongshu_uploader",
    "weibo": "weibo_uploader",
    "tencent": "tencent_uploader",
    "baijiahao": "baijiahao_uploader",
    "bilibili": "bilibili_uploader",
    "tk": "tk_uploader",
}


def default_account_file(platform: str) -> Optional[str]:
    """未发现账号文件时的默认保存路径,登录流程会把扫码结果写到这里"""
    subdir = PLATFORM_ACCOUNT_SUBDIRS.get(platform)
    if subdir is None:
        return None
    account_dir = BASE_DIR / "cookies" / subdir
    account_dir.mkdir(parents=True, exist_ok=True)
    return str(account_dir / "account.json")


def _discover_single_account_file(cookies_dir: Path, platform: str, prefix: str) -> Optional[Path]:
    """Find the unambiguous account file for one platform."""
    account_dir = cookies_dir / PLATFORM_ACCOUNT_SUBDIRS[platform]
    canonical = account_dir / "account.json"
    if canonical.is_file():
        return canonical

    legacy_files = sorted(file for file in cookies_dir.glob(f"{prefix}*.json") if file.is_file())
    if account_dir.exists():
        legacy_files.extend(
            sorted(file for file in account_dir.glob("*.json") if file.is_file() and file.name != "account.json")
        )
    return legacy_files[0] if len(legacy_files) == 1 else None


def _discover_account_files() -> Dict[str, str]:
    cookies_dir = BASE_DIR / "cookies"
    platform_prefixes = {
        "douyin": "douyin_",
        "kuaishou": "kuaishou_",
        "xiaohongshu": "xiaohongshu_",
        "weibo": "weibo_",
        "tencent": "tencent_",
        "baijiahao": "baijiahao_",
        "bilibili": "bilibili_",
        "tk": "tk_",
    }

    platforms = {}
    for platform, prefix in platform_prefixes.items():
        account_file = _discover_single_account_file(cookies_dir, platform, prefix)
        if account_file:
            platforms[f"{platform}_account"] = str(account_file.relative_to(BASE_DIR))
    return platforms


def default_params_from_overrides(overrides: Optional[PublishOverrides] = None) -> Dict[str, Any]:
    overrides = overrides or PublishOverrides()
    params: Dict[str, Any] = {
        "content_type": "note" if overrides.note else "video",
        "title": overrides.title or "",
        "desc": overrides.desc or "",
        "tags": _split_csv(overrides.tags),
        "video_file": overrides.video or "",
        "images": _split_csv(overrides.images),
        "publish_strategy": "scheduled" if overrides.schedule else "immediate",
        "publish_time": overrides.schedule,
        "enabled_platforms": _split_csv(overrides.platforms),
        "platforms": _discover_account_files(),
        "convert_to_video": overrides.convert_to_video,
        "video_duration": overrides.video_duration,
        "start_from": overrides.start_from if overrides.start_from else 1,
    }
    if overrides.force:
        params["force"] = True
    return params
