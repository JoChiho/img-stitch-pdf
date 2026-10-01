# img-stitch-pdf

把多张**图片**和/或多个 **PDF** 导出为**可读的多页 PDF**（桌面工具：Python + tkinter）。

> 默认行为是按列表顺序组成多页 PDF：**一图一页**；PDF 文件贡献其全部页并直接合并（**不重新栅格化**）。不会把图片拼成一张巨图，因此导出几十上百张图时不会因超大 RGB 画布而卡死 / OOM。

## 功能

- 多选添加图片；**添加 PDF…** 多选 PDF；列表展示（标注 `[图]` / `[PDF]`），可调顺序
- **添加文件夹…**：递归扫描所有子目录，收集常见图片（jpg/jpeg/png/webp/bmp/tif/tiff；默认跳过 gif）以及 **`.pdf`**，按相对路径**自然排序**
- 上移 / 下移调整顺序；或选中后在输入框填目标位置点「移到第…位」（支持多选）；双击列表项也可设序号。可移除、清空
- **生成 PDF…**：列表顺序 = 页序；仅图片走 `img2pdf`；仅 PDF / 混合用 `pypdf` 合并页（图片先转临时 PDF 再合并）
- **默认导出目录**：全局设置（`%APPDATA%/img-stitch-pdf/config.json` 的 `output_dir`）；首次启动自动创建并使用 `文档/PDF导出`；工具栏「设定默认导出文件夹…」可更改；保存对话框以该目录为 `initialdir`；勾选「直接导出到默认文件夹」则免对话框直接保存
- 尽量保留原图像素：JPEG 经 `img2pdf` **原样嵌入**；PNG 等走无损路径；PDF 页不重新栅格化
- 默认 PDF 文件名：来自文件夹时用**文件夹名**；否则同源目录名 / **首文件 stem** / `merge.pdf`
- **创建桌面快捷方式**（Windows `.lnk`：`pythonw.exe` + `main.py`，工作目录=项目目录）
- 导出在**后台线程**进行，界面可继续操作；状态栏显示进度（如 `3/98`）；导出中禁用「生成 PDF」按钮
- **本地翻译后导出**（可选）：勾选「本地翻译后导出」或点同名按钮 → 对列表中的图片做 **EN→ZH**（EasyOCR + Argos Translate），在**原图同目录**生成 `translated_zh/*_zh` 新图（**绝不覆盖原图**），再按默认导出目录写出多页 PDF；**跳过 PDF / GIF**；全程后台线程，避免界面冻结
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
- `easyocr` — 本地 OCR（英文检测；**首次运行下载模型**，体积较大，需网络）
- `argostranslate` — 本地机器翻译 en→zh（**首次运行下载语言包**）

> Windows 上优先 EasyOCR（相对 PaddleOCR 更易装）。若 Argos 安装失败，可另行 `pip install deep-translator` 作为**联网**后备（非默认）。
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

1. 点「添加图片…」多选图片，或「添加 PDF…」多选 PDF，或「添加文件夹…」一次加载整个文件夹（含 PDF，自然排序）
2. 用「上移 / 下移」或「移到第…位」调整页序，必要时「移除选中」「清空列表」
3. （可选）点「设定默认导出文件夹…」更改全局导出目录；状态栏/标签显示当前路径
4. 点「生成 PDF…」：保存对话框打开在默认导出目录，预填文件名（文件夹名 / 首文件名 / `merge.pdf`）；也可勾选「直接导出到默认文件夹」跳过对话框
5. 等待状态栏进度（如 `正在导出 3/98…`）；完成后弹出提示（页数为实际 PDF 页数）
6. 打开 PDF 即可按页阅读：图片各占一页，原 PDF 的页按列表顺序插入

### 本地翻译后导出

1. 列表中加入英文截图 / 图片（可混有 PDF，翻译时会跳过 PDF 并提示）
2. 勾选「本地翻译后导出」，再点「生成 PDF…」；或直接点「本地翻译后导出…」
3. 状态栏会显示翻译进度；**首次**会下载 EasyOCR 与 Argos 模型，请保持网络畅通，界面不阻塞
4. 每张原图旁生成 `原名_zh.扩展名`（例如 `page1.jpg` → `page1_zh.jpg`），再导出多页 PDF 到默认导出目录 / 保存对话框所选路径

命令行单独试一张图：

```bash
python translate_local.py path\to\image.jpg
```

## 质量与限制

- OCR 对小字、艺术字、低对比、倾斜文本效果有限；可能漏检或框不准
- Argos 为轻量离线翻译，专有名词 / 长难句质量一般，不如云端大模型
- 叠字：半透明白底 + 自适应字号居中；复杂排版（多栏、竖排）效果一般
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
  "output_dir": "C:\\Users\\你\\Documents\\PDF导出"
}
```

首次启动若尚未配置，会自动创建 `文档/PDF导出`（或 `~/Documents/PDF导出`）并写入该文件。

## 项目结构

```
img-stitch-pdf/
├── main.py                      # GUI 与导出逻辑
├── translate_local.py           # 本地 EN→ZH（OCR / MT / 叠字）
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
