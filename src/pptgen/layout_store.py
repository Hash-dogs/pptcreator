# -*- coding: utf-8 -*-
"""自定义版式的存储与注册 —— `layouts_custom/*.json` 的读写、启用/禁用、装卸。

目录形态（默认仓库根下的 `layouts_custom/`，随仓库提交 —— 自定义版式是**资产**
不是产物，识别一次要花一次模型调用，同事之间也该共用）：

    layouts_custom/
      left_hero_stack.json     一份版式一个文件（字段见 layout_dsl 的模块注释）
      _state.json              {"disabled": ["quadrant", ...]}

## 注册一套版式要同时写四处，缺一处就是**静默失效**

    layouts.LAYOUTS[name]        渲染函数分派（不写 → 渲染 KeyError）
    layout_dsl.register_decl()   区块声明（不写 → 页面渲出来是空的，只有页眉页脚）
    layouts.LAYOUT_NAMES         合法性判据（不写 → 规划阶段把它悄悄换成 statement）
    layout_spec.REGISTRY[name]   元数据（不写 → 模型永远选不到它）

前两处是「怎么画」，后两处是「能不能被选中」。`register()` / `unregister()`
把这几件事收在一处，就是为了不再出现「加了一半」的状态。

## 禁用不等于删除

`_state.json` 里的 `disabled` 对**内置与自定义一视同仁**：被禁用的版式不进候选、
不进喂给模型的目录，但渲染函数还在 —— 旧 deck 照样能渲染、能按页修订。
删除只对自定义版式开放（内置版式的定义是 `layouts.py` 里的代码，删不掉也不该删）。
"""
from __future__ import annotations

import json
import os
import sys

from . import config, layout_dsl, layout_spec, layouts

STATE_FILE = '_state.json'
SUFFIX = '.json'


# ── 路径与状态文件 ────────────────────────────────────────────

def directory() -> str:
    return config.layouts_dir()


def _ensure_dir() -> str:
    d = directory()
    os.makedirs(d, exist_ok=True)
    return d


def state_path() -> str:
    return os.path.join(directory(), STATE_FILE)


def load_state() -> dict:
    """读启用状态。文件缺失/损坏都当成「全部启用」—— 读不了状态不该让版式全没了。"""
    try:
        with open(state_path(), encoding='utf-8') as f:
            got = json.load(f)
        if isinstance(got, dict):
            got.setdefault('disabled', [])
            return got
    except (OSError, ValueError):
        pass
    return {'disabled': []}


def save_state(state: dict) -> str:
    p = os.path.join(_ensure_dir(), STATE_FILE)
    # 不引 `pipeline.save_json`：那会把整条 pipeline（连带 llm）拉进「读注册表」
    # 这条本该很轻的路径里。写 JSON 就三行，不值得。
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write('\n')
    return p


# ── 读：目录里有哪些版式 ──────────────────────────────────────

def _meta_path(name: str) -> str:
    return os.path.join(directory(), name + SUFFIX)


def list_metas() -> list[dict]:
    """目录里的全部版式声明（**不注册**，只读）。

    顺带把每份声明**校验一遍**，不合格的挂上 `_problems` —— 界面上要能看见
    坏掉的那一份并把它删掉。静默跳过会让「文件明明在、就是不出现在列表里」
    变成查不出的怪事（加载路径也是靠这个字段决定注册不注册的）。

    ⚠️ 这里**只用 `layout_spec.REGISTRY` 这个裸字典**，不调 `layout_spec.get()`
    / `names()` —— 那些会触发 `_ensure_loaded()`，而本函数正是被 `load_all()`
    调用的，会递归回自己。
    """
    out = []
    try:
        files = sorted(os.listdir(directory()))
    except OSError:
        return out
    for fn in files:
        if not fn.endswith(SUFFIX) or fn == STATE_FILE:
            continue
        path = os.path.join(directory(), fn)
        name = fn[:-len(SUFFIX)]
        try:
            with open(path, encoding='utf-8') as f:
                meta = json.load(f)
        except (OSError, ValueError) as e:
            out.append(dict(name=name, _problems=['读不了这个文件：%s' % e], _path=path))
            continue
        if not isinstance(meta, dict):
            out.append(dict(name=name, _problems=['文件内容不是对象'], _path=path))
            continue
        meta.setdefault('name', name)
        meta['_path'] = path
        out.append(meta)

    disk_names = {m.get('name') for m in out}
    for meta in out:
        if meta.get('_problems'):
            continue
        # 重名检查要排掉**自己**：同一份声明会被反复读，不能自己跟自己冲突
        existing = (set(layout_spec.REGISTRY) | disk_names) - {meta.get('name')}
        bad = layout_dsl.problems(meta, existing=existing)
        if bad:
            meta['_problems'] = bad
    return out


