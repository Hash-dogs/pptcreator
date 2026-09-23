# -*- coding: utf-8 -*-
"""配置加载：读项目根目录的 `.env`，不额外依赖 python-dotenv。

设计原则：
  - **缺配置不报错**。文本模型没配 → 大纲/规划退回确定性模式；
    视觉模型没配 → 看图环节输出人工复核包。整条链路永远能跑完。
  - 视觉配置留空时可回退用文本模型的 base/key（同一家服务商常共用 key）。
"""
from __future__ import annotations
import contextlib
import os
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENV_PATH = os.path.join(ROOT, '.env')


def load_env(path: str = ENV_PATH, override: bool = False) -> dict:
    """极简 .env 解析：KEY=VALUE，# 开头为注释，值两端的引号会被剥掉。"""
    out = {}
    if not os.path.isfile(path):
        return out
    with open(path, encoding='utf-8-sig') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if v == '':
                continue
            out[k] = v
            if override or k not in os.environ:
                os.environ[k] = v
    return out


def get(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key) or default


def get_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, '') or default)
    except ValueError:
        return default


def get_bool(key: str, default: bool = False) -> bool:
    v = (os.environ.get(key) or '').strip().lower()
    if v in ('1', 'true', 'yes', 'on'):
        return True
    if v in ('0', 'false', 'no', 'off'):
        return False
    return default


class LLMConfig:
    """一个 OpenAI 兼容端点的连接信息。"""

    def __init__(self, base: str, key: str, model: str,
                 api_version: str | None = None):
        self.base = base.rstrip('/')
        self.key = key
        self.model = model
        self.api_version = api_version

    @property
    def is_azure(self) -> bool:
        return bool(self.api_version) or '.openai.azure.com' in self.base

    @property
    def url(self) -> str:
        if self.is_azure:
            if '/chat/completions' in self.base:
                base = self.base
            else:
                base = self.base + '/chat/completions'
            sep = '&' if '?' in base else '?'
            return '%s%sapi-version=%s' % (base, sep, self.api_version or '2024-08-01-preview')
        return self.base + '/chat/completions'

    @property
    def headers(self) -> dict:
        if self.is_azure:
            return {'Content-Type': 'application/json', 'api-key': self.key}
        return {'Content-Type': 'application/json',
                'Authorization': 'Bearer ' + self.key}

    def __repr__(self):
        return '<LLMConfig %s @ %s>' % (self.model, self.base)


def llm_config() -> LLMConfig | None:
    base, key, model = (get('PPTGEN_LLM_BASE_URL'), get('PPTGEN_LLM_API_KEY'),
                        get('PPTGEN_LLM_MODEL'))
    if base and key and model:
        return LLMConfig(base, key, model, get('PPTGEN_LLM_API_VERSION'))
    return None


# 已知看不了图的模型名特征。
#
# ⚠️ **这张名单只用来提醒，不用来否决配置。** 原先它是一道硬拦截：模型名命中就
# 直接返回 None，等于用一个「写代码时的假设」去否决用户**显式写下**的配置。
# 实测踩过：`.env` 里 `PPTGEN_VISION_MODEL` 配得好好的，`run.py config` 却报
# 「视觉模型：未配置」，看起来像用户没配 —— 而名单里那个名字（`deepseek-flash`）
# 在用户的部署里就是能看图的。
#
# 保留它是为了**防静默**：把图发给看不见图的模型，它往往照样返回一段像样的文字，
# 看起来像「看图通过」。所以命中名单时给一句可听见的提醒（`vision_warning()`），
# 由 `run.py config`、网页的配置状态、版式管理页三处显示出来。
_NO_VISION = ('deepseek-chat', 'deepseek-flash', 'deepseek-reasoner',
              'deepseek-coder', 'gpt-3.5', 'text-davinci', 'o1-mini')


def vision_capable(model: str | None) -> bool:
    """模型名在「已知看不见图」名单里 → False。**只用于提醒**，不用于否决。"""
    if not model:
        return False
    m = model.lower()
    return not any(m.startswith(p) for p in _NO_VISION)


