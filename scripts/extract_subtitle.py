#!/usr/bin/env python3
"""
B站视频字幕提取工具（纯标准库，无需第三方依赖）

支持输入类型：
  1. 单个视频URL: https://www.bilibili.com/video/BV1Qe4y1E77h
  2. 纯BV号: BV1Qe4y1E77h
  3. 合集/系列URL: https://space.bilibili.com/{mid}/lists/{series_id}?type=series
  4. 收藏夹合集URL: https://space.bilibili.com/{mid}/channel/collectiondetail?sid={season_id}

四条字幕获取路径自动降级：
  路径0: view 接口直接返回字幕
  路径1: /x/v2/dm/view (弹幕视图接口，不需要 WBI 签名)
  路径2: /x/player/wbi/v2 (播放器接口，需要 WBI 签名，Cookie 可选)
  路径3: yt-dlp 降级 (通过浏览器Cookie获取AI字幕，支持多分P视频)
"""

import argparse
import glob as glob_mod
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from functools import reduce
from typing import Optional

# ========== 常量 ==========

PREFERRED_LANGS = ["zh-Hans", "zh", "zh-CN", "ai-zh", "en", "ja", "ko"]

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# yt-dlp 降级触发阈值：每分钟视频至少应有这么多段字幕
# 如果低于此值，认为API返回的数据不完整
YTDLP_MIN_SEGMENTS_PER_MIN = 1.0


# ========== HTTP ==========

def _build_headers(cookie: str = "") -> dict:
    h = {
        "User-Agent": DEFAULT_UA,
        "Referer": "https://www.bilibili.com",
    }
    if cookie:
        h["Cookie"] = cookie
    return h


def fetch_json(url: str, cookie: str = "") -> dict:
    req = urllib.request.Request(url)
    for k, v in _build_headers(cookie).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ========== WBI 签名 ==========

def _get_mixin_key(orig: str) -> str:
    return reduce(lambda s, i: s + orig[i], MIXIN_KEY_ENC_TAB, "")[:32]


def get_wbi_keys(cookie: str = "") -> tuple:
    data = fetch_json("https://api.bilibili.com/x/web-interface/nav", cookie)
    wbi_img = data["data"]["wbi_img"]
    img_key = wbi_img["img_url"].rsplit("/", 1)[1].split(".")[0]
    sub_key = wbi_img["sub_url"].rsplit("/", 1)[1].split(".")[0]
    return img_key, sub_key


def enc_wbi(params: dict, img_key: str, sub_key: str) -> str:
    mixin_key = _get_mixin_key(img_key + sub_key)
    params["wts"] = round(time.time())
    params = dict(sorted(params.items()))
    params = {
        k: "".join(c for c in str(v) if c not in "!'()*")
        for k, v in params.items()
    }
    query = urllib.parse.urlencode(params)
    wbi_sign = hashlib.md5((query + mixin_key).encode()).hexdigest()
    params["w_rid"] = wbi_sign
    return urllib.parse.urlencode(params)


# ========== URL 类型识别 ==========

def parse_bvid(url: str) -> Optional[str]:
    m = re.search(r"(BV[a-zA-Z0-9]+)", url)
    return m.group(1) if m else None


def parse_series_url(url: str) -> Optional[dict]:
    """解析合集/系列URL，返回 {mid, series_id} 或 None"""
    # https://space.bilibili.com/476706561/lists/2795389?type=series
    m = re.search(r"space\.bilibili\.com/(\d+)/lists/(\d+)", url)
    if m:
        return {"mid": m.group(1), "series_id": m.group(2)}
    return None


def parse_season_url(url: str) -> Optional[dict]:
    """解析收藏夹合集URL，返回 {mid, season_id} 或 None"""
    # https://space.bilibili.com/476706561/channel/collectiondetail?sid=12345
    m = re.search(r"space\.bilibili\.com/(\d+)/channel/collectiondetail\?sid=(\d+)", url)
    if m:
        return {"mid": m.group(1), "season_id": m.group(2)}
    # 也支持新版合集URL: /lists/{id}?type=season
    m2 = re.search(r"space\.bilibili\.com/(\d+)/lists/(\d+)\?type=season", url)
    if m2:
        return {"mid": m2.group(1), "season_id": m2.group(2)}
    return None


