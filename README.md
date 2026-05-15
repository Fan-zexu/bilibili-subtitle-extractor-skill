# bilibili-subtitle-extractor-skill

B站(bilibili)视频字幕提取 CatPaw Skill。

从B站视频中自动提取 AI 字幕和 CC 字幕，输出带时间戳的 Markdown 文字稿和纯文本 TXT 文件。

## 功能特性

- **单视频提取**：支持视频URL和纯BV号
- **合集/系列批量提取**：自动获取视频列表并逐个提取，生成目录索引
- **三条获取路径自动降级**：view → dm/view → player/wbi/v2
- **纯标准库实现**：无需 pip install 任何第三方依赖
- **智能语言选择**：按优先级自动选择最佳字幕语言

## 支持的输入

| 类型 | 示例 |
|------|------|
| 视频URL | `https://www.bilibili.com/video/BV1Qe4y1E77h` |
| 纯BV号 | `BV1Qe4y1E77h` |
| 系列URL | `https://space.bilibili.com/{mid}/lists/{id}?type=series` |
| 合集URL | `https://space.bilibili.com/{mid}/channel/collectiondetail?sid={id}` |

## 使用方式

### 作为 CatPaw Skill

将本仓库放入 `~/.catpaw/skills/bili-subtitle/` 或项目的 `.catpaw/skills/bili-subtitle/` 目录下，CatPaw 会自动加载。

### 命令行直接使用

```bash
# 单视频
python3 scripts/extract_subtitle.py "https://www.bilibili.com/video/BV1Qe4y1E77h" --output-dir ./output

# 合集批量
python3 scripts/extract_subtitle.py "https://space.bilibili.com/476706561/lists/2795389?type=series" --output-dir ./output
```

## 输出

- `<视频标题>_字幕.md` — 带时间戳的 Markdown 文字稿
- `<视频标题>_字幕.txt` — 纯文本（适合喂给 AI 做后续加工）
- `00_目录.md` — 合集模式下的汇总索引

## License

MIT
