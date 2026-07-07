# SVGEval

SVGEval 用于评估“图像二值化结果再转换为 SVG”之后的效果。它不依赖人工标注 ground truth，而是把二值图作为参考，检查 SVG 渲染回像素后是否忠实保留二值结果，同时统计二值图和 SVG 的结构复杂度。

输出是一个 CSV 表格：第一行是全部样本的平均值，后面每一行是单个样本的指标。

## 1. 克隆代码

```bash
git clone https://github.com/Junyiggg/SVGEval.git
cd SVGEval
```

如果你是直接下载 zip，也可以解压后进入项目根目录。

## 2. 创建环境

推荐使用 Python 3.10 或更高版本。

使用 conda：

```bash
conda create -n svgeval python=3.10
conda activate svgeval
```

或者使用已有环境：

```bash
python --version
```

## 3. 安装依赖

```bash
pip install -r requirements.txt
```

依赖包括：

- `pillow`：读取二值图、处理 SVG 渲染后的 PNG。
- `numpy`：像素级计算。
- `scikit-image`：连通域统计。
- `cairosvg`：把 SVG 渲染回像素图。

Windows 上如果 `cairosvg` 报 Cairo 相关错误，建议用 conda-forge 安装：

```bash
conda install -c conda-forge cairosvg
```

## 4. 整理输入文件夹

程序需要两个文件夹：

```text
your_data/
  binaries/
    sample_001.png
    sample_002.png
  svgs/
    sample_001.svg
    sample_002.svg
```

要求：

- `binaries/` 放二值化结果图，支持 `.png/.jpg/.jpeg/.bmp/.webp/.tif/.tiff`。
- `svgs/` 放 SVG 结果，扩展名为 `.svg`。
- 样本通过文件名 stem 配对，例如 `sample_001.png` 对应 `sample_001.svg`。
- 文件夹可以有子目录，但同一个输入类型里不能出现重复 stem。
- 默认约定是黑色前景、白色背景。

没有配对成功的文件不会参与平均值，清单会写到 `unmatched_files.txt`。

## 5. 运行评估

在项目根目录运行：

```bash
python -m binary_svg_eval.evaluate \
  --binary-root "path/to/binaries" \
  --svg-root "path/to/svgs" \
  --out-dir "path/to/output"
```

Windows PowerShell 可以写成：

```powershell
python -m binary_svg_eval.evaluate `
  --binary-root "D:\data\binaries" `
  --svg-root "D:\data\svgs" `
  --out-dir "D:\data\svgeval_output"
```

## 6. 查看输出

输出目录包含：

```text
evaluation.csv
unmatched_files.txt
```

`evaluation.csv` 的第一行 `row_type` 为 `AVERAGE`，表示所有样本的平均值；后面每行 `row_type` 为 `SAMPLE`，表示单个样本的结果。

CSV 包含这些列：

```text
row_type
sample_name
binary_file
svg_file
foreground_area_ratio
component_count
small_component_count
small_component_ratio
svg_binary_precision
ssim_binary_svg
num_paths
num_path_commands
tiny_path_count
tiny_path_ratio
notes
```

## 7. 指标说明

### foreground_area_ratio

二值图中前景像素占整张图像的比例。默认黑色像素为前景。

这个指标用于发现二值化结果是否明显过黑或过白。如果比例接近 0，可能说明主体被漏掉；如果比例接近 1，可能说明背景被错误地归入前景。它不是越大越好，也不是越小越好，而是用于发现异常样本。

### component_count

二值图前景连通域数量，使用 8 邻域连通。

连通域过多通常意味着噪点、断裂笔画或碎边较多；连通域过少可能意味着原本分开的结构被粘连。这个指标主要反映二值化结果的结构完整性和碎片程度。

### small_component_count

面积很小的前景连通域数量。默认小连通域阈值是整张图像面积的 `0.0002`，并且至少为 4 个像素。

这个指标主要用于发现噪点和细碎残片。数量越高，说明二值图中越可能存在大量小碎片，转换成 SVG 后也更容易产生很多小路径。

### small_component_ratio

所有小连通域面积之和占全部前景面积的比例。

`small_component_count` 关注“小碎片有多少个”，`small_component_ratio` 关注“小碎片占前景多少面积”。如果数量高但比例低，通常是零散小噪点；如果比例也高，说明碎片已经占据较多前景区域。

### svg_binary_precision

SVG 渲染回像素后，SVG 前景中有多少比例也属于二值图前景：

```text
svg_binary_precision = SVG 与二值图重合的前景像素 / SVG 前景像素
```

它主要衡量 SVG 是否画出了额外前景。数值低通常说明 SVG 结果比二值图更粗、更脏，或者存在额外填充、错误闭合、背景误判等问题。

### ssim_binary_svg

二值图和 SVG 渲染图之间的全局结构相似度。

它比较两张 mask 的整体均值、方差和协方差，比单纯逐像素错误更关注整体结构是否接近。数值越高，说明 SVG 渲染结果越接近二值图的整体形状和分布。

### num_paths

SVG 中 `<path>` 元素的数量。

很多图像转 SVG 工具会把前景轮廓表达成 path。`num_paths` 高通常意味着 SVG 中存在很多独立路径，可能来自噪点、碎片或过度追踪。注意：如果工具把多个轮廓合并进一个 compound path，这个值可能较低，需要结合 `num_path_commands` 和 `tiny_path_count` 一起看。

### num_path_commands

所有 path 中路径命令数量的估计，包括 `M/L/C/Q/A/Z` 等命令及其重复参数组。

它比 `num_paths` 更能反映 path 内部复杂度。即使只有一个 `<path>`，如果里面有大量命令，SVG 仍然会很复杂、难编辑、渲染成本也可能更高。

### tiny_path_count

包围盒面积很小的 path/subpath 数量。默认阈值是整张图像面积的 `0.0002`。

这里会按 path 内部的独立 subpath 统计，因此一个 compound path 里包含很多小轮廓时也能被发现。大量 tiny path 通常意味着噪点、毛刺、断裂边缘或过度矢量化。

### tiny_path_ratio

小 path/subpath 数量占全部 path/subpath 数量的比例。

这个指标用于判断 SVG 中碎片路径的占比。`tiny_path_count` 高说明小碎片多；`tiny_path_ratio` 高说明 SVG 的路径结构主要由小碎片组成。它适合和 `small_component_count` 一起看：如果二值图小连通域很多，问题可能来自二值化；如果二值图较干净但 tiny path 很多，问题可能来自 SVG 转换过程。

## 8. 注意事项

- 本工具不输出综合评分，因为没有人工标注 ground truth。
- 指标应该分开看：二值图结构、SVG 像素一致性、SVG 路径复杂度分别反映不同问题。
- 当前版本默认黑色为前景，白色为背景。
- CSV 使用 UTF-8 with BOM 编码，便于在 Excel 中直接打开。