def detect_url_type(url: str) -> str:
    """
    返回: "series" | "season" | "video" | "unknown"
    """
    if parse_season_url(url):
        return "season"
    if parse_series_url(url):
        return "series"
    if parse_bvid(url):
        return "video"
    return "unknown"


# ========== 合集视频列表获取 ==========

def fetch_series_videos(mid: str, series_id: str, cookie: str = "") -> list:
    """获取系列(series)下的所有视频，返回 [{bvid, title, duration}, ...]"""
    all_videos = []
    pn = 1
    ps = 30
    while True:
        url = (f"https://api.bilibili.com/x/series/archives"
               f"?mid={mid}&series_id={series_id}"
               f"&only_normal=true&sort=asc&pn={pn}&ps={ps}")
        data = fetch_json(url, cookie)
        if data.get("code") != 0:
            raise ValueError(f"获取系列视频列表失败: {data.get('message')}")
        archives = data["data"].get("archives", [])
        if not archives:
            break
        for v in archives:
            all_videos.append({
                "bvid": v.get("bvid", ""),
                "title": v.get("title", ""),
                "duration": v.get("duration", 0),
            })
        total = data["data"].get("page", {}).get("total", 0)
        if len(all_videos) >= total:
            break
        pn += 1
    return all_videos


def fetch_season_videos(mid: str, season_id: str, cookie: str = "") -> list:
    """获取合集(season/collection)下的所有视频"""
    all_videos = []
    pn = 1
    ps = 30
    while True:
        url = (f"https://api.bilibili.com/x/polymer/space/seasons_archives_list"
               f"?mid={mid}&season_id={season_id}"
               f"&sort_reverse=false&page_num={pn}&page_size={ps}")
        data = fetch_json(url, cookie)
        if data.get("code") != 0:
            raise ValueError(f"获取合集视频列表失败: {data.get('message')}")
        archives = data["data"].get("archives", [])
        if not archives:
            break
        for v in archives:
            all_videos.append({
                "bvid": v.get("bvid", ""),
                "title": v.get("title", ""),
                "duration": v.get("duration", 0),
            })
        total = data["data"].get("page", {}).get("total", 0)
        if len(all_videos) >= total:
            break
        pn += 1
    return all_videos


# ========== 字幕选择 ==========

def pick_best_subtitle(subtitle_list: list) -> Optional[dict]:
    if not subtitle_list:
        return None
    for pref in PREFERRED_LANGS:
        for s in subtitle_list:
            if s.get("lan", "") == pref:
                return s
    return subtitle_list[0]


def normalize_url(url: str) -> str:
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("http://"):
        url = "https://" + url[7:]
    return url


# ========== 三条API获取路径 ==========

def path0_from_view(view_data: dict) -> Optional[dict]:
    """路径0: view 接口直接带字幕（且字幕URL不为空）"""
    subtitle_list = view_data.get("subtitle", {}).get("list", [])
    valid_subs = [s for s in subtitle_list if s.get("subtitle_url")]
    if valid_subs:
        print(f"  [路径0] view 接口返回 {len(valid_subs)} 条有效字幕")
        return {"subtitles": valid_subs, "source": "view"}
    elif subtitle_list:
        print(f"  [路径0] view 返回 {len(subtitle_list)} 条字幕但 URL 为空，跳过")
    return None