# ── 注册 / 注销 ───────────────────────────────────────────────

def register(meta: dict) -> layout_spec.LayoutSpec:
    """把一份声明挂进注册表（四处一起写，见模块注释）。"""
    name = meta['name']
    spec = layout_spec.LayoutSpec(
        name=name,
        roles=tuple(meta.get('roles') or ('content',)),
        intents=tuple(meta.get('intents') or ()),
        source='custom',
        min_items=meta.get('min_items'),
        max_items=meta.get('max_items'),
        item_chars=int(meta.get('item_chars') or 0),
        total_chars=int(meta.get('total_chars') or 0),
        max_item_chars=meta.get('max_item_chars'),
        requires=tuple(meta.get('requires') or ()),
        filter_items=bool(meta.get('filter_items', True)),
        signature=str(meta.get('signature') or ''),
        best_for=str(meta.get('best_for') or ''),
        avoid_for=str(meta.get('avoid_for') or ''),
        fallback=tuple(meta.get('fallback') or ()),
        reuse_friendly=bool(meta.get('reuse_friendly', True)),
        # catalog / capacity 由区块**生成**，不存 JSON：存一份就会与渲染器漂移
        catalog=meta.get('catalog') or layout_dsl.catalog_of(meta),
        capacity=meta.get('capacity') or layout_dsl.capacity_of(meta),
    )
    layouts.LAYOUTS[name] = layout_dsl.render_blocks
    layout_dsl.register_decl(name, meta.get('blocks') or [])
    if name not in layouts.LAYOUT_NAMES:
        layouts.LAYOUT_NAMES.append(name)
    return layout_spec.register(spec)


def unregister(name: str) -> None:
    """从注册表里摘掉一套**自定义**版式（内置的不动：那是代码）。

    ⚠️ 一律用 `layout_spec.get()`（它会 `_ensure_loaded()`）而不是直接查
    `REGISTRY` 这个裸字典 —— 后者在一个还没读过注册表的进程里是空的，
    `REGISTRY.get(name)` 会返回 None，于是**内置版式的那道守卫被绕过**，
    `--rm statement` 会打印「已删除」而什么也没删（实测踩过）。
    """
    sp = layout_spec.get(name)
    if sp is None:
        raise ValueError('没有这套版式：%s' % name)
    if sp.source != 'custom':
        raise ValueError('%s 是内置版式，只能禁用、不能删除' % name)
    layouts.LAYOUTS.pop(name, None)
    layout_spec.REGISTRY.pop(name, None)
    layout_dsl.register_decl(name, [])
    while name in layouts.LAYOUT_NAMES:
        layouts.LAYOUT_NAMES.remove(name)


def is_registered(name: str) -> bool:
    return name in layout_spec.REGISTRY


# ── 加载（幂等）────────────────────────────────────────────────

_LOADED = False


def load_all(force: bool = False) -> int:
    """把 `layouts_custom/` 下的版式与启用状态读进注册表。返回加载的版式数。

    幂等：`layout_spec._ensure_loaded()` 每次读注册表都会调它，重复调用必须是
    便宜的 no-op。`force=True` 用于测试与「新增后立刻生效」的场景。
    """
    global _LOADED
    if _LOADED and not force:
        return len(_custom_names())
    _LOADED = True

    state = load_state()
    known = set(layout_spec.REGISTRY)
    disabled = {n for n in (state.get('disabled') or []) if n in known}
    # 每轮都按**磁盘上的文件**重算：force 重载时旧的自定义版式要先摘掉，否则
    # 删掉的文件会以「幽灵版式」的形式留在注册表里，直到重启进程。
    on_disk = {m.get('name') for m in list_metas()}
    for name in _custom_names():
        if name not in on_disk:
            unregister(name)

    ok = 0
    for meta in list_metas():
        # 校验由 list_metas 做完（它要保证界面能看到坏掉的那一份），这里只管注册
        if meta.get('_problems'):
            _warn('%s 有问题，已跳过：%s'
                  % (meta.get('name'), '；'.join(meta['_problems'])))
            continue
        try:
            register(meta)
            ok += 1
        except Exception as e:                      # noqa: BLE001 —— 一个坏文件不该拖垮全部
            _warn('%s 注册失败：%s' % (meta.get('name'), e))

    layout_spec.set_disabled(disabled)
    return ok


