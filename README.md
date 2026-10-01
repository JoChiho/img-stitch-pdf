# img-stitch-pdf

把多张图片导出为**可读的多页 PDF**（桌面工具：Python + tkinter）。

> 默认行为是**一图一页**，不会把图片拼成一张巨图，因此导出几十上百张图时不会因超大 RGB 画布而卡死 / OOM。

## 功能

- 多选添加图片；列表展示，可调顺序
- **添加文件夹…**：递归扫描所有子目录，收集常见图片（jpg/jpeg/png/webp/bmp/tif/tiff；默认跳过 gif），按相对路径**自然排序**
- 上移 / 下移调整顺序；或选中后在输入框填目标位置点「移到第…位」（支持多选）；双击列表项也可设序号。可移除、清空
- **生成 PDF…**：**多页导出**，列表顺序 = 页序；每张图单独一页
- 尽量保留原图像素：JPEG 经 `img2pdf` **原样嵌入**；PNG 等走无损路径（不经 JPEG 二次有损压缩）
- 默认 PDF 文件名：来自文件夹时用**文件夹名**；否则同源目录名 / 首图名 / `images.pdf`
- **创建桌面快捷方式**（Windows `.lnk`：`pythonw.exe` + `main.py`，工作目录=项目目录）
- 导出在**后台线程**进行，界面可继续操作；状态栏显示进度（如 `3/98`）；导出中禁用「生成 PDF」按钮
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

- `pillow` — 非 JPEG / 特殊 PNG 的转换
- `img2pdf` — 多页 PDF 写入（JPEG 原样嵌入等）

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

1. 点「添加图片…」多选文件，或点「添加文件夹…」一次加载整个文件夹（自然排序）
2. 用「上移 / 下移」调整页序，必要时「移除选中」「清空列表」
3. 点「生成 PDF…」，在保存对话框确认路径（预填默认文件名：文件夹名等）
4. 等待状态栏进度（如 `正在导出 3/98…`）；完成后弹出提示
5. 打开 PDF 即可按页阅读，每页对应一张原图

## 项目结构

```
img-stitch-pdf/
├── main.py                      # GUI 与导出逻辑
├── create_desktop_shortcut.ps1  # 创建桌面快捷方式
├── requirements.txt             # 运行依赖
├── requirements-dev.txt         # 开发 / 测试依赖
├── tests/                       # pytest 单元测试
├── README.md
└── .gitignore
```

## 仓库

- 本地目录：`D:\workspace\king\img-stitch-pdf`
- GitHub：https://github.com/JoChiho/img-stitch-pdf