def path1_dm_view(aid: int, cid: int, cookie: str = "") -> Optional[dict]:
    """路径1: dm/view 弹幕视图接口（无需WBI签名）"""
    print("  [路径1] 尝试 dm/view 接口...")
    try:
        url = f"https://api.bilibili.com/x/v2/dm/view?aid={aid}&oid={cid}&type=1"
        data = fetch_json(url, cookie)
        subs = data.get("data", {}).get("subtitle", {}).get("subtitles", [])
        if subs:
            print(f"  [路径1] 成功，找到 {len(subs)} 条字幕")
            return {"subtitles": subs, "source": "dm/view"}
        print("  [路径1] 未返回字幕")
        return None
    except Exception as e:
        print(f"  [路径1] 失败: {e}")
        return None


def path2_player_wbi(bvid: str, cid: int, cookie: str = "") -> Optional[dict]:
    """路径2: player/wbi/v2 接口（需WBI签名）"""
    print("  [路径2] 尝试 player/wbi/v2 接口...")
    try:
        img_key, sub_key = get_wbi_keys(cookie)
        query = enc_wbi({"bvid": bvid, "cid": cid}, img_key, sub_key)
        url = f"https://api.bilibili.com/x/player/wbi/v2?{query}"
        data = fetch_json(url, cookie)
        subs = data.get("data", {}).get("subtitle", {}).get("subtitles", [])
        if subs:
            print(f"  [路径2] 成功，找到 {len(subs)} 条字幕")
            return {"subtitles": subs, "source": "player/wbi/v2"}
        print("  [路径2] 未返回字幕")
        return None
    except Exception as e:
        print(f"  [路径2] 失败: {e}")
        return None


# ========== 路径3: yt-dlp 降级 ==========

def _check_ytdlp() -> bool:
    """检查 yt-dlp 是否可用"""
    return shutil.which("yt-dlp") is not None


def _parse_srt_time(time_str: str) -> float:
    """解析 SRT 时间格式 HH:MM:SS,mmm -> 秒数"""
    time_str = time_str.strip()
    parts = time_str.replace(",", ".").split(":")
    if len(parts) == 3:
        h, m, s = float(parts[0]), float(parts[1]), float(parts[2])
        return h * 3600 + m * 60 + s
    return 0.0


def _parse_srt(srt_content: str) -> list:
    """
    解析 SRT 内容为 segments 列表。
    返回 [{"from": float, "to": float, "content": str}, ...]
    """
    segments = []
    blocks = re.split(r"\n\s*\n", srt_content.strip())
    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 2:
            continue
        # 找到时间行（包含 -->）
        time_line = None
        text_lines = []
        found_time = False
        for line in lines:
            if "-->" in line and not found_time:
                time_line = line
                found_time = True
            elif found_time:
                text_lines.append(line.strip())
        if not time_line:
            continue
        # 解析时间
        time_match = re.match(
            r"(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3})",
            time_line.strip()
        )
        if not time_match:
            continue
        start = _parse_srt_time(time_match.group(1))
        end = _parse_srt_time(time_match.group(2))
        text = " ".join(text_lines).strip()
        if text:
            segments.append({"from": start, "to": end, "content": text})
    return segments


def _extract_part_number(filename: str) -> int:
    """从 yt-dlp 生成的文件名中提取分P编号"""
    # yt-dlp 多P视频文件名通常包含 .Pxx. 或 _Pxx_ 之类的模式
    # 例如: video_title.P01.ai-zh.srt, video_title.P02.ai-zh.srt
    m = re.search(r"[._]P(\d+)[._]", filename)
    if m:
        return int(m.group(1))
    # 也尝试匹配 part 或分P数字
    m2 = re.search(r"[._](\d+)[._]", os.path.basename(filename))
    if m2:
        return int(m2.group(1))
    return 0