def vision_warning(model: str | None = None) -> str:
    """视觉模型名可疑时返回一句人话提醒，否则空串。"""
    m = model or get('PPTGEN_VISION_MODEL') or ''
    if m and not vision_capable(m):
        return ('「%s」在已知的看不见图的模型名单里。如果识别或看图的结果是空的、'
                '或明显跑题，多半是它读不到图 —— 换一个支持图片输入的模型'
                '（填法见 .env.example 第 2 节）。' % m)
    return ''


def vision_config() -> LLMConfig | None:
    base = get('PPTGEN_VISION_BASE_URL')
    key = get('PPTGEN_VISION_API_KEY')
    model = get('PPTGEN_VISION_MODEL')
    # 用户**显式写了**视觉模型名，那就是他的断言 —— 不再拿名单否决它。
    if model and base and key:
        return LLMConfig(base, key, model, get('PPTGEN_VISION_API_VERSION'))
    # 回退：给了视觉模型名、但没给 base/key 时复用文本模型的连接信息
    #（同家服务商常共用 key）。
    if model and get_bool('PPTGEN_VISION_FALLBACK_TO_LLM', True):
        fb = llm_config()
        if fb is not None:
            return LLMConfig(fb.base, fb.key, model, fb.api_version)
    # 连模型名都没有 → 没有视觉能力可用。**这才是名单最初要拦的那个 bug**：
    # 早先这里直接返回文本模型配置，于是把看不见图的模型当视觉模型调用了。
    return None


def page_range() -> tuple[int, int]:
    lo = get_int('PPTGEN_MIN_PAGES', 13)
    hi = get_int('PPTGEN_MAX_PAGES', 18)
    return (lo, hi) if lo <= hi else (hi, lo)


def preview_size() -> tuple[int, int]:
    """Web 逐页预览图的渲染分辨率（宽, 高）。

    默认 1920×1080 就是 officecli native 渲染的上限 —— 传更大的
    `--screenshot-width` 仍然只出 1920×1080，所以没有调高的余地。
    调低只省磁盘：渲染耗时取决于 PowerPoint 起进程，与分辨率无关
    （实测 1280 与 1920 都是每页约 10 秒）。

    只给 Web 预览用。见 `visual.render_pages()` 的 width/height 参数 ——
    AI 看图复核那一路（Tier 2 详情图）刻意保持默认，那些图会 base64
    塞进视觉模型，像素翻 2.25 倍等于凭空放大请求体积。
    """
    return (get_int('PPTGEN_PREVIEW_WIDTH', 1920),
            get_int('PPTGEN_PREVIEW_HEIGHT', 1080))


# ── 分阶段输出上限 ────────────────────────────────────────────
# 大纲与规划都是「一次要吐好几千 token 的 JSON」的调用，而 .env 里那个
# PPTGEN_MAX_TOKENS 是给通用调用调的（默认 8000）。推理模型会把相当一部分额度
# 花在 reasoning_content 上，两相叠加就会把正文挤空 —— 而**正文为空会被兜底吞掉**，
# 表现成「大纲质量就是这么差」，非常难查。
#
# 所以这两处不再直接读 PPTGEN_MAX_TOKENS，而是派生时抬到保底：
# 没显式配 <阶段>_MAX_TOKENS 就取 max(PPTGEN_MAX_TOKENS, 保底)；
# 显式配了就以显式值为准（用户要压额度时有路可走）。
# 提高上限几乎不额外花钱 —— 只为实际生成的 token 付费。
def _stage_max_tokens(key: str, floor: int) -> int:
    explicit = get_int(key, 0)
    if explicit > 0:
        return explicit
    return max(get_int('PPTGEN_MAX_TOKENS', 8000), floor)


# 保底值取 16000：实测 deepseek-flash 的推理会**撑满给定预算** —— 给 4000 就
# 烧掉 4001、给 8000 就烧掉 8000，正文一律为空。所以额度必须一次给够，
# 否则两个阶段都会静默退回确定性兜底。
_STAGE_TOKEN_FLOOR = 16000


