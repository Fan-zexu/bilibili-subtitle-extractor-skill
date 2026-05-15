#!/usr/bin/env python3
"""
B站视频字幕提取工具（纯标准库，无需第三方依赖）

支持输入类型：
  1. 单个视频URL: https://www.bilibili.com/video/BV1Qe4y1E77h
  2. 纯BV号: BV1Qe4y1E77h
  3. 合集/系列URL: https://space.bilibili.com/{mid}/lists/{series_id}?type=series
  4. 收藏夹合集URL: https://space.bilibili.com/{mid}/channel/collectiondetail?sid={season_id}

三条字幕获取路径自动降级：
  路径0: view 接口直接返回字幕
  路径1: /x/v2/dm/view (弹幕视图接口，不需要 WBI 签名)
  路径2: /x/player/wbi/v2 (播放器接口，需要 WBI 签名，Cookie 可选)
"""

import argparse
import hashlib
import json
import os
import re
import ssl
import sys
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


# ========== 三条获取路径 ==========

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

def extract(url: str, cookie: str = "") -> dict:
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

    # 三条路径依次尝试
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

    if not result:
        return {
            "has_subtitle": False, "title": title, "bvid": bvid,
            "duration": duration, "owner": owner, "language": "",
            "subtitle_type": "none", "source": "", "segments": [],
            "timestamped_text": "", "full_text": "",
        }

    # 选最佳字幕
    best = pick_best_subtitle(result["subtitles"])
    lang = best.get("lan_doc", best.get("lan", "未知"))
    lan_code = best.get("lan", "")
    sub_type = ("ai" if best.get("ai_type") or lan_code.startswith("ai-")
                else "auto" if "自动" in lang else "manual")

    sub_url = normalize_url(best.get("subtitle_url", ""))
    print(f"  字幕: {lang} ({sub_type}, {result['source']})")

    segments = fetch_json(sub_url, cookie).get("body", [])
    print(f"  共 {len(segments)} 段")

    return {
        "has_subtitle": True, "title": title, "bvid": bvid,
        "duration": duration, "owner": owner, "language": lang,
        "subtitle_type": sub_type, "source": result["source"],
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

def extract_batch(videos: list, output_dir: str, cookie: str = "") -> dict:
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
            result = extract(bvid, cookie)
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
        batch_result = extract_batch(videos, args.output_dir, args.cookie)
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
        batch_result = extract_batch(videos, args.output_dir, args.cookie)
        _print_batch_summary(batch_result)
        return

    # ===== 单视频模式 =====
    if url_type == "unknown":
        print(f"无法识别的输入: {args.url}")
        sys.exit(1)

    result = extract(args.url, args.cookie)

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
