# -*- coding: utf-8 -*-
"""截图 → 版式声明：视觉识别 → 校验 → 渲试片 → 构图比对 → 草稿。

    python run.py layouts --add-image shot.png --name my_layout
    # 或者从网页的「版式管理」页上传截图

## 为什么是一串闸门，而不是「识别一次就入库」

用户**不能对话修正**识别结果（只能「采用」或「放弃」），所以质量闸门必须在入库前
全部自动跑完。每一步都是确定性的、可判定的：

    ① 视觉模型看着截图输出一份**声明**（区块 + 字段 + 意图 + 示例文案）
    ② `layout_dsl.normalize_blocks` 把「图片比例」换算成英寸并吸附裁剪
    ③ `layout_dsl.problems` 校验声明本身（名字/意图/降级链/样例齐不齐）
    ④ 渲一页试片（用 sample 当内容）→ 几何检查 + **截断检查**
    ⑤ 把「原图 + 试片」一起发给视觉模型，问构图是否一致
    ⑥ 任一步不过 → 把原因拼进提示词重试（默认 2 轮）→ 仍不过就**不入库**

④ 里的截断检查不能省：`_fit_lines` 的截断是**静默**的 —— 它让文字不再溢出，
几何检查反而全绿，问题只在肉眼看渲染图时才暴露。这是本仓库反复踩过的坑
（已删除的 `node_flow` 8 个节点里 6 个被截成残句，而几何报告是干净的）。

## 重试的口径与全项目一致

「带违例清单重试 → 仍不合规则降级，并把原因原样带回，**绝不静默**」
（同 `pipeline._outline_by_llm`、`revise.redo_slide`）。这里没有「确定性降级」
可言 —— 识别不出就是识别不出，所以最后一轮不合格时返回 `ok=False` + 原因，
由界面显示「这版做不了，因为……」，而不是塞一个半成品进版式库。
"""
from __future__ import annotations

import os
import re

from . import build, config, layout_dsl, layout_spec, llm, tokens
from .qa import geometry, visual

# 试片渲染的页码：build 里封面/目录各占一页，正文从第 3 页起（`sl['page'] = i + 3`）
FIRST_CONTENT_PAGE = 3
# 试片里的页眉页脚（版式不负责这两块，但试片要像真的一页）
TRIAL_KICKER = '版式试片'
TRIAL_TITLE = '版式试片'