def path3_ytdlp(bvid: str, browser: str = "chrome", pages: list = None,
                 duration: int = 0) -> Optional[dict]:
    """
    路径3: 使用 yt-dlp + 浏览器Cookie 获取字幕（降级方案）

    适用于：
    - API路径0/1/2都获取不到字幕
    - 多分P视频API只返回部分数据
    - 需要登录才能获取AI字幕的视频

    参数:
        bvid: BV号
        browser: Cookie来源浏览器，默认 chrome
        pages: 视频分P信息列表 [{cid, part, duration}, ...]
        duration: 视频总时长（秒）

    返回:
        成功: {"segments": [...], "source": "yt-dlp", "language": str, "subtitle_type": str}
        失败: None
    """
    if not _check_ytdlp():
        print("  [路径3] yt-dlp 未安装，跳过降级方案")
        print("  [路径3] 可通过 pip3 install yt-dlp 安装")
        return None

    print(f"  [路径3] 使用 yt-dlp 降级方案 (浏览器Cookie: {browser})...")
    video_url = f"https://www.bilibili.com/video/{bvid}"

    # 创建临时目录存放 SRT 文件
    tmp_dir = tempfile.mkdtemp(prefix="bili_ytdlp_")
    try:
        # 构建 yt-dlp 命令
        cmd = [
            "yt-dlp",
            "--cookies-from-browser", browser,
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs", "ai-zh,zh-Hans,zh,zh-CN,en",
            "--sub-format", "srt",
            "--skip-download",
            "--no-warnings",
            "-o", os.path.join(tmp_dir, "%(title)s.%(autonumber)s.%(ext)s"),
            video_url,
        ]

        print(f"  [路径3] 执行: yt-dlp --cookies-from-browser {browser} ...")
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,  # 5分钟超时
        )

        if proc.returncode != 0:
            stderr = proc.stderr.strip()
            # 检查是否是cookie相关错误
            if "cookies" in stderr.lower():
                print(f"  [路径3] 浏览器Cookie提取失败: {stderr[:200]}")
                print(f"  [路径3] 请确保 {browser} 浏览器已登录B站")
            else:
                print(f"  [路径3] yt-dlp 执行失败: {stderr[:200]}")
            return None

        # 查找所有 SRT 文件
        srt_files = sorted(glob_mod.glob(os.path.join(tmp_dir, "*.srt")))
        if not srt_files:
            print("  [路径3] yt-dlp 未生成任何字幕文件")
            # 检查是否只有弹幕（danmaku）
            all_files = os.listdir(tmp_dir)
            if all_files:
                print(f"  [路径3] 目录中的文件: {all_files[:5]}")
            return None

        print(f"  [路径3] 找到 {len(srt_files)} 个 SRT 字幕文件")

        # 检测字幕语言
        detected_lang = "ai-zh"
        subtitle_type = "ai"
        for f in srt_files:
            basename = os.path.basename(f)
            if "ai-zh" in basename:
                detected_lang = "中文（AI生成）"
                subtitle_type = "ai"
                break
            elif "zh-Hans" in basename or "zh-CN" in basename:
                detected_lang = "中文（简体）"
                subtitle_type = "manual"
                break
            elif ".zh." in basename:
                detected_lang = "中文"
                subtitle_type = "auto"
                break
            elif ".en." in basename:
                detected_lang = "English"
                subtitle_type = "auto"
                break

        # 解析并合并所有 SRT 文件
        all_segments = []

        if len(srt_files) == 1:
            # 单个文件，直接解析
            with open(srt_files[0], "r", encoding="utf-8") as f:
                content = f.read()
            all_segments = _parse_srt(content)
        else:
            # 多个文件（多分P），按分P顺序合并，累加时间偏移
            # 按文件名中的编号排序
            file_parts = []
            for srt_file in srt_files:
                part_num = _extract_part_number(srt_file)
                file_parts.append((part_num, srt_file))
            file_parts.sort(key=lambda x: x[0])

            time_offset = 0.0
            for i, (part_num, srt_file) in enumerate(file_parts):
                with open(srt_file, "r", encoding="utf-8") as f:
                    content = f.read()
                part_segments = _parse_srt(content)
                if not part_segments:
                    continue

                # 给每段加上时间偏移
                for seg in part_segments:
                    seg["from"] += time_offset
                    seg["to"] += time_offset
                    all_segments.append(seg)

                # 计算下一个分P的时间偏移
                # 使用当前分P最后一段字幕的结束时间
                max_end = max(seg["to"] for seg in part_segments)
                time_offset += max_end

                print(f"    分P{i+1}: {len(part_segments)} 段字幕，"
                      f"时长 {max_end:.1f}s")

        if not all_segments:
            print("  [路径3] SRT 文件解析后无有效字幕段")
            return None

        total_chars = sum(len(seg["content"]) for seg in all_segments)
        print(f"  [路径3] 成功: 共 {len(all_segments)} 段, {total_chars} 字")

        return {
            "segments": all_segments,
            "source": "yt-dlp",
            "language": detected_lang,
            "subtitle_type": subtitle_type,
        }

    except subprocess.TimeoutExpired:
        print("  [路径3] yt-dlp 执行超时（5分钟限制）")
        return None
    except Exception as e:
        print(f"  [路径3] 异常: {e}")
        return None
    finally:
        # 清理临时目录
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