def outline_max_tokens() -> int:
    """大纲阶段的输出上限（要吐 3–5 章十几页的 JSON）。"""
    return _stage_max_tokens('PPTGEN_OUTLINE_MAX_TOKENS', _STAGE_TOKEN_FLOOR)


def plan_max_tokens() -> int:
    """单批规划的输出上限（PLAN_BATCH 页的完整版式字段）。"""
    return _stage_max_tokens('PPTGEN_PLAN_MAX_TOKENS', _STAGE_TOKEN_FLOOR)


def section_dividers() -> bool:
    """是否在正文里插入章节分隔页（每章开头一页）。

    分隔页是**结构页**：它让「换章了」这件事在翻页时可见 —— 在此之前 5 个章节
    只靠左上角那行 kicker 区分。但它要占页数预算，所以 `pipeline._divider_budget()`
    只在「扣掉之后每章还留得下一页正文」时才插；装不下就完全不插。

    关掉它：`.env` 里设 `PPTGEN_SECTION_DIVIDERS=0`。
    """
    return get_bool('PPTGEN_SECTION_DIVIDERS', True)


def outline_segment() -> bool:
    """骨架分不出章（或分得太碎）时，让模型把页单元归成几章。

    它是**兜底**而不是主路径：能确定性抽出来的结构（书签 / 分隔页 / 标题层级）
    绝不走它。只在两种情况下触发，见 `pipeline._chapter_count_ok()`。

    关掉它：`.env` 里设 `PPTGEN_OUTLINE_SEGMENT=0` —— 那就退回「整份文档一章」
    的老行为，适合想完全离线跑、一次模型调用都不多花的场合。
    """
    return get_bool('PPTGEN_OUTLINE_SEGMENT', True)


def repair_rounds() -> int:
    """修复回环的最大轮数（`PPTGEN_REPAIR_ROUNDS`，默认 3）。

    「最大」而不是「固定跑几轮」：某一轮几何检查已经干净就提前停（见
    `repair.repair_deck` 的 `for ... else`），所以这个值只决定**最坏情况**下多花
    几次模型调用。给 0 就是不修。

    它是**部署口径**，不再是网页上的输入项：原来那个「修复轮数」输入框默认 3，
    但没人知道 3 是怎么来的，改与不改都看不出区别 —— 放到 `.env` 里一次定好，
    `run.py --rounds` 仍可对单次命令覆盖。
    """
    return max(0, get_int('PPTGEN_REPAIR_ROUNDS', 3))


CONTENT_MODES = ('strict', 'balance', 'enrich')

# 「内容策略」允许在**一次流程内**被临时覆盖 —— Web 前端让人现选一次
# （见 `web/index.html` 的内容策略下拉），不必改 `.env` 再重启服务。
# 覆盖挂在**线程局部**上而不是写 `os.environ`：每个任务跑在自己的线程里，
# 覆盖只作用于这一次流程，并发两个任务互不干扰，CLI 那一侧也永远只读 `.env`。
_content_local = threading.local()


def pick_content_mode(m: str | None) -> str | None:
    """合法就归一化成小写，否则 None —— 非法值一律静默退回默认口径。"""
    v = (m or '').strip().lower()
    return v if v in CONTENT_MODES else None


def content_mode() -> str:
    """内容取舍口径，优先取本次流程的覆盖，其次 `.env` 的 `PPTGEN_CONTENT_MODE`。

    `strict` 只做结构整理 / `balance` 允许合并提炼 / `enrich` 可以补写过渡。
    它注入**分章提示词**与**一次性大纲提示词**两处 —— 按章分片那条路径不读它，
    长文档永远按平衡口径生成（见 `docs/程序运行逻辑.md`）。
    """
    return (pick_content_mode(getattr(_content_local, 'value', None))
            or pick_content_mode(get('PPTGEN_CONTENT_MODE'))
            or 'balance')


@contextlib.contextmanager
def content_mode_override(mode: str | None):
    """在 `with` 块内把内容策略定成 `mode`（None / 非法值 = 不覆盖，照旧读 `.env`）。"""
    prev = getattr(_content_local, 'value', None)
    _content_local.value = mode
    try:
        yield content_mode()
    finally:
        _content_local.value = prev