def recognize_prompt(feedback: str = '') -> str:
    """识别提示词。可枚举的部分（区块种类、意图清单）**从代码生成**，
    免得提示词里的清单与渲染器/注册表分叉。"""
    kinds = layout_dsl.kind_catalog_text()
    intents = '\n'.join('  %-12s %s' % (k, layout_spec.INTENT_LABELS[k])
                        for k in layout_spec.INTENTS)
    roles = '、'.join(layout_spec.ROLES)
    p = """你在把一张 PPT 版式的**截图**还原成一个可复用的版式声明。

**只描述正文区。** 页眉（小字章节标签 + 大标题）、页码、来源行、公司 logo 都由
程序统一画，不要识别它们、也不要为它们建区块。

坐标系（一律用比例，不用像素）：
  - 正文区的宽 = 1.0：区块的 x 与 w 相对这个宽度（x=0 贴左，x+w=1 贴右）
  - 正文区的高 = 1.0：区块的 y 与 h 相对这个高度（y=0 贴顶，y+h=1 贴底）
  - 区块之间**不要重叠**；上下左右的相对关系保持截图里的样子

可用区块种类（kind）：
%s

输出**一个 JSON 对象**，不要任何解释文字：

{
  "name": "英文小写下划线，3–32 字符，概括这个构图（如 left_hero_stack）",
  "signature": "一句话说出这版式的视觉特征（后面选版式的模型会读它）",
  "best_for": "适合承载什么内容",
  "avoid_for": "不适合什么内容",
  "roles": ["%s 之一"],
  "intents": ["从下面挑，可多选"],
  "min_items": 2, "max_items": 4,
  "item_chars": 22, "total_chars": 220,
  "blocks": [
    {"kind": "text", "field": "lead", "x": 0, "y": 0, "w": 0.38, "h": 0.55,
     "size": 30, "bold": true},
    {"kind": "bullets", "field": "items", "x": 0.46, "y": 0, "w": 0.54, "h": 1,
     "ncol": 1}
  ],
  "sample": {"lead": "一句话主张", "items": [{"name": "要点", "desc": "说明"}]}
}

intents 只能取下面这些（括号里是含义）：
%s

可选的区块参数（都放在区块对象里）：
  ncol      bullets/columns/kpi 分几栏或几列（默认 1 / 3 / 3）
  size      text 的最大字号（默认 28，程序会按框自动缩小）
  color     颜色令牌名：RED（强调）/ DARK（正文）/ MUTED（次要）/ GREY / TINT
  numbered  bullets 是否显示 01/02 序号（默认 true）
  row_rule  分条时是否画行分隔线（默认 false；kpi 默认 true）

要求（每一条都会在入库前被程序检查）：
  1. `blocks` 3–6 个；`sample` 必须给全 blocks 里用到的每个 field。
  2. sample 的文案要**短**：中文按「1 个字 ≈ 字号 ÷ 72 英寸」估宽度，
     别让一段话超出你给它的框 —— 程序会把放不下的字**截断**，那版就不合格。
  3. `min_items`/`max_items` 是你这套构图能舒服容纳的条目数范围。
  4. name 不要与已有版式重名。
  5. **页面里嵌的截图 / 图片 / 图表截图一律不要建区块。** 版式描述的是**版面结构**
     （几栏、每栏放什么文字内容），图区里的东西是**内容**不是版式。那一带留空即可 ——
     试片里那块是空的，不会被当成错误。
  6. 序号/要点前面的**底色块、圆角方块、徽标**这类装饰不要单独建区块 —— 用
     `bullets` 的序号表达就够了（颜色与底色是版式的样式，不是结构）。
""" % (kinds, roles, intents)
    if feedback:
        p += ('\n⚠️ 上一轮的结果有以下问题，这次**必须**修正：\n' + feedback + '\n')
    return p


VERIFY_PROMPT = """下面两张图：第一张是用户给的**版式截图**，第二张是用识别结果
渲染出来的**同版式试片**（内容文字不同，那是示例文案）。

判断试片是否复现了截图的**版面分区** —— 只看三件事：
  1. 分成几栏 / 几个区块；
  2. 各区块之间的**位置关系**（谁在左、谁在上、谁占满整宽）；
  3. 各区块的**相对大小**（主区是不是还是那么大、有没有多出或少掉一整块）。

**这些一律不算不一致**（试片本来就做不到，或属于内容层面）：
  - 页面里嵌入的**截图 / 图片 / 图表**及其所在区域 —— 版式不承载图片内容，
    试片里那一带是空的，属正常；
  - 序号或要点前的**底色块、圆角方块、徽标、图标**；
  - 字体、字号微差，配色深浅，是否通栏色带；
  - 文字内容本身（示例文案与截图上的字不同是必然的）；
  - 截图外框、水印、公司 logo、装饰弧线。

只输出一行 JSON，不要解释：
{"consistent": true 或 false,
 "kind": "structure" 或 "detail" 或 "none",
 "reason": "不超过 40 字"}

`kind` 的含义：`structure` = 分栏数 / 区块位置 / 相对大小对不上（版面分区没复现）；
`detail` = 分区是对的，只差上面那份「不算不一致」清单里的东西；`none` = 没看出差异。
`consistent` 与 `kind` 要一致：只有 `kind` 是 `structure` 时才该是 `false`。"""


# ══════════════════════════════════════════════════════════════
# 声明的归一与校验
# ══════════════════════════════════════════════════════════════

_BAD_CHARS = re.compile(r'[^a-z0-9_]+')