def _should_try_ytdlp(segments: list, duration: int, pages: list) -> bool:
    """
    判断是否需要触发 yt-dlp 降级。

    触发条件（满足任一即触发）：
    1. API完全没返回字幕（segments为空）
    2. 多分P视频（pages > 1）但API只返回了很少的字幕段
    3. 字幕段数相对视频时长异常少（低于阈值）
    """
    # 条件1: 完全没字幕
    if not segments:
        return True

    # 条件2: 多分P视频，字幕段数异常少
    num_pages = len(pages) if pages else 1
    if num_pages > 1:
        expected_min = num_pages * 5  # 每P至少5段
        if len(segments) < expected_min:
            print(f"  [降级判断] 多分P视频({num_pages}P)但仅{len(segments)}段字幕"
                  f"（期望至少{expected_min}段），触发yt-dlp降级")
            return True

    # 条件3: 字幕密度过低
    if duration > 60:  # 仅对超过1分钟的视频检查
        duration_min = duration / 60.0
        density = len(segments) / duration_min
        if density < YTDLP_MIN_SEGMENTS_PER_MIN:
            print(f"  [降级判断] 字幕密度过低({density:.2f}段/分钟"
                  f"< {YTDLP_MIN_SEGMENTS_PER_MIN})，触发yt-dlp降级")
            return True

    return False


# ========== 文字稿转换 ==========

