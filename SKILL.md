---
name: bili-subtitle
description: >-
  从B站(bilibili)视频中提取字幕/文字稿。支持自动获取AI生成字幕和人工CC字幕，输出带时间戳的Markdown文字稿和纯文本TXT文件。
  支持单视频、合集/系列URL批量提取。
  当用户提到以下内容时使用：B站视频字幕、bilibili字幕提取、视频文字稿、视频转文字、提取B站视频内容、
  bilibili.com链接、BV号、b23.tv链接、抓取视频字幕、视频字幕下载、AI字幕、CC字幕。
  即使用户只是给了一个B站链接并问"帮我提取文字稿/字幕"，也应该触发此skill。
---

# B站视频字幕提取

从B站视频中提取字幕，生成带时间戳的Markdown文字稿和纯文本TXT文件。纯标准库实现为主，可选 yt-dlp 作为降级方案。

## 支持的输入类型

脚本支持四种输入：

1. **单视频URL**：`https://www.bilibili.com/video/BV1Qe4y1E77h`
2. **纯BV号**：`BV1Qe4y1E77h`
3. **系列(series)URL**：`https://space.bilibili.com/{mid}/lists/{series_id}?type=series`
4. **合集(season)URL**：`https://space.bilibili.com/{mid}/channel/collectiondetail?sid={season_id}` 或 `https://space.bilibili.com/{mid}/lists/{id}?type=season`

对于合集/系列URL，脚本会自动获取视频列表并逐个提取字幕，支持分页（每页30个，自动翻页获取全部），最终生成目录索引文件。

## 工作原理

脚本通过五条路径依次尝试获取字幕，自动降级：

1. **路径0 — view 接口**：调用 `/x/web-interface/view` 获取视频信息时，有时响应中直接包含字幕列表
2. **路径1 — dm/view 接口**：调用 `/x/v2/dm/view`（弹幕视图接口），不需要 WBI 签名和 Cookie，能获取大部分视频的 AI 字幕
3. **路径2 — player/wbi/v2 接口**：需要 WBI 签名，Cookie 可选但有助于获取更多 AI 字幕。当路径0和路径1都失败时使用
4. **路径3 — yt-dlp 降级**（需安装 yt-dlp）：当路径0/1/2都失败、或返回的字幕数据不完整时自动触发。通过 yt-dlp + 浏览器Cookie 获取字幕，特别适合以下场景：
   - 需要登录才能获取AI字幕的视频
   - 多分P视频（API仅返回部分分P的字幕）
   - API返回的字幕段数异常少（字幕密度低于阈值）
5. **路径4 — Whisper 语音识别**（需安装 openai-whisper + yt-dlp）：当路径0/1/2/3 均无法获取字幕时的最终兜底方案。先用 yt-dlp 下载音频，再用本地 Whisper 模型（默认 small）进行语音识别，适合完全没有字幕的视频。耗时较长，请耐心等待

### yt-dlp 降级触发条件

满足以下任一条件时自动触发路径3：

- API 完全没有返回字幕
- 多分P视频（分P数 > 1）但 API 返回的字幕段数 < 每P 5段
- 字幕密度低于 1段/分钟（视频时长 > 1分钟时检查）

路径3获取到字幕后，会与 API 结果比较，自动选择更完整的数据。

### Whisper 语音识别触发条件

路径0/1/2/3 全部失败（segments 为空）时自动触发路径4，使用 Whisper small 模型进行本地语音识别。

字幕语言按优先级自动选择：`zh-Hans > zh > zh-CN > ai-zh > en > ja > ko`

## 使用方式

运行脚本，传入B站视频URL、BV号或合集URL：

```bash
python3 <skill-dir>/scripts/extract_subtitle.py <URL或BV号> [--output-dir <输出目录>] [--cookie <Cookie字符串>] [--browser <浏览器>]
```

参数说明：
- 第一个参数：B站视频URL、纯BV号，或合集/系列URL
- `--output-dir`：输出文件保存目录，默认为当前工作目录
- `--cookie`：可选，B站登录Cookie（SESSDATA等），有助于路径0/1/2获取更多AI字幕
- `--browser`：可选，yt-dlp 降级（路径3/4）时提取Cookie的浏览器（默认 `chrome`，可选：`firefox`、`edge`、`safari`等）

### 示例

单视频提取：
```bash
python3 <skill-dir>/scripts/extract_subtitle.py "https://www.bilibili.com/video/BV1Qe4y1E77h" --output-dir ./output
```

合集批量提取：
```bash
python3 <skill-dir>/scripts/extract_subtitle.py "https://space.bilibili.com/476706561/lists/2795389?type=series" --output-dir ./output
```

使用 Firefox 浏览器Cookie（当Chrome不可用时）：
```bash
python3 <skill-dir>/scripts/extract_subtitle.py "BV1sD4y1v7oC" --output-dir ./output --browser firefox
```

## 输出文件

### 单视频模式

脚本会在输出目录下生成两个文件：

1. **`<视频标题>_字幕.md`** — Markdown格式，包含：
   - 视频元信息（BV号、时长、UP主、字幕语言和类型）
   - 获取来源（view / dm/view / player/wbi/v2 / yt-dlp / whisper）
   - 带时间戳的文字稿（`[HH:MM:SS] 文本`格式）
   - 按标点合并的纯文字稿段落

2. **`<视频标题>_字幕.txt`** — 纯文本格式，仅包含文字内容（无时间戳），适合直接阅读或喂给AI做后续加工

### 合集/系列模式

脚本会为每个有字幕的视频生成 `.md` + `.txt` 文件，另外生成一个目录索引：

- **`00_目录.md`** — 汇总文件，包含提取成功/无字幕/失败的统计和各视频链接

## 使用流程

1. 用户给出B站视频链接、BV号或合集/系列URL
2. 确认输出目录（默认当前工作目录）
3. 运行脚本提取字幕
4. 检查输出文件是否正确生成
5. 单视频：告诉用户文件路径，并展示文字稿前几百字作为预览
6. 合集：告诉用户目录索引路径和提取统计（成功/失败/无字幕数量）

## 依赖说明

- **核心功能**（路径0/1/2）：纯Python标准库（urllib、json、hashlib等），不需要 pip install 任何包
- **降级方案**（路径3）：需要安装 `yt-dlp`（`pip3 install yt-dlp`），且本地浏览器已登录B站。yt-dlp 未安装时自动跳过路径3
- **语音识别兜底**（路径4）：需要安装 `openai-whisper`（`pip3 install openai-whisper`）和 `yt-dlp`。首次运行会自动下载 Whisper small 模型（约461MB）。未安装时自动跳过路径4

## 注意事项

- 不是所有B站视频都有字幕，部分视频可能没有AI字幕也没有CC字幕
- 如果五条路径都未获取到字幕（包括 Whisper 未安装或转录失败），告知用户该视频暂无可用字幕
- 路径4（Whisper）耗时较长：small 模型在 CPU 上处理1小时视频约需10-30分钟，请提前告知用户
- 如果用户提供了Cookie，通过 `--cookie` 参数传入，可以提高路径0/1/2的AI字幕获取成功率
- yt-dlp 降级方案需要本地浏览器（默认Chrome）已登录B站，通过 `--browser` 参数可切换浏览器
- 合集批量提取时，每个视频之间会间隔1秒，避免触发B站频率限制
- 合集视频较多时（如几十上百个），提取过程可能需要较长时间，请耐心等待
- 多分P视频（如一个BV号包含几十上百个分P）是路径3的典型适用场景
