# img-stitch-pdf

多图拼接并导出为单页 PDF 文件的桌面工具（Python + tkinter）。

## 功能

- 多选添加图片，列表展示，可调顺序
- **添加文件夹…**：自动加载文件夹内全部常见图片（jpg/jpeg/png/webp/bmp/tif/tiff/gif），按文件名**自然排序**
- 上移 / 下移调整顺序；移除、清空
- 纵向拼接（默认）或横向拼接
- **输出像素质量默认与原图一致**：留空「统一尺寸」时按图中最大边对齐，**只放大较小图，绝不缩小最大原图**（LANCZOS）；可选填写更大尺寸仅作进一步放大
- 导出为单页 PDF（Pillow + img2pdf）；能原样嵌入 JPEG 时不重编码，否则用无损 PNG 再写入 PDF，避免 JPEG 二次压缩损失
- 默认 PDF 文件名：来自文件夹时用**文件夹名**；否则用首图名或 `stitch.pdf`
- **创建桌面快捷方式**（Windows `.lnk`，`pythonw.exe` + `main.py`，工作目录=项目目录）
- 底部状态栏显示进度与结果

## 环境要求

- Python 3.9+（推荐 3.10+）
- Windows / macOS / Linux（需可用的 tkinter）

## 安装

在项目目录下执行：

```bash
pip install -r requirements.txt
```

依赖：

- `pillow` — 读图、缩放、拼接
- `img2pdf` — 将拼接结果写入 PDF（若未安装则回退到 Pillow 直接写 PDF）

可选：使用虚拟环境

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate
pip install -r requirements.txt
```

## 运行

```bash
python main.py
```

### 桌面快捷方式（Windows）

任选其一：

1. **应用内**：打开程序后点击「创建桌面快捷方式」
2. **脚本**：在项目目录执行

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\create_desktop_shortcut.ps1
```

快捷方式默认创建在当前用户桌面，名称为 `图片拼接转PDF.lnk`：

- 目标：`pythonw.exe`
- 参数：项目下的 `main.py`（带引号）
- 起始位置：项目目录

双击快捷方式即可无控制台窗口启动。

## 使用说明

1. 点「添加图片…」多选文件，或点「添加文件夹…」一次加载整个文件夹（自然排序）
2. 用「上移 / 下移」调整拼接顺序，必要时「移除选中」「清空列表」
3. 选择「纵向拼接」或「横向拼接」；统一尺寸建议**留空**（保持最大原图像素，只放大较小图）
4. 点「导出 PDF…」；保存对话框会预填默认文件名（文件夹名或首图名）
5. 底部状态栏会显示进度与结果信息

## 项目结构

```
img-stitch-pdf/
├── main.py                      # GUI 入口
├── create_desktop_shortcut.ps1  # 创建桌面快捷方式
├── requirements.txt             # 依赖
├── README.md
└── .gitignore
```

## 仓库

- 本地目录：`D:\workspace\king\img-stitch-pdf`
- GitHub：https://github.com/JoChiho/img-stitch-pdf