def to_timestamped(segments: list) -> str:
    lines = []
    for item in segments:
        start = item.get("from", 0)
        content = item.get("content", "").strip()
        if not content:
            continue
        h, m, s = int(start // 3600), int((start % 3600) // 60), int(start % 60)
        lines.append(f"[{h:02d}:{m:02d}:{s:02d}] {content}")
    return "\n".join(lines)


def to_plain_text(segments: list) -> str:
    texts = [item.get("content", "").strip() for item in segments
             if item.get("content", "").strip()]
    paragraphs, current = [], []
    for t in texts:
        current.append(t)
        if t and t[-1] in "。！？…~》」）)!?.~；;":
            paragraphs.append("".join(current))
            current = []
    if current:
        paragraphs.append("".join(current))
    return "\n\n".join(paragraphs)


# ========== 单视频提取 ==========

def extract(url: str, cookie: str = "", browser: str = "chrome") -> dict:
    """提取单个视频字幕，返回结构化结果"""
    bvid = parse_bvid(url)
    if not bvid:
        raise ValueError(f"无法从输入中提取BV号: {url}")
    print(f"BV号: {bvid}")

    # 获取视频信息
    print("获取视频信息...")
    resp = fetch_json(
        f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}", cookie
    )
    if resp.get("code") != 0:
        raise ValueError(f"获取视频信息失败: {resp.get('message', '未知错误')}")

    vdata = resp["data"]
    title = vdata.get("title", "未知标题")
    default_cid = vdata.get("cid")
    aid = vdata.get("aid")
    duration = vdata.get("duration", 0)
    owner = vdata.get("owner", {}).get("name", "未知")
    pages = vdata.get("pages", [])

    # 收集所有分P的 cid，去重保序
    cids = [default_cid] + [p["cid"] for p in pages if p.get("cid") != default_cid]
    seen = set()
    unique_cids = []
    for c in cids:
        if c not in seen:
            seen.add(c)
            unique_cids.append(c)
    cids = unique_cids

    print(f"  标题: {title}")
    print(f"  UP主: {owner}")
    print(f"  时长: {duration // 60}分{duration % 60}秒")
    if len(pages) > 1:
        print(f"  分P数: {len(pages)}")

    # === API路径 0/1/2 ===
    result = path0_from_view(vdata)

    if not result:
        for cid in cids:
            result = path1_dm_view(aid, cid, cookie)
            if result:
                break

    if not result:
        for cid in cids:
            result = path2_player_wbi(bvid, cid, cookie)
            if result:
                break

    # 如果API路径成功，先获取字幕内容
    api_segments = []
    api_meta = {}
    if result:
        best = pick_best_subtitle(result["subtitles"])
        lang = best.get("lan_doc", best.get("lan", "未知"))
        lan_code = best.get("lan", "")
        sub_type = ("ai" if best.get("ai_type") or lan_code.startswith("ai-")
                    else "auto" if "自动" in lang else "manual")
        sub_url = normalize_url(best.get("subtitle_url", ""))
        print(f"  字幕: {lang} ({sub_type}, {result['source']})")

        api_segments = fetch_json(sub_url, cookie).get("body", [])
        print(f"  共 {len(api_segments)} 段")
        api_meta = {
            "language": lang, "subtitle_type": sub_type,
            "source": result["source"],
        }

    # === 路径3: yt-dlp 降级判断 ===
    # 检查API结果是否足够完整，不够则尝试 yt-dlp
    segments = api_segments
    meta = api_meta

    if _should_try_ytdlp(api_segments, duration, pages):
        ytdlp_result = path3_ytdlp(bvid, browser, pages, duration)
        if ytdlp_result:
            ytdlp_segments = ytdlp_result["segments"]
            # 如果 yt-dlp 获取的字幕比API多，使用 yt-dlp 的结果
            if len(ytdlp_segments) > len(api_segments):
                print(f"  [降级] yt-dlp 获取了 {len(ytdlp_segments)} 段"
                      f"（API仅 {len(api_segments)} 段），使用 yt-dlp 结果")
                segments = ytdlp_segments
                meta = {
                    "language": ytdlp_result["language"],
                    "subtitle_type": ytdlp_result["subtitle_type"],
                    "source": ytdlp_result["source"],
                }
            else:
                print(f"  [降级] yt-dlp ({len(ytdlp_segments)}段)"
                      f" 未比API ({len(api_segments)}段) 多，保留API结果")

    if not segments:
        return {
            "has_subtitle": False, "title": title, "bvid": bvid,
            "duration": duration, "owner": owner, "language": "",
            "subtitle_type": "none", "source": "", "segments": [],
            "timestamped_text": "", "full_text": "",
        }

    return {
        "has_subtitle": True, "title": title, "bvid": bvid,
        "duration": duration, "owner": owner,
        "language": meta.get("language", ""),
        "subtitle_type": meta.get("subtitle_type", ""),
        "source": meta.get("source", ""),
        "segments": segments,
        "timestamped_text": to_timestamped(segments),
        "full_text": to_plain_text(segments),
    }


# ========== 保存 ==========