def _custom_names() -> list[str]:
    # `names()`（而非裸 REGISTRY）会先确保加载 —— 否则冷启动时这里返回空，
    # 「幽灵版式」的清理逻辑就永远不会触发。
    return [n for n in layout_spec.names()
            if layout_spec.REGISTRY[n].source == 'custom']


def _warn(msg: str) -> None:
    """坏文件只告警不抛 —— 版式目录损坏不该让整个程序打不开。"""
    sys.stderr.write('[layouts] %s\n' % msg)


# ── 写：新增 / 删除 / 启停 ────────────────────────────────────

def save_meta(meta: dict) -> str:
    """落盘一份版式声明并**立刻注册**（不重启就生效）。返回文件路径。

    同名可以重存（编辑一份已有的自定义版式走的就是这条路）。但**不许覆盖内置
    版式** —— 那会把 `LAYOUTS['statement']` 换成声明式渲染器，把内置版式悄悄
    弄坏，而外表上什么异常都没有。
    """
    name = meta.get('name') or ''
    sp = layout_spec.get(name)
    if sp is not None and sp.source != 'custom':
        raise ValueError('%s 是内置版式，不能覆盖。换个名字。' % name)
    # 重名检查排掉自己（重存同一份声明是合法的）
    bad = layout_dsl.problems(meta,
                              existing={n for n in layout_spec.REGISTRY if n != name})
    if bad:
        raise ValueError('；'.join(bad))
    path = os.path.join(_ensure_dir(), name + SUFFIX)
    payload = {k: v for k, v in meta.items() if not k.startswith('_')}
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write('\n')
    register(meta)
    return path


def remove_meta(name: str) -> bool:
    """删掉一套自定义版式：先摘注册表，再删文件。返回是否真的删了文件。"""
    unregister(name)                 # 内置的会在这里抛 ValueError
    path = _meta_path(name)
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False


def set_enabled(name: str, on: bool) -> dict:
    """启用/禁用一套版式（内置与自定义都可以）。返回新的状态。"""
    if layout_spec.get(name) is None:        # 用 get()：它会先加载注册表
        raise ValueError('没有这套版式：%s' % name)
    state = load_state()
    got = {n for n in (state.get('disabled') or [])}
    if on:
        got.discard(name)
    else:
        got.add(name)
    state['disabled'] = sorted(got)
    save_state(state)
    layout_spec.set_disabled(state['disabled'])
    return state


def info(name: str) -> dict:
    """一套版式的完整信息（界面与 CLI 共用）：元数据 + 来源 + 启用状态 + 分组。"""
    # 用 `layout_spec.get`（它会 `_ensure_loaded()`）而不是直接查 `REGISTRY` ——
    # 后者在一个还没读过注册表的进程里是个空字典，会静默返回 {}。
    sp = layout_spec.get(name)
    if sp is None:
        return {}
    return dict(
        name=name, source=sp.source, enabled=layout_spec.is_enabled(name),
        roles=list(sp.roles), intents=list(sp.intents),
        signature=sp.signature, best_for=sp.best_for, avoid_for=sp.avoid_for,
        items=sp.item_range(), item_chars=sp.item_chars, total_chars=sp.total_chars,
        fallback=list(sp.fallback),
        # 图鉴的分组按主职意图来（与 gallery.py 同一套规则）；中文标签也由服务端给，
        # 免得前端再抄一份意图映射（那就是第三份清单了）。
        group=(sp.intents[0] if sp.intents else ''),
        intent_label=(layout_spec.INTENT_SHORT.get(sp.intents[0], sp.intents[0])
                      if sp.intents else '结构骨架'),
    )
