# img-stitch-pdf

把多张**图片**和/或多个 **PDF** 导出为**可读的多页 PDF**（桌面工具：Python + tkinter）。

> 默认行为是按列表顺序组成多页 PDF：**一图一页**；PDF 文件贡献其全部页并直接合并（**不重新栅格化**）。不会把图片拼成一张巨图，因此导出几十上百张图时不会因超大 RGB 画布而卡死 / OOM。

## 功能

- 多选添加图片；**添加 PDF…** 多选 PDF；列表展示（标注 `[图]` / `[PDF]`），可调顺序
- **分区界面**：添加 / 排序 / 视图 / 文件列表 / 设定 / 翻译 / 导出 分组；浅色强调色条与 clam 主题，便于扫视；默认窗口约 1040×720
- **视图切换**：「列表」与「缩略图」；缩略图网格显示序号与文件名，支持多选后移除 / 移到第…位；缩略图懒加载并缓存
- **添加文件夹…**：收集常见图片（jpg/jpeg/png/webp/bmp/tif/tiff；默认跳过 gif）以及 **`.pdf`**，按相对路径**自然排序**；勾选 **「包含子文件夹」**（默认开，写入 `config.json` 的 `include_subfolders`）则递归子目录，取消则仅顶层
- **拖放添加（Windows）**：将图片、PDF 或文件夹拖到「文件列表」即可加入；文件夹是否递归子目录跟随「包含子文件夹」勾选
- 上移 / 下移调整顺序；或选中后在输入框填目标位置点「移到第…位」（支持多选）；双击列表项也可设序号。可移除、清空
- **生成 PDF…**：列表顺序 = 页序；仅图片走 `img2pdf`；仅 PDF / 混合用 `pypdf` 合并页（图片先转临时 PDF 再合并）
- **默认导出目录**：全局设置（`%APPDATA%/img-stitch-pdf/config.json` 的 `output_dir`）；首次启动自动创建并使用 `文档/PDF导出`；工具栏「设定默认导出文件夹…」可更改；保存对话框以该目录为 `initialdir`；勾选「直接导出到默认文件夹」则免对话框直接保存
- 尽量保留原图像素：JPEG 经 `img2pdf` **原样嵌入**；PNG 等走无损路径；PDF 页不重新栅格化
- 默认 PDF 文件名：来自文件夹时用**文件夹名**；否则同源目录名 / **首文件 stem** / `merge.pdf`
- **创建桌面快捷方式**（Windows `.lnk`：`pythonw.exe` + `main.py`，工作目录=项目目录）
- 导出在**后台线程**进行，界面可继续操作；状态栏显示进度（如 `3/98`）；导出中禁用「生成 PDF」按钮
- **本地翻译后导出**：勾选或点「本地翻译后导出」→ 对列表中的图片做 **EN→ZH**（PaddleOCR（默认）/ EasyOCR 回退 + Ollama / Argos）。每张图：OCR 阅读顺序后把**全部英文拼成一段**，**只翻译一次**，在原图**下方追加**那一条中文译文条带（自动换行、CJK 字体）；写入 `translated_zh/*_zh`（**不覆盖原图**），再导出多页 PDF（跳过 PDF / GIF）。界面提供 **Ollama 模型下拉框**（`ollama list`），选择写入 `config.json` 的 `ollama_model`。
- **OCR 文本导出 / 本地翻译 / 译文导入**：点「导出英文OCR文本」对列表图片做 OCR，生成带机器稳定键 `===PAGE NNN===` 的 UTF-8 `.txt`（含 `path:` 行）；可点「本地翻译OCR文本」（Ollama 按页）或外部译成中文后点「导入译文并生成图/PDF」，按页序号映射回列表，在原图下方追加中文译文条写入 `translated_zh/`，可选再导出 PDF。Ollama 本地翻译路径保持不变。
- **下载视频（yt-dlp）**：「设定」区点「下载视频…」，粘贴链接（可多行，按顺序排队）→ 下载；等价于 `yt-dlp -f "bv*+ba/b" -P "%USERPROFILE%\Downloads" "<URL>"`，带进度条、实时日志与取消
- 底部状态栏提示进度与结果

## 环境要求

- Python 3.9+（推荐 3.10+）
- Windows / macOS / Linux（需可用的 tkinter）

## 安装

在项目目录下执行：

```bash
pip install -r requirements.txt
```

依赖：

