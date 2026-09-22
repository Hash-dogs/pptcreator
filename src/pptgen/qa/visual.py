# -*- coding: utf-8 -*-
"""视觉 QA 闭环：渲染 → 粗看 → 细看 → 结构化裁决。

两级策略（成本/覆盖的折中）：

  Tier 1  整份缩略图联系表，**一次**视觉调用，粗看每一页。
          这一步是抓「生硬」的关键 —— 几何检查查不出「不难看但没设计感」，
          只把几何告警的页送去细看，就永远看不见最想解决的问题。

  Tier 2  把 Tier 1 判为 fail 的页 + 几何检查告警的页，逐页渲染大图细看。

裁决格式沿用 zcode judge 代理的 schema，每页一行 JSON：

    {"page": 3, "verdict": "pass"}
    {"page": 4, "verdict": "fail", "issues": [
        {"category": "Design", "problem": "...", "evidence": "..."}]}

    category ∈ Spec | Content | Visual | Design | Unverified

模型接入：任何 OpenAI 兼容的 chat/completions 端点，用环境变量配置
    PPTGEN_VISION_BASE_URL   例如 https://api.openai.com/v1
    PPTGEN_VISION_API_KEY
    PPTGEN_VISION_MODEL      例如 gpt-4o / glm-4v / qwen-vl-max
未配置时**不报错**，改为输出人工复核包（图片 + 评审提示词），可交给人或子代理看。
"""
from __future__ import annotations
import base64
import json
import os
import shutil
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

REVIEW_PROMPT = """你是幻灯片成品的视觉验收员。逐页判定 pass / fail。

对每一页检查三件事：
1. 视觉资产 —— 图片/图表/表格/图标切题、比例自然无拉伸、图表类型与数值和周围文字一致、干净未裁切。
2. 版式与构图 —— 这一页读起来像完成品。报告：元素重叠、内容被压住或遮住、溢出页面或容器、
   模块挤在一起、明显的失衡（视觉重心偏移、一侧过载另一侧空荡）。
3. 内容一致性 —— 可读，无乱码，无截断；与主题相符。

幻灯片场景请特别留意（按"投影距离"看）：
- 一页是否一眼就能读懂；文字有没有压到卡片或形状边缘上；
- 单个容器是否一半空着（那也是失衡）；
- 图表标签投影后是否小到读不清；
- 跨页一致性：页码、页眉、配色。

只报你能说出具体问题的项，不要给泛泛的美化建议。
每页输出一行 JSON，按页码顺序，pass 的也要输出：

{"page": 1, "verdict": "pass"}
{"page": 2, "verdict": "fail", "issues": [{"category": "Design", "problem": "...", "evidence": "..."}]}

category 取值：Spec | Content | Visual | Design | Unverified
有任一项不达标即 fail；无法确认写 Unverified。
除 JSON 行外不要输出任何其他内容。"""


# ── 定位 officecli ────────────────────────────────────────────
def find_officecli() -> str | None:
    p = shutil.which('officecli')
    if p:
        return p
    cand = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'OfficeCLI', 'officecli.exe')
    return cand if os.path.isfile(cand) else None


def model_config():
    """视觉模型配置。读 .env，未配置时返回 None（走人工复核包）。"""
    try:
        from .. import config
    except ImportError:
        return None
    try:
        return config.vision_config()
    except Exception:
        return None


# ── 渲染 ──────────────────────────────────────────────────────
# officecli 的 stdout 是 UTF-8，而 `text=True` 会用系统 locale（中文 Windows 上是
# GBK）去解码 —— 路径里只要有一个字节不是合法 GBK 序列，subprocess.run 就抛
# UnicodeDecodeError，整个逐页渲染中断（只留下联系表）。显式指定 UTF-8 并容错。
_SUBPROC = dict(capture_output=True, encoding='utf-8', errors='replace')


def render_pages(pptx: str, out_dir: str, pages: list[int] | None = None,
                 native: bool = True, width: int | None = None,
                 height: int | None = None) -> dict[int, str]:
    """用 officecli 逐页渲染 PNG，返回 {页码: 路径}。

    width/height 传了才指定输出分辨率，不传就走 officecli 自己的默认
    （1280×720）。**默认值刻意保持不动**：走这条默认的是 AI 看图复核的
    Tier 2 详情图，那些图会 base64 塞进视觉模型，像素翻 2.25 倍等于凭空
    放大请求体积 —— 那是 QA 回路调好的参数，不该被前端预览顺手改掉。
    只有 Web 逐页预览（server.py 的 `_render`）需要更高分辨率。
    """
    exe = find_officecli()
    if not exe:
        raise RuntimeError('未找到 officecli，无法渲染')
    os.makedirs(out_dir, exist_ok=True)
    size = (['--screenshot-width', str(width), '--screenshot-height', str(height)]
            if width and height else [])
    got: dict[int, str] = {}
    for pg in (pages or []):
        out = os.path.join(out_dir, 'page-%02d.png' % pg)
        # 先拼出不含渲染器选择的公共部分，退回分支复用它 —— 不回带 size 的话
        # native 一失败，分辨率就静默掉回默认，而预览图是 1280 还是 1920
        # 在页面上看不出来（只是糊一点），这种降级很难被发现。
        base = [exe, 'view', pptx, 'screenshot', '--page', str(pg), '-o', out] + size
        r = subprocess.run(base + (['--render', 'native'] if native else []),
                           **_SUBPROC)
        if r.returncode != 0 or not os.path.isfile(out):
            # native 渲染器在部分环境下不稳定，退回默认渲染
            r = subprocess.run(base, **_SUBPROC)
        if os.path.isfile(out):
            got[pg] = out
    return got