def out_dir() -> str:
    d = get('PPTGEN_OUT', 'out') or 'out'
    return d if os.path.isabs(d) else os.path.join(ROOT, d)


def out_sub(name: str) -> str:
    """输出目录下的一个子目录（绝对路径）。

    `run.py` 与 `server.py` 各自硬编码过一份 `out/<sub>` 常量，两边重复且与
    `out_dir()` 无关 —— 于是 `PPTGEN_OUT` 成了个**只有打印时会读的死配置**。
    产物路径统一走这里，改 `PPTGEN_OUT` 才真的会把东西挪走。
    """
    return os.path.join(out_dir(), name)


# ── 文件日志 ──────────────────────────────────────────────────
def log_enabled() -> bool:
    return get_bool('PPTGEN_LOG', True)


def log_root() -> str:
    """每次流程的日志文件夹的根目录。"""
    d = get('PPTGEN_LOG_DIR', '') or ''
    if not d:
        return out_sub('logs')
    return d if os.path.isabs(d) else os.path.join(ROOT, d)


def log_max_copy_mb() -> int:
    """源文件/成品超过这个大小就只记路径与哈希，不复制进日志文件夹。

    Web 上传有 40MB 上限，但 CLI 没有 —— 不兜底的话
    `run.py full --src <500MB 的 pdf>` 会把它整个复制进日志目录。
    """
    return get_int('PPTGEN_LOG_MAX_COPY_MB', 64)


def log_copy_pptx() -> bool:
    return (get('PPTGEN_LOG_PPTX', 'copy') or 'copy').lower() != 'none'


def template_path() -> str:
    t = get('PPTGEN_TEMPLATE', '迈胜PPT模板.pptx') or '迈胜PPT模板.pptx'
    return t if os.path.isabs(t) else os.path.join(ROOT, t)


def layouts_dir() -> str:
    """自定义版式的目录（默认仓库根下的 `layouts_custom/`，**随仓库提交**）。

    与 `out/` 下的产物不同，自定义版式是**资产**不是产物：识别一次要花一次模型
    调用，同事之间也该共用同一套。所以默认落在仓库里、进版本控制。

    `PPTGEN_LAYOUTS_DIR` 可以指到别处（绝对路径，或相对仓库根）——
    单测靠它把注册表隔离到临时目录，不然跑一遍测试就会污染真实的版式库。
    """
    d = get('PPTGEN_LAYOUTS_DIR', 'layouts_custom') or 'layouts_custom'
    return d if os.path.isabs(d) else os.path.join(ROOT, d)


def summary() -> str:
    llm, vis = llm_config(), vision_config()
    lo, hi = page_range()
    pw, ph = preview_size()
    warn = vision_warning()
    lines = [
        '配置状态',
        '  文本模型 : %s' % (llm or '未配置（大纲/规划将走确定性模式）'),
        '  视觉模型 : %s' % (vis or '未配置（看图输出人工复核包）'),
    ]
    if warn:
        # 名单命中的提醒放在模型名下面一行 —— 它只提醒，不否决（见 _NO_VISION 注释）
        lines.append('             ⚠ %s' % warn)
    lines += [
        '  页数区间 : %d–%d（正文；章节分隔页另计）' % (lo, hi),
        '  预览分辨率: %d×%d（仅 Web 逐页预览）' % (pw, ph),
        '  章节分隔 : %s' % ('插入' if section_dividers() else '不插（PPTGEN_SECTION_DIVIDERS=0）'),
        '  内容策略 : %s' % content_mode(),
        # 界面上没有这一项了，`run.py config` 是唯一能看见生效值的地方
        '  修复轮数 : %d（几何修复回环的上限；PPTGEN_REPAIR_ROUNDS）' % repair_rounds(),
        '  模板     : %s' % template_path(),
        '  版式目录 : %s（自定义版式，进版本控制）' % layouts_dir(),
        '  输出目录 : %s' % out_dir(),
        '  文件日志 : %s' % (log_root() if log_enabled() else '已关闭（PPTGEN_LOG=0）'),
    ]
    return '\n'.join(lines)