- `pillow` — 非 JPEG / 特殊 PNG 的转换；翻译叠字
- `img2pdf` — 多页 PDF 写入（JPEG 原样嵌入等）
- `pypdf` — PDF 页合并（不重新栅格化）
- `paddlepaddle` + `paddleocr` — 默认本地 OCR（英文；**首次运行下载模型**；Windows 用官方 CPU 索引安装 paddlepaddle）
- `easyocr` — OCR 回退（Paddle 失败或 `ocr_engine=easyocr` 时）
- `argostranslate` — 本地机器翻译 en→zh（**首次运行下载语言包**）
- **Ollama**（推荐本地 MT）：安装 https://ollama.com ，执行 `ollama serve`，并 `ollama pull qwen2.5:14b`（内存 ≥ 约 16GB；内存较低时用 `qwen2.5:7b` / `qwen2.5:3b`）。GUI **模型下拉框**列出 `ollama list` 结果，默认优先 `qwen2.5:14b`（若已安装），选择持久化到 `config.json` 的 `ollama_model`；也可用环境变量 `IMG_STITCH_OLLAMA_MODEL` / `IMG_STITCH_OLLAMA_HOST` 覆盖。

> 默认 OCR 为 **PaddleOCR（EN）**；失败时自动回退 EasyOCR。Windows 安装 paddlepaddle CPU：
> `python -m pip install paddlepaddle -i https://www.paddlepaddle.org.cn/packages/stable/cpu/`
> （应用 `ensure_deps` 也会自动按该索引安装。）若 Ollama 未运行则自动回退 Argos；若 Argos 也失败，可再 `pip install deep-translator` 作为**联网**后备（非默认）。
>
> 中文字体：自动查找 `msyh.ttc` / `simhei.ttf` 等；也可设环境变量 `IMG_STITCH_CJK_FONT` 指向字体文件。

可选：使用虚拟环境

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate
pip install -r requirements.txt
```

开发 / 运行测试：

```bash
pip install -r requirements-dev.txt
pytest
```

## 启动

```bash
python main.py
```

### 桌面快捷方式（Windows）

任选其一：

1. **应用内**：打开程序后点「创建桌面快捷方式」
2. **脚本**：在项目目录执行

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\create_desktop_shortcut.ps1
```

快捷方式默认创建在当前用户桌面，名称为 `图片导出PDF.lnk`：

- 目标：`pythonw.exe`
- 参数：项目下的 `main.py`（带引号）
- 起始位置：项目目录

双击快捷方式即可无控制台窗口启动。

## 使用说明

1. 点「添加图片…」多选图片，或「添加 PDF…」多选 PDF，或「添加文件夹…」一次加载文件夹（含 PDF，自然排序；可用「包含子文件夹」控制是否递归）
2. 用「上移 / 下移」或「移到第…位」调整页序，必要时「移除选中」「清空列表」；可在「列表 / 缩略图」之间切换查看
3. （可选）点「设定默认导出文件夹…」更改全局导出目录；状态栏/标签显示当前路径
4. 点「生成 PDF…」：保存对话框打开在默认导出目录，预填文件名（文件夹名 / 首文件名 / `merge.pdf`）；也可勾选「直接导出到默认文件夹」跳过对话框
5. 等待状态栏进度（如 `正在导出 3/98…`）；完成后弹出提示（页数为实际 PDF 页数）
6. 打开 PDF 即可按页阅读：图片各占一页，原 PDF 的页按列表顺序插入

### 本地翻译后导出

1. 列表中加入英文截图 / 图片（可混有 PDF，翻译时会跳过 PDF 并提示）
2. （可选）在 **Ollama 模型** 下拉框中选择本地模型（列表来自 `ollama list` / API tags）；默认若已安装则选 `qwen2.5:14b`，选择会写入 `%APPDATA%/img-stitch-pdf/config.json` 的 `ollama_model`
3. 勾选「本地翻译后导出」，再点「生成 PDF…」；或直接点「本地翻译后导出…」
4. 状态栏会显示翻译进度；**首次会准备 EasyOCR 与 Ollama/Argos**（Ollama 需事先 `ollama pull`；Argos 会下载语言包）
5. 每张图：OCR 阅读顺序 → 全部英文空格拼成**一段** → **一次**机器翻译 → 原图下方一条中文译文；输出为 `translated_zh/原名_zh.扩展名`，再导出多页 PDF



### 导出英文 OCR / 本地翻译 / 导入译文

适合用本地 Ollama 或外部翻译（人工、其它 MT）而仍要 `translated_zh/` 图或 PDF 的流程：

1. 列表顺序与最终页序一致后，点 **「导出英文OCR文本」**，保存 UTF-8 `.txt`
2. 文本格式（请保留标记；只改英文正文为中文）：

```text
===PAGE 001===
path: relative/or/basename.jpg

english text from OCR...

===PAGE 002===
path: another.png

more english...
```

