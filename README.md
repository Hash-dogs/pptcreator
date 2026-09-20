# 文档转 PPT 生成器

用公司模板（`迈胜PPT模板.pptx`）生成 PPT：把文档内容按 12 套版式渲染成正文页，
外面套公司的封面 / 目录 / 封底，并带一套可回归的 QA。

> 方案与决策记录见 [`docs/方案-文档转PPT生成器.md`](docs/方案-文档转PPT生成器.md)
> zcode skill 解读见 [`docs/pptx-skill-分析.md`](docs/pptx-skill-分析.md)

---

## 当前进度

| 阶段 | 状态 |
|---|---|
| 最小验证（能否在公司模板 layout 上自由画） | ✅ |
| 设计令牌 + 12 套版式 | ✅ |
| QA 脚本（几何 + 结构自检） | ✅ |
| AI 看图闭环 | ✅（模型未配置时输出人工复核包） |
| **解析 → 大纲 → 人工确认 → 规划 → 渲染** | ✅ 链路已通 |

> ⚠️ **回退模式 ≠ 可用产出。** 未配置模型时，大纲与规划走确定性回退：
> 能跑完、版式合法、结构自检通过，但**内容取舍、标题主张、版式与语义的匹配**
> 都做不了，产出会明显单调（实测 18 页里 12 页同一种版式），
> 且原文直接塞入常超出框高（几何检查会报 `text_overflow`）。
> **要拿到可用的东西，必须配模型。**

---

## 配置

```bash
cp .env.example .env     # 然后填 key
python run.py config     # 看当前配置状态
```

`.env.example` 里有各家服务商（智谱 / DeepSeek / 通义 / OpenAI / Azure / 本地 vLLM）的填法。
两组配置：

- `PPTGEN_LLM_*` —— 文本模型，用于**生成大纲**与**规划每页版式**
- `PPTGEN_VISION_*` —— 视觉模型，用于**AI 看图验收**（需支持 base64 图片输入）

视觉留空且 `PPTGEN_VISION_FALLBACK_TO_LLM=1` 时会回退复用文本模型配置。
**两者都没配也能跑完整条链路**，只是质量退化。

---

## 跑起来（Web 前端 · 推荐）

```bat
cd C:\gyb-software\programs\program-pptcreator02
.venv\Scripts\python.exe server.py
```
然后浏览器打开 **http://127.0.0.1:8000**

页面是三步向导：

1. **选源文档** —— 从项目根目录的文档里挑一个，点「生成大纲」
2. **审阅并修改大纲** —— 章节名、页标题**直接在页面上改**（这是人工确认的落点），
   还能设修复轮数与输出文件名
3. **生成进度** —— 实时日志（规划分批进度、几何检查结果都会打出来）
4. **预览与下载** —— 逐页渲染图 + 几何检查徽章 + 下载 pptx

换端口：`server.py --port 8080`。只用标准库，无额外依赖。

---

## 跑起来（命令行）

项目自带 `.venv`，依赖已装好。

```bash
cd C:\gyb-software\programs\program-pptcreator02
set PY=.venv\Scripts\python.exe
```

**一条命令走完（推荐）**

```bat
%PY% run.py full --src "Dify 介绍与实战.pptx"          :: 做到大纲就停下等你确认
%PY% run.py full --src "Dify 介绍与实战.pptx" --yes    :: 跳过确认一路走完
```