def slugify(raw: str) -> str:
    """把模型起的名字收敛成合法版式名（小写字母/数字/下划线，3–32 字符）。"""
    s = _BAD_CHARS.sub('_', str(raw or '').strip().lower()).strip('_')
    s = re.sub(r'_{2,}', '_', s)
    if not s or not s[0].isalpha():
        s = 'layout_' + s if s else 'layout_new'
    if len(s) < 3:
        # 短于 3 的名字过不了 `_NAME_RE`（那道校验是给**人**改的，别在这里放过）
        s = 'layout_' + s
    return s[:32]


def unique_name(base: str, existing: set[str]) -> str:
    """重名加序号 —— 名字冲突是**确定性**的，不值得让模型重试一轮。"""
    if base not in existing:
        return base
    i = 2
    while ('%s_%d' % (base[:28], i)) in existing:
        i += 1
    return '%s_%d' % (base[:28], i)


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def coerce_meta(raw: dict, existing: set[str] | None = None,
                dropped: list[str] | None = None) -> dict:
    """模型输出 → 一份能交给 `layout_dsl.problems` / `layout_store.save_meta` 的声明。

    这里只做**确定性**的收敛（名字合法化、枚举过滤、数字兜底、降级链去悬空），
    不做「猜模型的意思」—— 该由模型给的（意图、区块、样例文案）缺了就留给
    `problems()` 报出来，让它下一轮补。

    `dropped` 要传进来：区块归一化是**有损**的（太小的框会被丢掉），
    而那些原因要进重试的提示词。⚠️ 归一只做**一次** —— 它按「0–1 比例」解释
    坐标，拿已经换算成英寸的结果再喂一遍会得到完全错位的版式。
    """
    existing = existing or set()
    raw = raw if isinstance(raw, dict) else {}
    meta: dict = {}
    meta['name'] = unique_name(slugify(raw.get('name')), existing)
    meta['roles'] = [r for r in (raw.get('roles') or ['content'])
                     if r in layout_spec.ROLES] or ['content']
    meta['intents'] = [i for i in (raw.get('intents') or [])
                       if i in layout_spec.INTENTS]
    for k in ('signature', 'best_for', 'avoid_for'):
        meta[k] = str(raw.get(k) or '').strip()
    # 降级链：指向不存在的版式会让 `test_fallbacks_exist` 直接红，去掉它（记在 notes）。
    notes = []
    fbs = [f for f in (raw.get('fallback') or []) if f in layout_spec.REGISTRY]
    if len(fbs) != len(raw.get('fallback') or []):
        notes.append('降级链里有不存在的版式，已去掉')
    meta['fallback'] = fbs or ['statement']
    meta['reuse_friendly'] = bool(raw.get('reuse_friendly', True))

    meta['blocks'] = layout_dsl.normalize_blocks(raw.get('blocks'), dropped)
    meta['sample'] = raw.get('sample') if isinstance(raw.get('sample'), dict) else {}

    cap = layout_dsl.item_capacity(meta)
    lo = _int(raw.get('min_items'), 0)
    hi = _int(raw.get('max_items'), 0)
    meta['min_items'] = lo if lo > 0 else (2 if cap >= 2 else 1)
    meta['max_items'] = hi if hi > 0 else (cap or meta['min_items'])
    meta['item_chars'] = _int(raw.get('item_chars'), 22) or 22
    meta['total_chars'] = _int(raw.get('total_chars'), 240) or 240
    meta['_notes'] = notes
    return meta


def problems_of(meta: dict, *, existing: set[str] | None = None,
                dropped: list[str] | None = None) -> list[str]:
    """声明本身的问题（中文原因列表）。空 = 可以渲试片了。"""
    out = list(layout_dsl.problems(meta, existing=existing))
    if not meta.get('blocks'):
        out.append('一个区块都没认出来 —— 这张截图的构图本版做不了')
    for d in (dropped or []):
        out.append(d)
    return out


# ══════════════════════════════════════════════════════════════
# 试片：渲一页，过几何 + 截断
# ══════════════════════════════════════════════════════════════