3. 可选：点 **「本地翻译OCR文本」**（使用当前 Ollama 模型，按页翻译并保留 ===PAGE NNN=== 键）保存中文 TXT；也可外部翻译。
4. 点 **「导入译文并生成图/PDF」** 选择中文 TXT
5. 按 `===PAGE NNN===` 的页序号映射到当前列表中第 N 张可处理图片，写入 `translated_zh/`；可选择是否再导出多页 PDF

映射键是**页序号**（不是文件名）。导出与导入时列表顺序须一致。

命令行单独试一张图：

```bash
python translate_local.py path\to\image.jpg
```


## 下载视频（yt-dlp）

主窗口「设定」区点 **下载视频…** 打开窗口（与 PDF 功能互不影响，可同时使用）：

1. 打开时若剪贴板里是 http(s) 链接会自动填入；也可点 **粘贴**。可一次粘贴多个链接（每行一个），会按顺序逐个下载
2. **保存到**：默认 `%USERPROFILE%\Downloads`；**选择文件夹…** 修改后写入 `config.json` 的 `video_download_dir`；**打开文件夹** 用资源管理器打开
3. **只下载单个视频（--no-playlist）** 默认勾选；**浏览器 Cookies**（无 / chrome / edge / firefox）用于需要登录的网站（`--cookies-from-browser`）
   - **Cookies 文件**：点 **选择…** 选浏览器扩展导出的 `cookies.txt`（Netscape 格式），**清除** 取消；路径记在 `config.json` 的 `video_cookies_file`。设置后传 `--cookies`，**优先于浏览器 Cookies**（两者不会同时传）
   - 扩展导出的文件常有格式问题（如 `.x.com\tFALSE\t/…`：域名以 `.` 开头但第 2 列是 `FALSE`），yt-dlp 会报 `invalid Netscape format cookies file`。程序每次下载前会在 `%TEMP%` 写一份**整理后的临时副本**（补 `# Netscape HTTP Cookie File` 头；只保留 7 列 Tab 分隔的行，含 `#HttpOnly_` 行；第 2 列按域名是否以 `.` 开头改为 `TRUE`/`FALSE`；兼容 UTF-8 BOM 与 CRLF），传给 yt-dlp，运行结束即删除；原文件不改动，日志不打印 cookie 值
   - 日志出现 `No video could be found`（且未设置 Cookies）时会提示：该推文可能需要登录，请选择 Cookies 文件；Chrome/Edge 的 `--cookies-from-browser` 报 `Could not copy Chrome cookie database` 时会提示先完全关闭浏览器，或改用 Cookies 文件
4. 点 **下载**：进度条与日志实时显示（解析 `yt-dlp --newline` 输出的百分比、速度、剩余时间）；**取消** 会结束 yt-dlp 及其子进程（如 ffmpeg）

实际执行的命令（每个链接一条，日志里会打印）：

```text
yt-dlp -f bv*+ba/b -P <保存目录> --newline [--no-playlist] [--cookies <整理后的临时副本> | --cookies-from-browser X] <URL>
```

- **yt-dlp 定位**：优先 PATH 上的 `yt-dlp`，否则用当前 Python 的 `python -m yt_dlp`；都没有时提示一键 `pip install yt-dlp`。窗口里的 **更新 yt-dlp** 执行 `pip install -U yt-dlp`（网站改版导致下载失败时先更新）
- **ffmpeg**：`bv*+ba` 需要 ffmpeg 合并音视频。未找到时窗口会中文提示，安装：`winget install Gyan.FFmpeg`（装好后重开程序；刚装完 PATH 未刷新时也会自动在 WinGet 目录里找到并传 `--ffmpeg-location`）
- 下载在后台线程 + 子进程（Windows 无黑框）运行，界面不卡；后台线程只通过队列把进度交给 Tk 主线程（`after()` 轮询），不直接操作界面
- 逻辑在 `video_dl.py`（无 Tk 依赖，有单元测试）；请只下载你有权保存的内容

## 本地 MT（Ollama）

