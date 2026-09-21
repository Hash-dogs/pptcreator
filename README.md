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

> ⚠️ **回退模式 ≠ 可用产出。** 未配置模型、或模型调用失败时，大纲与规划走确定性回退：
> 能跑完、版式合法、结构自检通过，但**内容取舍、标题主张、版式与语义的匹配**
> 都做不了，产出会明显单调（实测 18 页里 12 页同一种版式），
> 且原文直接塞入常超出框高（几何检查会报 `text_overflow`）。
> **要拿到可用的东西，必须配模型。**
>
> 回退产物**不再是静默的**：大纲 JSON 里带 `_meta.generated_by`，`.md` 预览、
> Web 界面和生成日志三处都会明确标出「这是兜底产物」。以前它只 print 一行
> stdout，界面上和模型产出长得一模一样 —— 结果就是拿着一份兜底大纲当模型产出看。

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

页面是「左侧栏 + 主区」：左侧常驻**状态栏**与**制作进度**（按 `info / active /
warning / success` 上色，长任务时有点在跳），主区从上到下是选源 → 大纲确认 →
生成日志 → 生成结果。

1. **选源** —— 两种方式，用单选切换：
   - **上传文件**：把 `.docx / .pdf / .txt / .pptx / .md` 拖进落区，或点「选择文件」。
     可一次拖多个，但**同时只有一枚胶囊生效**（链路一次解析一份文档），
     点胶囊切换「使用中」，✕ 从磁盘删掉。
   - **选择已有文档**：从项目根目录里挑（`Dify 介绍与实战.pptx`、`迈胜PPT模板.pptx` 等）
2. **审阅并修改大纲** —— 章节名、页标题**直接在页面上改**（这是人工确认的落点），
   也能展开「高级」直接编辑 JSON，两边双向同步
3. **生成进度** —— 实时日志（规划分批进度、几何检查结果都会打出来）
4. **预览与下载** —— 逐页渲染图 + 几何检查徽章 + 下载 pptx

换端口：`server.py --port 8080`。只用标准库，无额外依赖。

### 上传接口

前端走的是 `POST /api/upload?name=<文件名>`，**裸 body 就是文件字节**
（不是 multipart —— 服务端要保住「零依赖」，自己解析 boundary 不划算）：

```bat
curl -X POST --data-binary "@需求说明.docx" "http://127.0.0.1:8000/api/upload?name=需求说明.docx"
curl "http://127.0.0.1:8000/api/sources"                      :: 列出根目录 + 已上传
curl -X DELETE "http://127.0.0.1:8000/api/uploads/需求说明.docx"
```

单文件上限 40MB，扩展名限制在 `SOURCE_EXT` 内，重名自动加 `-1`。
上传的文件落在 `out/uploads/`（已 gitignore），重启后仍会出现在文件胶囊里。

> ⚠️ 若上传报「读出来是 DLP 密文」，说明**读文件的那个进程**不是加密客户端
> 信任的进程（实测 git-bash 的 curl 读 `.txt` 就是密文，chrome 一般不是）。
> 见下方「工程约束 §6」。

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

**跑单测**（纯确定性逻辑，不联网、不调用模型，约 1 秒）：

```bat
%PY% -m unittest discover -s tests -v
```

覆盖：标题判据（`60,000+` / `01` 这类大字号数字噪声不能被当成标题）、
pptx 章节检测（分隔页、只有 kicker 的章、front matter 排除）、docx 层级回退、
「保章压页」的配额分配、规划层从大纲回填页眉、锚点取内容；
以及文件日志的命名/并发去重/任务名消毒/原子写/路径校验，
和 `server.py` 里两处踩过的路径坑（`_stem_of` 的叠加后缀、`_find_source` 的遮蔽）。

---

## 文件日志

每跑一次流程，在 `out/logs/` 下留一个**自包含**的文件夹：

```
out/logs/2026-09-21-11-02-Dify 介绍与实战/
├── run.json                  本次流程的关键值（每个阶段结束增量落盘）
├── run.log                   完整流水（CLI 的 stdout / Web 的任务日志）
├── Dify_介绍与实战-2.pptx      生成的成品
├── Dify_介绍与实战.pptx        源文件 —— **仅当源是上传的才放**
└── Dify_介绍与实战.outline-2.json  人工确认后的大纲
```

- **命名**：`<年-月-日-时-分>-<任务名>`，同分钟重名自动加 `-2`。
  CLI 的任务名取 `--name`，没有就用源文件 stem；Web 取源 stem。
- **源文件只有上传的才归档**：用根目录文件时源本来就在仓库里，没必要再存一份。
- **同名不覆盖**：源文件和成品常常同名（都叫 `<stem>.pptx`），后进的那个加 `-2`。
  到底哪个是哪个，看 `run.json` 里的 `source.archived_as` / `artifacts.pptx.archived_as`。
- **写入是原子的**（临时文件 + `os.replace`）：进程崩在写的中途也不会留下
  截断的、解析不了的 run.json —— 而那恰恰是最需要它可读的时刻。
- **写日志失败不会拖垮流程**：磁盘满、目录被删，都只往 stderr 吼一句就继续。

`run.json` 记四组：