def render_trial(meta: dict, work_dir: str, *, template: str | None = None,
                 on_log=None) -> dict:
    """用 sample 渲一页试片，返回 `{pptx, png, geometry, truncations}`。

    **不注册版式**：把区块塞进 slide spec（`render_blocks` 优先用 spec 里的 blocks），
    于是「还没入库的版式」也能渲 —— 注册了就等于把半成品放进候选池，
    一次并发的生成可能正好选中它。
    """
    def log(m):
        if on_log:
            on_log(m)

    template = template or config.template_path()
    if not os.path.isfile(template):
        raise RuntimeError('模板不存在：%s' % template)
    os.makedirs(work_dir, exist_ok=True)

    sample = dict(meta.get('sample') or {})
    sample['layout'] = meta['name']
    sample['blocks'] = meta['blocks']          # ← 试片：区块走 spec，不注册
    sample.setdefault('kicker', TRIAL_KICKER)
    sample.setdefault('title', TRIAL_TITLE)
    pptx = os.path.join(work_dir, 'trial.pptx')

    tokens.take_truncations()                  # 清掉上一轮的残留（模块级全局）
    build.build(dict(slides=[sample], toc=[]), template, pptx, on_log=log)
    truncations = tokens.take_truncations()
    # 用几何检查的**默认**跳过规则：1 页正文的 deck 一共 4 页（封面/目录/正文/封底），
    # 默认会跳过封面、目录、封底，只查正文那一页 —— 正好是试片。
    report = geometry.analyse(pptx)
    png = {}
    try:
        png = visual.render_pages(pptx, work_dir, pages=[FIRST_CONTENT_PAGE])
    except RuntimeError as e:                  # officecli 缺失：几何仍可用，别整个失败
        log('[recognize] 渲染试片图失败（%s），跳过构图比对' % e)
    return dict(pptx=pptx, png=png.get(FIRST_CONTENT_PAGE), geometry=report,
                truncations=truncations)


# 渲染后**硬性**不合格的几何项（与 revise.HARD_ISSUES 同一口径）
HARD_KINDS = ('text_overflow', 'text_overlap')


def gate(trial: dict) -> list[str]:
    """试片的硬性不合格项（中文原因）。空 = 可以进入构图比对了。"""
    out = []
    rep = trial['geometry']
    for i in rep['issues']:
        if i['severity'] == 'error':
            out.append('几何错误：第 %s 页 %s（%s）' % (i['slide'], i['kind'], i['detail']))
        elif i['kind'] in HARD_KINDS:
            out.append('文字装不下：%s（%s）' % (i['kind'], i['detail']))
    for src, cut in trial['truncations'][:4]:
        out.append('文案被截断：「%s…」→「%s」—— 框给小了或示例文案太长'
                   % (src[:20], cut[:20]))
    return out


# ══════════════════════════════════════════════════════════════
# 构图比对
# ══════════════════════════════════════════════════════════════

def verify_consistency(original_png: str, trial_png: str, cfg=None) -> dict:
    """原图 vs 试片：构图是否一致。→ `{consistent, kind, reason}`。

    这一步是**用户不能对话修正**的替代：识别得像不像，只能让模型自己再看一眼。
    没配视觉模型/拿不到试片图时返回 `consistent=None`（= 不判定，不阻断）。

    `kind` 区分**结构性**与**装饰性**差异（`structure` / `detail` / `none`）：
    只有结构性的才拦人。装饰性的差异是必然存在的 —— 试片用声明式渲染器画，
    序号没有底色块、图区是空的，这些不是「版式没认出来」。
    """
    cfg = cfg or config.vision_config()
    if not cfg or not original_png or not trial_png:
        return dict(consistent=None, kind='', reason='没有可用的视觉模型或试片图，跳过构图比对')
    try:
        got = llm.ask_vision_json(VERIFY_PROMPT, [original_png, trial_png], cfg)
    except llm.LLMError as e:
        return dict(consistent=None, kind='', reason='构图比对失败：%s' % str(e)[:80])
    if not isinstance(got, dict) or 'consistent' not in got:
        return dict(consistent=None, kind='', reason='构图比对返回不认识的结构')
    kind = str(got.get('kind') or '').strip().lower()
    if kind not in ('structure', 'detail', 'none'):
        kind = ''                      # 模型没按契约给 kind：按老口径（以 consistent 为准）
    return dict(consistent=bool(got.get('consistent')), kind=kind,
                reason=str(got.get('reason') or '')[:120])