推荐使用 [Ollama](https://ollama.com) 作为本地 EN→ZH 引擎（质量通常优于 Argos）：

1. 安装 Ollama（Windows 可用 winget：`winget install Ollama.Ollama`）。
2. 保持服务运行：`ollama serve`（安装后通常已在后台）。
3. 按内存拉取模型：
   - **≥ 约 16GB RAM**：`ollama pull qwen2.5:14b`（或 `qwen2.5:7b`）
   - **内存较低**：`ollama pull qwen2.5:3b`
4. 在 GUI 的 **Ollama 模型** 下拉框选择模型（等同 `ollama list`）；或设环境变量：
   - `IMG_STITCH_OLLAMA_MODEL`（默认 `qwen2.5:14b`）
   - `IMG_STITCH_OLLAMA_HOST`（默认 `http://127.0.0.1:11434`）
5. 若 Ollama 不可用，应用会**回退 Argos** 并在状态里提示。

每张图默认把 OCR 得到的全部英文按阅读顺序拼成**一个段落**再翻译一次（整页一条译文）。附近框合并（`merge_nearby`）仅作可选回退，不是默认路径。


## 质量与限制

- OCR 对小字、艺术字、低对比、倾斜文本效果有限；可能漏检或框不准
- 默认优先 **Ollama**（如 qwen2.5）做 EN→ZH；不可用时回退 Argos。
- 默认**不再叠字覆盖原图**：OCR 阅读顺序后将**全部英文拼成一段** → 一次 Ollama/Argos 翻译 → 在原图下方追加浅色译文条带（`font.getbbox` 换行与量高，CJK 字体、充足边距）。附近框合并仅为可选回退（`merge_nearby=True`）。旧的框内叠字路径仅保留为 `render_mode="overlay"`（非默认）。
- 译文条带：浅色底 + 自动换行左对齐；原图文字框保持不变；复杂排版依赖 OCR 阅读顺序
- 翻译模式**不处理 PDF 内嵌文字**（跳过 PDF）；也不处理 GIF
- 首次模型下载可能数百 MB；之后离线可用（Argos 路径）；EasyOCR 依赖 PyTorch，安装较慢

## 配置

默认导出目录保存在用户配置文件中（跨项目运行持久化）：

| 平台 | 路径 |
|------|------|
| Windows | `%APPDATA%\img-stitch-pdf\config.json` |
| macOS / Linux | `~/.config/img-stitch-pdf/config.json` |

示例：

```json
{
  "output_dir": "C:\Users\你\Documents\PDF导出",
  "ollama_model": "qwen2.5:14b",
  "ocr_engine": "paddle",
  "include_subfolders": true,
  "ocr_downscale": false,
  "ocr_downscale_max_long_side": 1600,
  "video_download_dir": "C:\Users\你\Downloads",
  "video_no_playlist": true,
  "video_cookies_browser": "无",
  "video_cookies_file": "C:\Users\你\Downloads\cookies.txt"
}
```

其中 `ollama_model` 由 GUI 模型下拉框写入；未配置时若本机已安装则默认优先 `qwen2.5:14b`。
`include_subfolders`（布尔，默认 `true`）控制「添加文件夹…」是否递归子目录，与界面勾选「包含子文件夹」同步。
`ocr_downscale`（布尔，默认 `false`）与 `ocr_downscale_max_long_side`（整数，默认 `1600`）：GUI「OCR降采样」勾选写入；开启后 OCR 文本导出与本地翻译在调用 Paddle/EasyOCR 前将图片最长边缩小到该值（检测框坐标会映射回原图尺寸）。
`video_download_dir` / `video_no_playlist` / `video_cookies_browser`：「下载视频」窗口的保存目录（默认 `%USERPROFILE%\Downloads`）、是否 `--no-playlist`（默认 `true`）、`--cookies-from-browser` 浏览器（`无` / `chrome` / `edge` / `firefox`）。
`video_cookies_file`：「Cookies 文件」路径（`--cookies`，优先于 `video_cookies_browser`；点「清除」后从配置中移除）。

首次启动若尚未配置导出目录，会自动创建 `文档/PDF导出`（或 `~/Documents/PDF导出`）并写入该文件。

## 项目结构

```
img-stitch-pdf/
├── main.py                      # GUI 与导出逻辑
├── translate_local.py           # 本地 EN→ZH（OCR / MT / 下方译文条带）
├── video_dl.py                  # 下载视频：yt-dlp 命令构建 / 进度解析 / 队列与取消
├── ocr_backend.py               # PaddleOCR / EasyOCR 引擎与自动安装
├── create_desktop_shortcut.ps1  # 创建桌面快捷方式
├── requirements.txt             # 运行依赖（含可选翻译栈）
├── requirements-dev.txt         # 开发 / 测试依赖
├── tests/                       # pytest 单元测试
├── README.md
└── .gitignore
```

## 仓库

- 本地目录：`D:\workspace\king\img-stitch-pdf`
- GitHub：https://github.com/JoChiho/img-stitch-pdf

## UI 响应性（OCR / 翻译 / PDF）

- 所有 OCR、Ollama 翻译、PDF 合并均在**后台线程**执行；Tk 主线程只通过队列刷新状态。
- 导出过程中可点 **取消**；状态栏约每 3 秒心跳更新（避免 Windows「未响应」）。
- Ollama HTTP 有超时（默认见 `IMG_STITCH_OLLAMA_TIMEOUT`，秒）。超时会给出明确错误；大模型（如 `qwen2.5:32b`）可增大该值。
- 启动时不在 UI 线程阻塞导入/拉取 Ollama 模型列表（后台刷新）。

