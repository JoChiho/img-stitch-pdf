# img-stitch-pdf

多图拼接并导出为单个 PDF 的简易桌面工具（Python + tkinter）。

## 功能

- 多选添加图片，列表展示并保持顺序
- 上移 / 下移调整顺序，移除或清空
- 纵向拼接（默认）或横向拼接
- 可选统一尺寸：纵向统一宽度、横向统一高度（留空则取图片中的最大值）
- 导出为单页 PDF（Pillow + img2pdf）
- 界面与状态提示为中文

## 环境要求

- Python 3.9+（建议 3.10+）
- Windows / macOS / Linux（需可用的 tkinter）

## 安装

在项目目录下执行：

```bash
pip install -r requirements.txt
```

依赖：

- `pillow` — 读图、缩放、拼接
- `img2pdf` — 将拼接结果写入 PDF（若未安装则会回退到 Pillow 直接写 PDF）

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

## 使用说明

1. 点击「添加图片…」多选图片文件
2. 用「上移 / 下移」调整拼接顺序，必要时「移除选中」或「清空列表」
3. 选择「纵向拼接」或「横向拼接」；可按需填写统一尺寸（像素）
4. 点击「导出 PDF…」选择保存路径
5. 底部状态栏会显示进度与错误信息

## 项目结构

```
img-stitch-pdf/
├── main.py            # GUI 入口
├── requirements.txt   # 依赖
├── README.md
└── .gitignore
```

## 仓库

- 本地目录：`D:\workspace\king\img-stitch-pdf`
- GitHub：https://github.com/JoChiho/img-stitch-pdf