# ══════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════

def recognize(image_png: str, work_dir: str, *, cfg=None, template: str | None = None,
              rounds: int = 2, on_log=None) -> dict:
    """识别一张版式截图 → 草稿（**已渲过试片、验过构图**，可直接采用或放弃）。

    返回：
        {ok, meta, trial, verify, attempts: [{round, problems}], reason}
        ok=False 时 `reason` 是给人看的一句话，`attempts` 里是每轮的具体问题。
    """
    def log(m):
        if on_log:
            on_log(m)

    cfg = cfg or config.vision_config()
    if cfg is None:
        return dict(ok=False, meta=None, attempts=[],
                    reason='未配置视觉模型（PPTGEN_VISION_*），无法识别版式截图')
    existing = set(layout_spec.names())
    work_dir = os.path.join(work_dir, 'trial')
    feedback, attempts = '', []
    for rd in range(1, max(int(rounds), 1) + 1):
        log('[recognize] 第 %d 轮：调用视觉模型识别…' % rd)
        try:
            raw = llm.ask_vision_json(recognize_prompt(feedback), [image_png], cfg)
        except llm.LLMError as e:
            attempts.append(dict(round=rd, problems=['模型调用失败：%s' % str(e)[:120]]))
            feedback = '上一轮模型调用失败，请重新输出一份完整的 JSON。'
            continue

        dropped: list[str] = []
        # 归一化只在 coerce_meta 里做一次（它按 0–1 比例解释坐标）
        meta = coerce_meta(raw, existing, dropped)
        bad = problems_of(meta, existing=existing, dropped=dropped)
        if bad:
            log('[recognize]   声明不合规：%s' % '；'.join(bad))
            attempts.append(dict(round=rd, problems=bad))
            feedback = '\n'.join('- ' + b for b in bad)
            continue

        try:
            trial = render_trial(meta, work_dir, template=template, on_log=on_log)
        except Exception as e:                 # noqa: BLE001 —— 渲染失败也要能重试
            log('[recognize]   试片渲染失败：%s' % e)
            attempts.append(dict(round=rd, problems=['试片渲染失败：%s' % e]))
            feedback = '上一轮渲染直接失败了：%s。把区块的框改大一些、文案改短一些。' % e
            continue

        bad = gate(trial)
        verify = verify_consistency(image_png, trial.get('png'), cfg)
        if verify.get('consistent') is False and verify.get('kind') != 'detail':
            # 只有**结构性**差异才拦人。`detail` 的差异（序号没底色块、图区是空的、
            # 配色深浅）是声明式渲染的必然结果，不是「版式没认出来」—— 早先一并拦下，
            # 结果「左文右图 + 序号带底色块」这类页面永远入不了库（实测）。
            bad.append('渲染出来与截图构图不一致：%s' % verify.get('reason'))
        elif verify.get('kind') == 'detail':
            meta.setdefault('_notes', []).append(
                '试片与截图只差装饰层面（不影响采用）：%s' % verify.get('reason'))
        if bad:
            log('[recognize]   试片不合格：%s' % '；'.join(bad))
            attempts.append(dict(round=rd, problems=bad, verify=verify))
            feedback = '\n'.join('- ' + b for b in bad)
            continue

        log('[recognize] 通过：%s（几何 %d error/%d warn）'
            % (meta['name'], trial['geometry']['summary']['error'],
               trial['geometry']['summary']['warn']))
        return dict(ok=True, meta=meta, trial=trial, verify=verify,
                    attempts=attempts, reason='')

    last = attempts[-1]['problems'] if attempts else ['没有产出任何结果']
    return dict(ok=False, meta=None, trial=None, verify=None, attempts=attempts,
                reason='识别了 %d 轮都不合格：%s' % (len(attempts), '；'.join(last[:3])))