def save(result: dict, output_dir: str) -> tuple:
    """保存为 Markdown + TXT，返回 (md_path, txt_path)"""
    os.makedirs(output_dir, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]', '_', result["title"])
    dur = result["duration"]

    md_path = os.path.join(output_dir, f"{safe}_字幕.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# {result['title']}\n\n")
        f.write(f"> BV号: {result['bvid']}  \n")
        f.write(f"> UP主: {result['owner']}  \n")
        f.write(f"> 时长: {dur // 60}分{dur % 60}秒  \n")
        f.write(f"> 字幕语言: {result['language']}  \n")
        f.write(f"> 字幕类型: {result['subtitle_type']}  \n")
        f.write(f"> 获取来源: {result['source']}  \n\n")
        f.write("---\n\n")
        f.write("## 带时间戳文字稿\n\n```\n")
        f.write(result["timestamped_text"])
        f.write("\n```\n\n## 纯文字稿\n\n")
        f.write(result["full_text"])
        f.write("\n")

    txt_path = os.path.join(output_dir, f"{safe}_字幕.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(result["full_text"])

    return md_path, txt_path


# ========== 合集批量提取 ==========

def extract_batch(videos: list, output_dir: str, cookie: str = "",
                  browser: str = "chrome") -> dict:
    """
    批量提取合集中所有视频的字幕。
    返回 {"success": [...], "failed": [...], "no_subtitle": [...], "index_path": str}
    """
    os.makedirs(output_dir, exist_ok=True)
    success, failed, no_subtitle = [], [], []

    total = len(videos)
    for i, v in enumerate(videos):
        bvid = v["bvid"]
        title = v["title"]
        dur = v["duration"]
        print(f"\n{'─' * 60}")
        print(f"[{i+1}/{total}] {title}")
        print(f"  BV号: {bvid} | 时长: {dur // 60}分{dur % 60}秒")

        try:
            result = extract(bvid, cookie, browser)
            if result["has_subtitle"]:
                md_path, txt_path = save(result, output_dir)
                success.append({
                    "bvid": bvid, "title": title,
                    "md_path": md_path, "txt_path": txt_path,
                    "segments": len(result["segments"]),
                    "chars": len(result["full_text"]),
                })
                print(f"  ✓ 保存完成 ({len(result['segments'])}段, {len(result['full_text'])}字)")
            else:
                no_subtitle.append({"bvid": bvid, "title": title})
                print(f"  ○ 无可用字幕")
        except Exception as e:
            failed.append({"bvid": bvid, "title": title, "error": str(e)})
            print(f"  ✗ 提取失败: {e}")

        # 请求间隔，避免触发频率限制
        if i < total - 1:
            time.sleep(1)

    # 生成目录索引
    index_path = os.path.join(output_dir, "00_目录.md")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(f"# 合集字幕提取目录\n\n")
        f.write(f"> 共 {total} 个视频，成功 {len(success)}，无字幕 {len(no_subtitle)}，失败 {len(failed)}\n\n")

        if success:
            f.write("## 提取成功\n\n")
            for j, s in enumerate(success):
                md_name = os.path.basename(s["md_path"])
                f.write(f"{j+1}. [{s['title']}](./{md_name}) — {s['segments']}段, {s['chars']}字\n")
            f.write("\n")

        if no_subtitle:
            f.write("## 无可用字幕\n\n")
            for s in no_subtitle:
                f.write(f"- {s['title']} ({s['bvid']})\n")
            f.write("\n")

        if failed:
            f.write("## 提取失败\n\n")
            for s in failed:
                f.write(f"- {s['title']} ({s['bvid']}): {s['error']}\n")
            f.write("\n")

    return {
        "success": success, "failed": failed,
        "no_subtitle": no_subtitle, "index_path": index_path,
    }


# ========== 主入口 ==========

def main():
    parser = argparse.ArgumentParser(
        description="B站视频字幕提取工具（支持单视频和合集/系列）"
    )
    parser.add_argument("url", help="B站视频URL、BV号、合集URL或系列URL")
    parser.add_argument("--output-dir", default=".", help="输出目录（默认当前目录）")
    parser.add_argument("--cookie", default="", help="B站Cookie（可选）")
    parser.add_argument(
        "--browser", default="chrome",
        help="yt-dlp降级时提取Cookie的浏览器（默认chrome，可选: firefox, edge, safari等）"
    )
    args = parser.parse_args()

    print("=" * 60)
    print("B站视频字幕提取工具")
    print("=" * 60)

    url_type = detect_url_type(args.url)
    print(f"输入类型: {url_type}")

    # ===== 合集/系列模式 =====
    if url_type == "series":
        info = parse_series_url(args.url)
        print(f"UP主ID: {info['mid']}, 系列ID: {info['series_id']}")
        print("获取系列视频列表...")
        videos = fetch_series_videos(info["mid"], info["series_id"], args.cookie)
        print(f"共 {len(videos)} 个视频:")
        for i, v in enumerate(videos):
            d = v["duration"]
            print(f"  [{i+1}] {v['bvid']} | {d//60}:{d%60:02d} | {v['title']}")
        print()
        batch_result = extract_batch(videos, args.output_dir, args.cookie, args.browser)
        _print_batch_summary(batch_result)
        return

    if url_type == "season":
        info = parse_season_url(args.url)
        print(f"UP主ID: {info['mid']}, 合集ID: {info['season_id']}")
        print("获取合集视频列表...")
        videos = fetch_season_videos(info["mid"], info["season_id"], args.cookie)
        print(f"共 {len(videos)} 个视频:")
        for i, v in enumerate(videos):
            d = v["duration"]
            print(f"  [{i+1}] {v['bvid']} | {d//60}:{d%60:02d} | {v['title']}")
        print()
        batch_result = extract_batch(videos, args.output_dir, args.cookie, args.browser)
        _print_batch_summary(batch_result)
        return

    # ===== 单视频模式 =====
    if url_type == "unknown":
        print(f"无法识别的输入: {args.url}")
        sys.exit(1)

    result = extract(args.url, args.cookie, args.browser)

    if not result["has_subtitle"]:
        print("\n该视频没有可用的字幕。")
        print(f"\n__RESULT_JSON__:{json.dumps(result, ensure_ascii=False)}")
        sys.exit(0)

    md_path, txt_path = save(result, args.output_dir)

    print(f"\n文件已保存:")
    print(f"  Markdown: {md_path}")
    print(f"  纯文本:   {txt_path}")

    plain = result["full_text"]
    print(f"\n{'=' * 60}")
    print(f"文字稿预览（前 1000 字，共 {len(plain)} 字）")
    print("=" * 60)
    print(plain[:1000])
    if len(plain) > 1000:
        print(f"\n... (共 {len(plain)} 字)")

    meta = {k: v for k, v in result.items() if k != "segments"}
    meta["md_path"] = md_path
    meta["txt_path"] = txt_path
    meta["segment_count"] = len(result["segments"])
    meta["char_count"] = len(plain)
    print(f"\n__RESULT_JSON__:{json.dumps(meta, ensure_ascii=False)}")


def _print_batch_summary(batch_result: dict):
    """打印合集批量提取的汇总"""
    s = batch_result["success"]
    f = batch_result["failed"]
    n = batch_result["no_subtitle"]
    total = len(s) + len(f) + len(n)
    total_segs = sum(x["segments"] for x in s)
    total_chars = sum(x["chars"] for x in s)

    print(f"\n{'=' * 60}")
    print("合集提取完成")
    print("=" * 60)
    print(f"  总计: {total} 个视频")
    print(f"  成功: {len(s)} 个 ({total_segs} 段, {total_chars} 字)")
    if n:
        print(f"  无字幕: {len(n)} 个")
    if f:
        print(f"  失败: {len(f)} 个")
    print(f"  目录: {batch_result['index_path']}")

    summary = {
        "total": total, "success_count": len(s),
        "no_subtitle_count": len(n), "failed_count": len(f),
        "total_segments": total_segs, "total_chars": total_chars,
        "index_path": batch_result["index_path"],
        "files": [{"bvid": x["bvid"], "title": x["title"],
                    "md": x["md_path"], "txt": x["txt_path"]} for x in s],
    }
    print(f"\n__RESULT_JSON__:{json.dumps(summary, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