def render_contact_sheet(pptx: str, out_path: str, cols: int = 3,
                         native: bool = True) -> str | None:
    """整份 deck 的缩略图联系表（一次调用）。"""
    exe = find_officecli()
    if not exe:
        raise RuntimeError('未找到 officecli，无法渲染')
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    base = [exe, 'view', pptx, 'screenshot', '--grid', str(cols), '-o', out_path]
    subprocess.run(base + (['--render', 'native'] if native else []), **_SUBPROC)
    if not os.path.isfile(out_path):
        subprocess.run(base, **_SUBPROC)
    return out_path if os.path.isfile(out_path) else None


# ── 模型调用 ──────────────────────────────────────────────────
def _b64(path: str) -> str:
    with open(path, 'rb') as f:
        return base64.b64encode(f.read()).decode('ascii')


def call_vision(image_paths: list[str], prompt: str, cfg) -> str:
    """走统一的 llm 客户端（含 Azure / 重试 / 超时）。"""
    from .. import llm
    return llm.ask_vision(prompt, image_paths, cfg)


def parse_verdicts(text: str) -> list[dict]:
    """从模型输出里抽出每页一行的 JSON。"""
    out = []
    for line in text.splitlines():
        line = line.strip().strip('`').strip()
        if not line.startswith('{'):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if 'page' in obj and 'verdict' in obj:
            out.append(obj)
    return out


# ── 主流程 ────────────────────────────────────────────────────
def run(pptx: str, work_dir: str, *, geometry_report: dict | None = None,
        pages: list[int] | None = None, cols: int = 3,
        native: bool = True) -> dict:
    """执行视觉 QA 闭环。返回 {mode, sheet, verdicts, review_packet?}。"""
    os.makedirs(work_dir, exist_ok=True)
    all_pages = pages or list(range(1, 1 + (geometry_report or {}).get('slides', 0)))
    sheet = render_contact_sheet(pptx, os.path.join(work_dir, 'contact-sheet.png'),
                                 cols=cols, native=native)

    cfg = model_config()
    if not cfg:
        # 无模型：输出人工复核包（图 + 提示词），交给人工或子代理
        prompt_path = os.path.join(work_dir, 'review-prompt.md')
        with open(prompt_path, 'w', encoding='utf-8') as f:
            f.write(REVIEW_PROMPT + '\n\n---\n\n## 待评审图片\n')
            f.write('- 缩略图总览：`%s`\n' % sheet)
            f.write('- 单页图目录：`%s`\n' % work_dir)
        return dict(mode='manual', sheet=sheet, verdicts=[],
                    review_packet=dict(sheet=sheet, prompt=prompt_path,
                                       work_dir=work_dir))

    verdicts: list[dict] = []
    # Tier 1 — 整份粗看
    if sheet:
        text = call_vision([sheet], REVIEW_PROMPT, cfg)
        verdicts = parse_verdicts(text)

    # Tier 2 — 细看：Tier1 判 fail 的页 + 几何告警的页
    flagged = {v['page'] for v in verdicts if v.get('verdict') != 'pass'}
    if geometry_report:
        flagged |= {i['slide'] for i in geometry_report.get('issues', [])}
    flagged = sorted(p for p in flagged if p in all_pages)
    detail = render_pages(pptx, os.path.join(work_dir, 'pages'), flagged, native=native)
    if detail:
        text2 = call_vision(list(detail.values()),
                            REVIEW_PROMPT + '\n\n这些是第 %s 页的单页大图，逐页判定。'
                            % ', '.join(str(p) for p in flagged), cfg)
        fine = {v['page']: v for v in parse_verdicts(text2)}
        verdicts = [fine.get(v['page'], v) for v in verdicts] or list(fine.values())

    return dict(mode='model', sheet=sheet, verdicts=verdicts,
                flagged=flagged, detail_pages=detail)


def format_report(res: dict) -> str:
    if res['mode'] == 'manual':
        p = res['review_packet']
        return ('视觉 QA — 未配置视觉模型，已生成人工复核包\n'
                '  联系表: %s\n  评审提示词: %s\n  单页目录: %s\n'
                '  配置 PPTGEN_VISION_BASE_URL / _API_KEY / _MODEL 可自动评审。'
                % (p['sheet'], p['prompt'], p['work_dir']))
    lines = ['视觉 QA — %d 页，模型裁决如下' % len(res['verdicts'])]
    fails = 0
    for v in res['verdicts']:
        if v.get('verdict') == 'pass':
            lines.append('  s%-3s pass' % v['page'])
        else:
            fails += 1
            lines.append('  s%-3s %s' % (v['page'], v.get('verdict')))
            for it in v.get('issues', []):
                lines.append('        [%s] %s  ← %s'
                             % (it.get('category'), it.get('problem'), it.get('evidence')))
    lines.append('通过 %d / 不通过 %d' % (len(res['verdicts']) - fails, fails))
    return '\n'.join(lines)
