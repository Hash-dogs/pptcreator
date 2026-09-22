# -*- coding: utf-8 -*-
"""版式元数据 —— 版式目录、容量约束、意图映射的**单一真相来源**。

在这个文件出现之前，同一份版式清单手写在三处，并且已经漂移：

    layouts.LAYOUTS            渲染函数（事实上的唯一真相）
    pipeline.LAYOUT_CATALOG    喂给模型的自然语言目录
    repair.LAYOUT_CAPACITY     压文案时的字数预算

`stat_hero.stats.label` 在前者写「≤20 字」、在后者写「≤22 字」—— 模型先看到 20，
被压时被告知 22。现在三者都从同一份 `REGISTRY` 派生：渲染函数在 layouts.py 里用
`@layout(...)` 注册（元数据与函数体写在一起），目录文本与容量文本由
`catalog_text()` / `capacity_text()` 生成，不可能再对不上。

另外两个机制借鉴自开源方案（PPTAgent 与 gpt-image2-ppt-skills）：

- **意图映射 + 候选收缩**：先用 `page_role` / `content_intent` 把 19 套收窄到 2–4 个
  候选，再让模型在候选内选。让模型从全部版式里盲选，是「大纲写『六参数对比表』、
  规划却选了 timeline_vertical」这类错配的温床。
- **容量前置校验**：条目数不落在 `[min_items, max_items]` 内的版式**直接剔除**，
  而不是等渲染时由 `_fit()` 静默截断。历史案例（版式已删除）：`node_flow` 那 8 个
  节点有 6 个被截成残句（`小红书正文 · 爆款写作…`），而几何检查是全绿的 ——
  截断让文字不再溢出，于是 QA 看不见它。
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field

# ── 分类维度 ──────────────────────────────────────────────────
# page_role 只有两档，**刻意不再细分「开场 / 目录 / 收尾」**：
# 位置语义已经由 content_intent 承担（statement 是开场、summary/quote 是收尾），
# 再加一层位置维度只会让「首尾页绕过意图过滤、候选塌成一个」——
# 实测这么写之后 13 页内容全部退化成 statement。
ROLES = ('section', 'content')

# content_intent: page_role=content 时，这一页**在表达什么**（语义）
INTENTS = ('statement', 'definition', 'enumeration', 'comparison', 'process',
           'timeline', 'hierarchy', 'quantitative', 'status', 'summary', 'quote')

INTENT_LABELS = {
    'statement':    '单点主张（一句话结论/判断）',
    'definition':   '概念解释（术语 + 释义）',
    'enumeration':  '并列列举（若干同级要点）',
    'comparison':   '对照（A/B 两方，或优劣、前后）',
    'process':      '步骤流程（有先后的动作）',
    'timeline':     '时间推进（阶段/里程碑，带时间感）',
    'hierarchy':    '层级包含（分层、支撑、嵌套关系）',
    'quantitative': '数据指标（数字、指标、参数、表格）',
    'status':       '状态进度（已完成/进行中/风险）',
    'summary':      '结论回收（把全篇要点收成几条）',
    'quote':        '引语（一句被引用的话）',
}


@dataclass(frozen=True)
class LayoutSpec:
    """一套版式的元数据。`catalog` / `capacity` 是给模型看的自然语言契约。"""
    name: str
    roles: tuple[str, ...]              # 适用的 page_role
    intents: tuple[str, ...] = ()       # 适用的 content_intent（空 = 只按角色用）
    # ── 容量（程序校验，不是靠模型自觉）──────────────────────
    min_items: int | None = None        # 主列表条目数下限
    max_items: int | None = None        # 上限 —— 超出直接剔除该版式
    item_chars: int = 0                 # 单条建议字数（进目录文本，引导模型压短）
    total_chars: int = 0                # 全页建议字数预算
    # 单条字数的**硬上限**，用于校验**模型写出来的成品**（不是源文档的条目 ——
    # 源条目天然是一整句，拿它去卡候选会把所有版式筛光）。默认取 item_chars + 6
    # 作为折行容忍；真正装不下的（**单行定高**的字段，如 layered_stack 的模块框
    # 16 字、timeline_vertical 半幅宽度下的 name+desc）要写死，否则长标签会被
    # `_fit()` 静默截断成残句。
    max_item_chars: int | None = None
    requires: tuple[str, ...] = ()      # 内容形态前置条件: 'table' / 'numbers'
    # min/max_items 是否参与**预筛**（拿源文档的条目数去卡候选）。
    # 有些版式的条目是源条目的**再分组**：`phase_grouped_flow` 的阶段（2–4 组）
    # 由若干节点归并而来，源文的 8 条是节点、不是阶段 —— 拿 8 去卡「阶段 ≤4」
    # 会把它误杀。这类版式设 False：条数只在**事后**校验模型写出来的成品。
    filter_items: bool = True
    # ── 选择辅助 ────────────────────────────────────────────
    signature: str = ''                 # 视觉签名，给模型比较候选用
    best_for: str = ''
    avoid_for: str = ''
    fallback: tuple[str, ...] = ()      # 装不下时的降级链
    reuse_friendly: bool = True         # False = 全篇只该出现一次
    # ── 给模型的文本 ────────────────────────────────────────
    catalog: str = ''                   # 字段契约（进 LAYOUT_CATALOG）
    capacity: str = ''                  # 超长时的压缩要求（进 repair）

    @property
    def hard_item_chars(self) -> int:
        """单条字数的**截断上限**。只在显式声明时生效（未声明返回 0 = 不检查）。

        注意它与 `item_chars` 是两回事：`item_chars` 是「建议写多短」的引导
        （进目录文本），`max_item_chars` 是「超过就会被 `_fit()` 静默截断」的
        硬边界。两者的差距可能很大 —— numbered_columns 的 desc 框在 3.4"×1.0"
        里按 13pt 放得下约 90 字，而引导值只有 22 字。

        所以这里**不做 +6 推导**：绝大多数版式的条目是多行文本，写长了只是
        溢出（几何检查会报 text_overflow、修复回环会压短，是可恢复的）；
        只有单行定高字段被截断才是**不可恢复**的信息丢失，那几个才值得写死。
        """
        return self.max_item_chars or 0

    def item_range(self) -> str:
        if self.min_items is None:
            return ''
        if self.min_items == self.max_items:
            return '正好 %d 条' % self.min_items
        return '%d–%d 条' % (self.min_items, self.max_items)


REGISTRY: dict[str, LayoutSpec] = {}

_LOADED = False


def _ensure_loaded() -> None:
    """确保渲染器已注册。

    `REGISTRY` 是 layouts.py 里 `@layout(...)` 装饰器的产物，所以**只 import
    layout_spec 的话它是空的**。这个函数把那次 import 推迟到第一次访问，
    免得「必须先 import layouts」变成一条隐形约定 —— 调用方只 import
    layout_spec 却拿到空目录，是最难查的那种错。
    推迟也顺手避开了 layout_spec ↔ layouts 的循环 import。

    只在**读**访问器里调用，绝不在 `register()` 里调 —— 那会在 layouts 导入
    过程中递归回自己。
    """
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    from . import layouts  # noqa: F401  （导入即注册）


def register(spec: LayoutSpec) -> LayoutSpec:
    REGISTRY[spec.name] = spec
    return spec


def get(name: str) -> LayoutSpec | None:
    _ensure_loaded()
    return REGISTRY.get(name)


# ══════════════════════════════════════════════════════════════
# 内容形态 —— 从源文档块算出，**不听模型自述**
# ══════════════════════════════════════════════════════════════
_NUM_RE = re.compile(r'\d')


def shape_of(blocks: list[dict]) -> dict:
    """一页素材的形态特征。用来做容量前置校验。

    这些量必须由程序从源块算出来：让模型自报「我这页有几条」再据此校验，
    等于让它自己给自己出题，校验就失去意义了。
    """
    items: list[str] = []
    for b in blocks or []:
        t = b.get('type')
        if t == 'bullets':
            items += [i for i in (b.get('items') or [])]
        elif t == 'para':
            items.append(b.get('text') or '')
        elif t == 'table':
            for row in (b.get('rows') or []):
                items += [c for c in row]
    items = [i.strip() for i in items if (i or '').strip()]
    text = ''.join(items)
    return dict(
        known=True,
        n_items=len(items),
        has_table=any(b.get('type') == 'table' for b in (blocks or [])),
        has_numbers=bool(_NUM_RE.search(text)),
        max_item_chars=max((len(i) for i in items), default=0),
        total_chars=len(text),
    )


# 「**没取到**素材」——不是「素材为空」。锚点查不到时就是这个状态。
# 区别很重要：前者的正确反应是**不设限**（让模型照常选版式），后者才是把
# 装不下的版式剔除。早先把两者混为一谈，于是锚点一失效整份 deck 就退化成
# 只有标题的空白页。
EMPTY_SHAPE = dict(known=False, n_items=0, has_table=False, has_numbers=False,
                   max_item_chars=0, total_chars=0)


def _capacity_ok(sp: LayoutSpec, shape: dict) -> bool:
    """内容形态是否装得进这套版式。装不进就该在**选之前**剔除。"""
    if not shape.get('known'):
        return True                      # 素材未知 → 不做任何形态否决

    n = shape.get('n_items', 0)
    if n and sp.filter_items:
        if sp.min_items is not None and n < sp.min_items:
            return False
        if sp.max_items is not None and n > sp.max_items:
            return False
    if 'table' in sp.requires and not shape.get('has_table'):
        return False
    if 'numbers' in sp.requires and not shape.get('has_numbers'):
        return False
    # ⚠️ **不按 max_item_chars 筛**。源文档里的条目本来就是一整句（实测 40–120 字），
    # 而版式的单条预算是针对**成品**的（22 字）—— 模型的工作正是把长句压短。
    # 早先拿源条目的长度去卡候选，版式被筛得只剩 statement，13 页内容全塌成一种版式。
    # 单条字数改为**事后**校验模型写出来的成品：见 pipeline._overflowing_items。
    return True


def candidates(role: str, intent: str, shape: dict | None = None) -> list[LayoutSpec]:
    """按角色 + 意图 + 内容形态，把 19 套收窄成候选集。"""
    _ensure_loaded()
    shape = shape if shape is not None else EMPTY_SHAPE
    out = []
    for sp in REGISTRY.values():
        if role == 'section':
            # 结构页只由结构版式承担，且不参与内容驱动的选择
            if sp.roles == ('section',):
                out.append(sp)
            continue
        if sp.roles == ('section',):
            continue
        if sp.intents and intent not in sp.intents:
            continue
        if not _capacity_ok(sp, shape):
            continue
        out.append(sp)
    # 排序：**以该意图为主职的版式排在前面**（intents[0] 命中），兼职工排在后面。
    # 定义顺序（REGISTRY 的插入顺序）在同类内保持不变。
    # 没有这一步，候选顺序就等于「作者写渲染函数的顺序」——模型被告知
    # 「排在前面的更贴题」，而那个顺序其实与贴题无关。
    return sorted(out, key=lambda s: 0 if (s.intents and s.intents[0] == intent) else 1)


def resolve(sp: LayoutSpec | None, role: str, intent: str,
            shape: dict | None = None, taken: set[str] | None = None) -> LayoutSpec:
    """候选集为空时的降级：沿 fallback 链走，最后兜底 statement。

    `taken` 是本次已经选定过的版式名 —— 降级时优先挑没用过的，
    避免「所有页都降级成同一个版式」这种兜底反而更单调的结果。
    """
    take = taken or set()
    seen = set()

    def walk(s: LayoutSpec | None) -> LayoutSpec | None:
        if s is None or s.name in seen:
            return None
        seen.add(s.name)
        if s.name not in take:
            return s
        for nxt in s.fallback:
            got = walk(REGISTRY.get(nxt))
            if got is not None:
                return got
        return None

    got = walk(sp)
    if got is not None:
        return got
    # 全局兜底：同角色的候选里挑第一个没用过的，再不济就用 statement
    pool = candidates(role, intent, shape) if role else []
    for s in pool:
        if s.name not in take:
            return s
    return REGISTRY.get('statement') or LayoutSpec(name='statement', roles=('content',))


# ══════════════════════════════════════════════════════════════
# 给模型的文本 —— 从注册表生成，不再手写
# ══════════════════════════════════════════════════════════════
def catalog_text() -> str:
    """完整版式目录（规划阶段的 prompt 用）。"""
    _ensure_loaded()
    L = ['**每个版式都有两个公共字段**（别漏，页眉靠它们）：',
         '  title  —— 本页标题，一句话主张，≤24 字。statement / quote / '
         'section_divider 没有标题位，不用给。',
         '  kicker —— 左上角小字章节标签，填所属章节名（如「03 Dify 能做什么」）。',
         '  source —— 页脚来源标注。',
         '',
         '可用版式（layout 字段填左边的名字）：', '']
    for i, sp in enumerate(REGISTRY.values(), 1):
        rng = sp.item_range()
        head = '%d. %s —— %s' % (i, sp.name, sp.signature or sp.best_for)
        L.append(head)
        if rng:
            L.append('   容量：%s。' % rng)
        for line in (sp.catalog or '').strip().splitlines():
            L.append('   ' + line.strip())
        L.append('')
    return '\n'.join(L)


def candidate_text(names: list[str]) -> str:
    """候选集文本（终选阶段用）—— 带容量声明，不是裸名字。

    PPTAgent 的 layout_selector 把「版式名 + 元素类型 + 长度 + 建议字数」一起
    交给模型，而不是只给名字。裸名字等于让模型凭名字猜容量。
    """
    _ensure_loaded()
    L = []
    for n in names:
        sp = REGISTRY.get(n)
        if sp is None:
            L.append('- %s' % n)
            continue
        bits = [b for b in (sp.signature, sp.item_range(),
                            ('单条 ≤%d 字' % sp.item_chars) if sp.item_chars else '',
                            ('适合：' + sp.best_for) if sp.best_for else '',
                            ('不适合：' + sp.avoid_for) if sp.avoid_for else '') if b]
        L.append('- **%s** —— %s' % (n, '；'.join(bits)))
    return '\n'.join(L)


def capacity_text(name: str) -> str:
    """压文案时的容量说明（repair 用）。"""
    _ensure_loaded()
    sp = REGISTRY.get(name)
    if sp is None or not sp.capacity:
        return '尽量精简，控制在原文的 60% 以内。'
    return sp.capacity


def names() -> list[str]:
    _ensure_loaded()
    return list(REGISTRY)