`--yes` 会依次执行：解析 → 大纲 → 规划 → **修复回环** → 渲染 → QA，成品落在
`out\samples\`，渲染图与评审包落在 `out\visual\`。

**分步执行（要逐步把关时用）**

```bat
%PY% run.py config                                  :: 看配置状态
%PY% run.py outline --src "Dify 介绍与实战.pptx"     :: 生成大纲
::   → 打开 out\plans\*.outline.json 审阅修改
%PY% run.py plan    --outline "out\plans\x.outline.json"
%PY% run.py repair  --spec    "out\plans\x.deck.json" --rounds 3
%PY% run.py qa
%PY% run.py render
```

**不想用模型时**：`%PY% run.py all --content dify` 用内置示例内容直接出片。

依赖：`python-pptx`、`python-docx`、`pypdfium2`、`Pillow`、`officecli`（渲染用）。

---

## 目录

```
├── 迈胜PPT模板.pptx        公司模板（4 页骨架 + 9 个 layout）
├── Dify 介绍与实战.pptx     内容来源样例（25 页）
├── pptx/                    zcode 原始 skill（未改动，仅作参考）
├── docs/                    方案与分析文档
├── src/pptgen/
│   ├── tokens.py            设计令牌 + 绘图原语（单一真相来源）
│   ├── layouts.py           12 套版式渲染器，数据驱动
│   ├── build.py             组装 + 结构自检
│   ├── content_dify.py      内容 spec（25 页源材料 → 17 页）
│   └── qa/
│       ├── geometry.py      几何检查（不经渲染）
│       └── visual.py        渲染 + AI 看图闭环
├── out/samples/             产物 pptx
├── out/reports/             QA 报告（.md / .json）
└── out/visual/              渲染图与评审包
```

---

## 必须遵守的工程约束（都踩过）

### 1. 只用 `Blank` / `Title Only`

`Full Blank` 上有个 `Rectangle 3`（`x=9.19, y=0, 4.15×1.24`）会盖住母版的 MEVION 徽标，
渲染出来是**纯白页**，没有任何公司品牌元素。
`Blank` / `Title Only` 同样没有内容占位符可以自由画，但**保留了红顶栏 + 徽标 + 装饰弧线**。

### 2. 所有 `add_slide` 必须在删页之前完成

python-pptx 按「现有 slide 部件数量」推算新文件名。先删再增会让新页与既有页
**partname 撞名**（zip 内出现两个 `slide4.xml`、两个 rId 指向同一部件），
PowerPoint 报修复或显示错页，而**模板封底被静默覆盖丢失**。`build.selfcheck()` 会拦住它。

### 3. `Inches()` 不要套在 `Pt()` 上

早期版本对四个尺寸统一套 `Inches()`，调用方传的却是 `Pt(0.75)`（已是 EMU），
于是 `Inches(9525)` 得到 **9525 英寸高**的巨型矩形，所有发丝线变成覆盖整页的色块。
**officecli 查不出这个**（它只检查右边界），只有自写边界扫描能抓。
现在 `tokens.hrule/vrule` 内部做 `pt/72` 转换，只接受英寸。

### 4. 内容右边界 12.00"

装饰弧线的图片框位于 `x=10.19~15.13`（伸出画布外），实测墨迹自 `x≈12.00` 起。

### 5. 每个 run 显式设字体

模板主题的 `fontScheme` 里 `ea` 为空，不显式设 `<a:ea>` 中文会走系统默认。

### 6. 不要用 python 脚本改源码

本机 DLP 会把 python 写出的文件加密（`.py` 也中招），而 Write/Edit 工具与 bash `cp`
写的是明文。详见 `docs/方案-文档转PPT生成器.md` 与项目记忆。

---

## QA 覆盖范围

`qa/geometry.py`（不经渲染）：

- 越出画布 / 尺寸荒诞（抓 `Inches(Pt())` 那类单位错误）
- 侵入弧线安全区
- 字号低于 12pt 下限
- 文字溢出估算（**按 run 各自字号**累加宽度，避免混排段误报）
- 含文字形状两两重叠
- 文本框内边距非 0
- 内容带最大连续空白块占比（「生硬」的可执行代理指标）

`build.selfcheck()`：zip 重名条目、`sldIdLst` 目标唯一、图片 rel 断链。

**已知边界**：几何检查只做包围盒层面，查不出「不难看但没设计感」——
那正是 `qa/visual.py` 要补的一层。