| 组 | 内容 |
|---|---|
| 运行基本信息 | 起止时间、耗时、入口（cli/web）、命令、是否成功、错误 |
| 源文档与结构 | 文件名、来源（upload/root）、原始文件名、字数、块数；骨架方法与章节树 |
| 各阶段质量与降级 | 每个阶段的耗时与成败；大纲是 `llm` 还是 `fallback`、模型名、warnings；规划的版式分布；QA 的 error/warn |
| 配置快照 | 页数区间、内容策略、模板名 + **模板 sha256**、各阶段 token 预算 |

> 为什么值得有：在这个板块之前，**跑一次流程事后几乎无法追溯** ——
> Web 的日志在内存里（重启就没），CLI 只有 stdout，落盘的只有产物。
> 结果是「这份大纲质量很差」摆在那里，却看不出它其实是兜底产物、模型压根没跑。

**关掉**：`PPTGEN_LOG=0`。**换目录**：`PPTGEN_LOG_DIR`。
**不复制成品**（省 8MB/次）：`PPTGEN_LOG_PPTX=none`。

> `out/` 整个被 gitignore，所以日志不进版本控制。

---

## 大纲是怎么生成的

借鉴了 [book-to-skill](https://github.com/virgiliojr94/book-to-skill) 的思路：
**先用确定性代码抽出文档结构，再让模型在既定结构上做内容取舍**（"Structure, not a summary"）。
分三层：

**① 结构抽取（`structure.py`，纯确定性、不调模型）**
从解析出的 blocks 里抽章节树。章节信号按可靠性从高到低尝试，认不出来就往下一级退化：

| 方法 | 判据 |
|---|---|
| `divider` | pptx 的分隔页：某一页最大字号 ≥ 全 deck 标题字号中位数 × 2.2，且是短序号 |
| `kicker` | 正文页左上角的章节标签（`01 初识 DIFY`）。**与分隔页取并集** —— 实测源文档第 5 章只有 kicker 没有分隔页 |
| `numeric` | 显式数字标题（`第3章` / `Chapter 5` / `01 产品介绍`） |
| `depth` | 标题层级（docx 的 Heading 1、md 的 `##`） |
| `flat` | 都不成立 → 单章 |

同时识别封面 / 目录页 / 封底为 front matter 并整体排除（目录页的内容是各章标题
粘在一起的产物，留着会让模型把目录条目当成章节）。
实测 `Dify 介绍与实战.pptx` → 5 章 18 页，与源文档一一对应。

**② 内容生成（`pipeline.py`）**
把骨架摘要 + 正文交给模型。正文短于 24k 字就一次调用；更长则**按章分片**，
每章一次、各自只带该章的正文 —— 这样 prompt 有界，长文档也不会因为截断丢章。
每页除标题外还产出 `hint`（展示形态）、`source`（来源标注）、
`anchor`（内容取自哪一条源页标题）。

**③ 校验与修正**
模型给的页数区间、章节数这些约束只是**建议**，代码这边才是执行：
页数落区间、章节数等于骨架章数、标题非退化（复用「≥4 个实义字符」的判据）、
每页 hint/source 非空。不合格就带着违规清单重试一次；仍不合格则确定性修正
（补章节、补字段、超页数就**保章压页**），并把修了什么记进 `_meta.warnings`。
**不会因为模型不听话就整份退回兜底** —— 兜底产出更差。

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
│   ├── structure.py         文档骨架（确定性抽章节，不调模型）
│   ├── runlog.py            文件日志（一次流程 = 一个文件夹）
│   ├── content_dify.py      内容 spec（25 页源材料 → 17 页）
│   └── qa/
│       ├── geometry.py      几何检查（不经渲染）
│       └── visual.py        渲染 + AI 看图闭环
├── tests/                   单测（stdlib unittest，不联网）
├── out/samples/             产物 pptx
├── out/plans/               中间 JSON（*.parsed / *.outline / *.deck）
├── out/reports/             QA 报告（.md / .json）
├── out/visual/              渲染图与评审包
├── out/logs/                每次流程的日志文件夹（见「文件日志」）
└── out/uploads/             页面上传的源文档（gitignore）
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

### 6. 有些文件在磁盘上是密文

本机装了透明加密客户端，部分文件带 `%TSD-Header-###%` 文件头。
**非信任进程**（Read/grep/od/PowerShell/officecli/git-bash）打开这类文件拿到的是密文
（乱码或 "File contains corrupted data"），`cmd.exe` 拿到的是明文。
officecli 属前者 —— 所以它读不了加密的 pptx，排查时别当成文件损坏。

抢救办法（重定向必须交给 bash，不能写进 cmd 里）：

```bash
cmd //c "type src\pptgen\qa\geometry.py" > /tmp/recover.py
cp /tmp/recover.py src/pptgen/qa/geometry.py
```

详见 `docs/pptx-skill-分析.md`。

### 7. 别用 python 写源文件

同上一节的透明加密：**`python.exe` 写出来的文件会被加密**，随后 Read / grep 全变密文。
改代码用 Edit/Write 工具（它们写的文件保持可读）。
已经在 python 里跑的批量改写，写完立刻 `Read` 一下确认没变成 `%TSD-Header-###%`。

> 注意 python **读**明文文件是正常的（`import` 照样成功），只有**写**会触发 ——
> 所以「脚本跑完没报错」不能说明文件是好的。

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
