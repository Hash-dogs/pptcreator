#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地 Web 前端 —— 只用标准库，不加任何依赖。

启动::

    python server.py            # 默认 http://127.0.0.1:8000
    python server.py --port 8080

交互链路（对应 CLI 的 full）::

    选源文档 → 生成大纲 → 〔页面里改〕→ 生成（规划 → 修复回环 → 渲染）→ 预览 → 下载

生成过程会调用模型，耗时以分钟计，所以跑在后台线程里，前端轮询 /api/job/<id> 拿日志。
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, 'src'))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

from pptgen import build as build_mod          # noqa: E402
from pptgen import config as cfg_mod           # noqa: E402
from pptgen import layout_spec                 # noqa: E402
from pptgen import layout_store                # noqa: E402
from pptgen import parse as parse_mod          # noqa: E402
from pptgen import pipeline                    # noqa: E402
from pptgen import recognize as recognize_mod  # noqa: E402
from pptgen import repair as repair_mod        # noqa: E402
from pptgen import revise as revise_mod        # noqa: E402
from pptgen import runlog as runlog_mod        # noqa: E402
from pptgen import samples                     # noqa: E402
from pptgen.qa import geometry                 # noqa: E402
from pptgen.qa import visual as visual_mod     # noqa: E402

WEB = os.path.join(ROOT, 'web')
# 与 run.py 共用同一份定义（config.out_sub），别再各写一份 `out/<sub>`。
# 这些常量在 import 期求值，而 `out_dir()` 是调用时读 env —— 必须先读 .env，
# 否则 `PPTGEN_OUT` / `PPTGEN_LOG_DIR` 设了也不生效。`load_env` 幂等，可重复调。
cfg_mod.load_env()
SAMPLES = cfg_mod.out_sub('samples')
PLANS = cfg_mod.out_sub('plans')
REPORTS = cfg_mod.out_sub('reports')
VISUAL = cfg_mod.out_sub('visual')
UPLOADS = cfg_mod.out_sub('uploads')
LOGS = cfg_mod.log_root()

SOURCE_EXT = ('.pptx', '.docx', '.pdf', '.txt', '.md', '.markdown')
MAX_UPLOAD = 40 * 1024 * 1024          # 单文件上限 40MB

# ── 版式管理（自定义版式：识别 / 采用 / 启停 / 删除）──────────
# 版式预览图落在 `out/visual/_layouts/<版式名>/page-03.png`：现成的 `/preview/`
# 路由服务的就是 out/visual 下的文件，不必再加一条静态路由。
LAYOUT_VISUAL = os.path.join(VISUAL, '_layouts')
SHOTS = os.path.join(UPLOADS, '_layout_shots')      # 上传的版式截图
SHOT_EXT = ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif')
PAGE = recognize_mod.FIRST_CONTENT_PAGE             # 单页 deck 里正文那一页
DRAFT_SUFFIX = 'layout-%s.json'                     # 识别草稿（刷新后还能接上）

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

# 上传的文件既落盘也留在内存：解析优先用内存副本，落盘那份负责持久化
# （重启后还能从下拉里选回来、能列出、能删）。两条路都留着，互不依赖。
UPLOAD_MEM: dict[str, dict] = {}       # id -> {id, name, stored, path, size, data, mtime}
UPLOAD_LOCK = threading.Lock()
_MEM_KEEP = 8                          # 内存里最多留几个上传
_MEM_MAX_BYTES = 64 * 1024 * 1024      # 且总量不超过这个数


# ══════════════════════════════════════════════════════════════
# 路径与名字（上传入口的一侧全是不可信输入，全部在这里收敛）
# ══════════════════════════════════════════════════════════════
def _safe_join(base: str, rel: str) -> str | None:
    """把 `rel` 解析到 `base` 下；越界就返回 None。

    `/preview/../../.env` 这类请求以前是能读到文件的（只是绑在 127.0.0.1
    才没出事），统一过这一层。
    """
    base_abs = os.path.abspath(base)
    p = os.path.normpath(os.path.join(base_abs, (rel or '').replace('\\', '/').lstrip('/')))
    if p == base_abs or p.startswith(base_abs + os.sep):
        return p
    return None


def _sanitize_name(raw: str) -> str:
    """把上传的文件名收敛成一个「安全且可读」的纯文件名。

    只取 basename —— 名字来自 query，必须假设它是敌意的。
    扩展名必须在 SOURCE_EXT 内，否则解析层也认不了。
    """
    name = os.path.basename(urllib.parse.unquote(raw or '').replace('\\', '/')).strip()
    name = re.sub(r'[^\w.\-]+', '_', name).strip('._')
    if not name:
        raise ValueError('文件名为空')
    stem, ext = os.path.splitext(name)
    if ext.lower() not in SOURCE_EXT:
        raise ValueError('不支持的格式 %s；支持：%s'
                         % (ext or '(无扩展名)', ' / '.join(SOURCE_EXT)))
    return (stem[:60] or 'upload') + ext.lower()


def _unique_path(name: str) -> str:
    """落盘路径，重名自动加 -1 / -2。"""
    stem, ext = os.path.splitext(name)
    cand = os.path.join(UPLOADS, name)
    i = 1
    while os.path.exists(cand):
        cand = os.path.join(UPLOADS, '%s-%d%s' % (stem, i, ext))
        i += 1
    return cand


# 源文档来自哪：项目根目录还是上传目录
ROOT_DIR, UPLOADS_DIR = 'root', 'upload'


def _find_source(name: str) -> tuple[str, str] | None:
    """按**纯文件名**在项目根目录与上传目录里找源文档。

    返回 `(绝对路径, 来源)`，`来源 ∈ {'root', 'upload'}`；找不到返回 None。

    必须**连来源一起返回**，不能只回路径：搜索顺序是 `(ROOT, UPLOADS)`，
    **根目录的同名文件会遮蔽上传的那份**。这是真实存在的静默行为 ——
    上传 `报告.docx` → 重启服务 → 胶囊从磁盘恢复（此时前端发的是 `{src: 名字}`）
    → 命中的其实是根目录那份，而界面上胶囊仍显示「使用中」。
    把来源记进日志，这类事才看得见。

    只认 basename，所以 `../../.env` 这类 `src` 会被收敛成 `.env`；
    但**扩展名也必须落在 SOURCE_EXT 内** —— 否则它会把根目录下的 `.env`
    之类的非文档文件也认成「源文档」，白起一个必然失败的任务。
    """
    base = os.path.basename((name or '').replace('\\', '/'))
    if not base or os.path.splitext(base)[1].lower() not in SOURCE_EXT:
        return None
    for d, origin in ((ROOT, ROOT_DIR), (UPLOADS, UPLOADS_DIR)):
        p = os.path.join(d, base)
        if os.path.isfile(p):
            return p, origin
    return None


def _evict_mem():
    """内存里只留最近若干个上传。调用方需持有 UPLOAD_LOCK。"""
    while len(UPLOAD_MEM) > 1 and (
            len(UPLOAD_MEM) > _MEM_KEEP
            or sum(e['size'] for e in UPLOAD_MEM.values()) > _MEM_MAX_BYTES):
        oldest = min(UPLOAD_MEM.values(), key=lambda e: e['mtime'])
        UPLOAD_MEM.pop(oldest['id'], None)


def _list_uploads() -> list[dict]:
    """内存里的（本次会话）+ 磁盘上的（跨重启），按名字去重。"""
    out, seen = [], set()
    with UPLOAD_LOCK:
        for e in sorted(UPLOAD_MEM.values(), key=lambda x: -x['mtime']):
            seen.add(e['name'])
            out.append(dict(name=e['name'], size=e['size'], mtime=e['mtime'],
                            kind='upload', session=True))
    if os.path.isdir(UPLOADS):
        for f in sorted(os.listdir(UPLOADS)):
            if f in seen or os.path.splitext(f)[1].lower() not in SOURCE_EXT:
                continue
            p = os.path.join(UPLOADS, f)
            if os.path.isfile(p):
                out.append(dict(name=f, size=os.path.getsize(p),
                                mtime=os.path.getmtime(p), kind='upload',
                                session=False))
    return out


# ══════════════════════════════════════════════════════════════
# 后台任务
# ══════════════════════════════════════════════════════════════
def _job_new() -> str:
    jid = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[jid] = dict(id=jid, status='running', log=[], progress=[],
                         result=None, error=None, started=time.time())
    return jid


def _spawn(fn, args: tuple, mode: str | None = None):
    """把一个任务丢进后台线程跑；`mode` 是这次流程选的内容策略。

    覆盖只在**这个线程**里生效（`config.content_mode_override`）—— 任务各跑
    一个线程，同时跑两个任务时互不干扰，也不会污染 CLI 那一侧的 `.env` 读数。
    `None` / 非法值 = 不覆盖，仍按 `.env` 的 `PPTGEN_CONTENT_MODE` 走。
    """
    def _run():
        with cfg_mod.content_mode_override(mode):
            fn(*args)
    threading.Thread(target=_run, daemon=True).start()


def _log(jid: str, msg: str, level: str = 'info', detail: str = ''):
    """一条日志同时喂两个视图：`log` 是纯文本（终端的流水），
    `progress` 是结构化的（侧边栏进度面板按 level 上色）。

    有流程日志时**再写一份到 run.log** —— 服务器不能像 CLI 那样 tee stdout：
    `log_message`（本文件末尾）把每个 HTTP 请求都写进 `sys.stderr`，
    并发任务的日志会互相污染。这里是所有消息的汇集点，所以在这儿分流。
    """
    runlog = None
    with JOBS_LOCK:
        j = JOBS.get(jid)
        if j is not None:
            if level == 'active':
                # 同时只让一个点在跳：新步骤开始，上一个"进行中"降级为普通记录，
                # 否则进度面板会挂着一排永远在跳的点。
                for prev in j['progress']:
                    if prev['level'] == 'active':
                        prev['level'] = 'info'
            j['log'].append(msg)
            j['progress'].append(dict(level=level, message=msg, detail=detail))
            runlog = j.get('runlog')
    if runlog is not None:
        # 文件 I/O 放在锁外，别占着 JOBS_LOCK
        runlog.log(msg if not detail else '%s（%s）' % (msg, detail))


def _finish(jid: str, result=None, error=None):
    runlog = None
    with JOBS_LOCK:
        if jid in JOBS:
            JOBS[jid]['status'] = 'failed' if error else 'done'
            JOBS[jid]['result'] = result
            JOBS[jid]['error'] = error
            # 任务结束了就不要再有点在跳：否则最后那个"进行中"会一直转下去，
            # 看起来像还没跑完。收尾那条 success / warning 自带结论。
            for p in JOBS[jid]['progress']:
                if p['level'] == 'active':
                    p['level'] = 'info'
            runlog = JOBS[jid].get('runlog')
    if runlog is not None:
        runlog.finish(ok=not error, error=error)


def run_generate(jid: str, outline: dict, parsed_path: str, name: str, rounds: int):
    """规划 → 修复回环 → 渲染。在后台线程里跑。"""
    try:
        # 接上大纲那次的流程文件夹（sidecar 由 `/api/outline` 写在 parsed.json 旁边）。
        # 找不到就新建一个 —— 用户从别处粘一份 outline 进来是合法操作，
        # 不该因为「接不上」就拒绝他。
        side = _read_sidecar(parsed_path) or {}
        runlog = runlog_mod.RunLog(name, origin='web', command='generate')
        if side.get('run_dir'):
            if not runlog.open_at(side['run_dir']):
                runlog.put('warning', '接不上原流程文件夹，已新建：%s' % side['run_dir'])
        with JOBS_LOCK:
            if jid in JOBS:
                JOBS[jid]['runlog'] = runlog
        runlog.put('deck_name', name)
        if not runlog._data.get('config'):
            runlog.put('config', runlog_mod.config_snapshot())
        _log(jid, '本次流程日志：%s' % runlog.dir, 'info')

        # 重新规划会从大纲**重做整份 deck**，磁盘上的按页修改根本不会参与 ——
        # 所以先提醒。这与项目里「兜底产物必须显式标出」是同一条原则：
        # 用户不该在事后才发现自己改了半天的东西没了。
        prior = _ledger_changes(name)
        if prior['count']:
            _log(jid, '注意：这份 deck 之前有 %d 处人工修改（第 %s 页），'
                      '本次重新规划会把它们覆盖掉'
                 % (prior['count'], '、'.join(str(p) for p in prior['pages'])),
                 'warning')

        _log(jid, '读取解析结果…')
        doc = pipeline.load_json(parsed_path)

        with runlog.stage('plan') as st:
            _log(jid, '规划每页版式与内容（调用模型）…', 'active')
            # 把 pipeline 内部的进度/失败原因也送进任务日志，否则前端只能看到沉默。
            # 单次模型调用最坏能沉默 9 分钟（PPTGEN_TIMEOUT×重试），这几行就是心跳。
            plan = pipeline.make_plan(outline, doc, on_log=lambda m: _log(jid, m))
            deck_path = os.path.join(PLANS, '%s.deck.json' % name)
            pipeline.save_json(plan, deck_path)
            counts = {}
            for s in plan['slides']:
                counts[s['layout']] = counts.get(s['layout'], 0) + 1
            st.update(slides=len(plan['slides']))
            # 用**实例**方法，不要用 `runlog_mod.note()` —— 后者走的是线程局部，
            # 只有 `scope()` 会设它，而服务器是直接建实例挂到 job 上的。
            runlog.note('plan', slides=len(plan['slides']), layouts=counts,
                        generated_by=plan.get('_generated_by'), deck_json=deck_path)
            _log(jid, '规划完成：%d 页，版式分布 %s' % (len(plan['slides']), counts),
                 'success')

        pptx = os.path.join(SAMPLES, '%s.pptx' % name)

        def build_qa(spec):
            build_mod.build(spec, cfg_mod.template_path(), pptx, fill_toc=True,
                            on_log=lambda m: _log(jid, m))
            rep = geometry.analyse(pptx)
            s = rep['summary']
            _log(jid, '几何检查：%d error / %d warn' % (s['error'], s['warn']),
                 'warning' if s['error'] else 'success')
            return rep

        with runlog.stage('repair') as st:
            _log(jid, '开始修复回环（最多 %d 轮）…' % rounds, 'active')
            deck, final = repair_mod.repair_deck(plan, build_qa, rounds=rounds,
                                                 verbose=False,
                                                 on_log=lambda m: _log(jid, m))
            deck_live = os.path.join(PLANS, '%s.deck.repaired.json' % name)
            pipeline.save_json(deck, deck_live)

            # 用最终 spec 重建一次，确保落盘的是修复后的版本
            build_mod.build(deck, cfg_mod.template_path(), pptx, fill_toc=True,
                            on_log=lambda m: _log(jid, m))
            rep = geometry.analyse(pptx)
            pipeline.save_json(rep, os.path.join(REPORTS, '%s.geometry.json' % name))
            with open(os.path.join(REPORTS, '%s.geometry.md' % name), 'w',
                      encoding='utf-8') as f:
                f.write('# 几何 QA 报告\n\n```\n' + geometry.format_report(rep) + '\n```\n')
            st.update(rounds=rounds)
            runlog.note('qa', error=rep['summary']['error'],
                        warn=rep['summary']['warn'],
                        issues=rep['issues'][:20])
            # 归档**人工确认后**的大纲：`/api/generate` 会把它存成
            # PLANS/<deck 名>.outline.json，而 deck 名默认就等于 stem ——
            # 于是它覆盖掉大纲阶段写的那份，模型产出的原始大纲就此消失。
            # 流程日志是它唯一的幸存地。
            runlog.attach_file('confirmed_outline', _write_confirmed_outline(outline, name))
            runlog.attach_pptx(pptx)

        with runlog.stage('render') as st:
            work = os.path.join(VISUAL, name)
            _log(jid, '渲染每页预览图…', 'active')
            shot = _render(pptx, work, deck, deck_path=deck_live)
            pages = shot['pages']
            # 「本次重渲了几张」必须打出来：整目录重渲（十几页、两三分钟）与
            # 增量重渲（一页、十秒）在日志上否则长得一模一样。
            st.update(pages=len(pages), rendered=len(shot['rendered']))
            runlog.note('render', pages=len(pages), rendered=shot['rendered'],
                        visual_dir=work)
            _log(jid, '预览图 %d 张（本次重渲 %d 张）'
                 % (len(pages), len(shot['rendered'])), 'success')

        # `deck` 与 `deck_live` 是**两份不同的文件**，别混用：
        #   deck      = `<name>.deck.json`          规划刚产出、**修复回环之前**的那份
        #   deck_live = `<name>.deck.repaired.json` 修复之后、**真正被渲染成这些预览图的**那份
        # 修复回环会重写十几页文案，两份差异很大。按页修订必须打在 deck_live 上，
        # 否则用户会看到「我只改了第 5 页标题，第 8、11 页的文案也一起变回去了」。
        # 早先 result 里只有 `deck`（指向修复前），是本次为修订功能才发现并补齐的。
        _finish(jid, result=dict(
            name=name, pptx=pptx, deck=deck_path, deck_live=deck_live, pages=pages,
            page_index=revise_mod.build_page_index(deck),
            summary=rep['summary'], slides=rep['slides'], log_dir=runlog.dir,
            download='/download/%s.pptx' % urllib.parse.quote(name)))
        _log(jid, '完成。', 'success',
             detail='%d 页，几何 %d error / %d warn'
                    % (len(deck.get('slides') or []), rep['summary']['error'],
                       rep['summary']['warn']))
    except Exception as e:
        traceback.print_exc()
        _log(jid, '失败：%s' % e, 'warning')
        _finish(jid, error='%s: %s' % (type(e).__name__, e))


def _revision_draft_path(stem: str, rid: str) -> str:
    return os.path.join(PLANS, '%s.revise-%s.json' % (stem, rid))


def _append_ledger(stem: str, rec: dict) -> tuple[int, str]:
    """记一笔「这份 deck 被人工改过」。→ (台账现有条数, 失败原因)。

    台账的用途是**事后可追溯**：重新规划前提示「会丢弃 N 处人工修改」，
    以及回答「这页标题是谁改的」。

    返回值不是装饰：**写失败必须能看见**。它原来只 print 到 stdout（服务器上
    没人看），而「台账静默没写进去」与「写进去了」在界面上完全一样。
    """
    path = _revisions_path(stem)
    try:
        data = pipeline.load_json(path) if os.path.isfile(path) else {}
        if not isinstance(data, dict):
            data = {}
    except (ValueError, OSError) as e:
        data = {}
        print('  [warn] 台账读不了（按空的续写）：%s' % e)
    data.setdefault('revisions', []).append(rec)
    try:
        pipeline.save_json(data, path)
    except Exception as e:
        return len(data['revisions']) - 1, '写台账失败：%s' % e
    return len(data['revisions']), ''


def _ledger_changes(stem: str) -> dict:
    """这份 deck 有过几笔人工修改、涉及哪些页。→ `{'count': n, 'pages': [...]}`。

    用途是「重新规划前提醒」：`/api/generate` 是从大纲重做整份 deck，
    磁盘上的按页修改不会参与 —— 不提醒的话用户会在事后才发现改动没了。
    """
    try:
        data = pipeline.load_json(_revisions_path(stem))
    except (ValueError, OSError):
        return dict(count=0, pages=[])
    revs = data.get('revisions') if isinstance(data, dict) else None
    if not isinstance(revs, list):
        return dict(count=0, pages=[])
    pages = sorted({int(pg) for r in revs if isinstance(r, dict)
                    for pg in (r.get('changed') or [])
                    if isinstance(pg, int)})
    return dict(count=len(revs), pages=pages)


def _lazy_json(path: str):
    """按需读一份 json（读到的结果缓存住）。

    用来延迟加载规划产物：正文页重做要用 `outline` / `parsed`（`redo_slide` 按
    `slides[]` 的下标回原文取条目），而封面/目录的重做只看 deck 本身 —— 这两份文件
    缺失或改名时，「只改封面」不该跟着一起失败。
    """
    box: list = []

    def get():
        if not box:
            box.append(pipeline.load_json(path))
        return box[0]

    return get


def run_revise(jid: str, deck_arg: str, mode: str, text: str):
    """阶段一：分诊 + 出方案。**不落盘**（除了草稿，供刷新页面后取回）。"""
    try:
        cfg = cfg_mod.llm_config()
        if cfg is None:
            raise RuntimeError('修订需要文本模型（配 PPTGEN_LLM_*）')
        stem = _stem_of(deck_arg)
        path, deck = _resolve_deck(stem)
        if deck is None:
            # 前端可能给的是绝对路径（它从上一轮 result 里拿到的）
            cand = _safe_join(PLANS, os.path.basename(deck_arg or ''))
            if cand and os.path.isfile(cand):
                path, deck = cand, pipeline.load_json(cand)
        if deck is None:
            raise RuntimeError('找不到这份 deck：%s' % deck_arg)
        stem = _stem_of(path)

        bad = revise_mod.check_deck_layouts(deck)
        if bad:
            raise RuntimeError(bad)

        index = revise_mod.build_page_index(deck)
        _log(jid, '分诊中…（第 1 次模型调用）', 'active')
        triage = revise_mod.split_requests(text, index, cfg,
                                           log=lambda m: _log(jid, m))
        for u in triage['unclear']:
            _log(jid, '待澄清：%s' % u, 'warning')
        if not triage['items']:
            raise RuntimeError('没有解析出任何可执行的修改要求'
                               + ('（%s）' % triage['unclear'][0] if triage['unclear'] else ''))

        by_preview = {e['preview']: e for e in index}
        if mode == 'rewrite':
            outline = pipeline.load_json(
                os.path.join(PLANS, stem + '.outline.json'))
            # `.parsed.json` 只有**正文页**重做要用（`redo_slide` 按锚点回原文取
            # 条目）；封面/目录的重做只看大纲。延迟加载：那份文件缺失或改名时，
            # 「只改封面」这种请求不该跟着一起失败。
            doc = _lazy_json(os.path.join(PLANS, stem + '.parsed.json'))

            items = []
            cap = revise_mod.max_rewrite_pages()
            rewrote = 0
            for n, it in enumerate(triage['items'], 1):
                page = by_preview.get(it['preview']) or {}
                if page.get('kind') == 'back':
                    # 只剩封底要拦：它是模板的品牌收尾页，没有可改的内容
                    # （`revise.page_spec` 对它返回空 dict）。
                    items.append(dict(it, status='reject', new=None,
                                      reason='%s —— 封底是品牌收尾页，'
                                             '没有可改的内容' % page.get('label')))
                    continue
                if page.get('layout') == revise_mod.DIVIDER_LAYOUT:
                    items.append(dict(it, status='reject', new=None,
                                      reason='章节分隔页是结构页，没有版式可换 —— '
                                             '只能微调它的章节名/导语'))
                    continue
                if rewrote >= cap:
                    # 截断必须说出来。`llm` 没有取消接口，一页一次调用最坏 9 分钟，
                    # 一次做十几页要等一小时且停不下来 —— 所以设上限，
                    # 但**不能悄悄少做几页**：那会变成「提了意见没反应」。
                    items.append(dict(it, status='reject', new=None,
                                      reason='一次最多重做 %d 页（PPTGEN_REVISE_MAX_PAGES）'
                                             '—— 这一页本次没做，请再提交一次'
                                             % cap))
                    continue
                rewrote += 1
                _log(jid, '正在重做第 %d 页…（第 %d/%d 条）'
                     % (it['preview'], n + 1, len(triage['items']) + 1), 'active')
                if page.get('kind') in ('cover', 'toc'):
                    # 模板页没有版式可换，「重做」= 重出这一页的文案，仍写在
                    # 模板自己的那一页上（`revise.propose_template_rewrite`）。
                    items.append(dict(it, **revise_mod.propose_template_rewrite(
                        deck, it['preview'], outline, it['request'], cfg,
                        log=lambda m: _log(jid, m))))
                    continue
                new, why = revise_mod.redo_slide(deck, it['preview'], outline, doc(),
                                                it['request'], cfg,
                                                log=lambda m: _log(jid, m))
                if new is None:
                    items.append(dict(it, status='reject', new=None, reason=why))
                else:
                    items.append(dict(it, status='warn' if why else 'ok', new=new,
                                      reason=why if why else '',
                                      was_layout=page.get('layout'),
                                      now_layout=new.get('layout')))
        else:
            _log(jid, '生成修改方案…（第 2 次模型调用）', 'active')
            items = revise_mod.propose_patches(deck, triage['items'], cfg,
                                               log=lambda m: _log(jid, m))

        rid = uuid.uuid4().hex[:8]
        result = dict(rid=rid, stem=stem, deck=path, mode=mode,
                      sha=revise_mod.deck_fingerprint(deck),
                      items=items, unclear=triage['unclear'], page_index=index)
        try:
            pipeline.save_json(result, _revision_draft_path(stem, rid))
        except Exception as e:
            _log(jid, '草稿落盘失败（不影响本次）：%s' % e, 'warning')
        ok = [i for i in items if i.get('status') != 'reject']
        _finish(jid, result=result)
        _log(jid, '方案就绪：%d 条可应用、%d 条不能应用'
             % (len(ok), len(items) - len(ok)),
             'success' if ok else 'warning')
    except Exception as e:
        traceback.print_exc()
        _log(jid, '失败：%s' % e, 'warning')
        _finish(jid, error='%s: %s' % (type(e).__name__, e))


def run_apply(jid: str, deck_arg: str, mode: str, items: list, sha: str = '',
              rid: str = ''):
    """阶段二：应用选中的条目 → 重建 → **只重渲改动的页**。

    沿用 `/api/outline` → `/api/generate` 那套 stateless 双阶段的惯例：
    前端把（可能被用户手改过新值的）方案原样回传，服务端**不重调模型**。
    """
    try:
        cfg = cfg_mod.llm_config()
        stem = _stem_of(deck_arg)
        path, deck = _resolve_deck(stem)
        if deck is None:
            raise RuntimeError('找不到这份 deck：%s' % deck_arg)
        stem = _stem_of(path)

        # 接上原来那条流程的日志文件夹（照 run_generate 的做法）。修订的成品会
        # 以 `-2` 命名归档进去 —— `runlog._copy_in` 同名不覆盖，于是磁盘上
        # **会**留下多个版本；`run.json` 里的 `artifacts.pptx` 是单槽位，
        # 读不出「哪份是哪次修订的」，所以另外记一笔台账（`_append_ledger`）。
        runlog = runlog_mod.RunLog(stem, origin='web', command='revise')
        side = _read_sidecar(os.path.join(PLANS, stem + '.parsed.json')) or {}
        if side.get('run_dir') and not runlog.open_at(side['run_dir']):
            runlog.put('warning', '接不上原流程文件夹，已新建：%s' % side['run_dir'])
        with JOBS_LOCK:
            if jid in JOBS:
                JOBS[jid]['runlog'] = runlog
        runlog.put('deck_name', stem)
        if not runlog._data.get('config'):
            runlog.put('config', runlog_mod.config_snapshot())
        _log(jid, '本次流程日志：%s' % runlog.dir, 'info')

        # 乐观并发：提案之后 deck 被别的路径改过（又生成了一次），
        # 就直接拒 —— 否则会把用户刚看到的那份方案套到另一份内容上
        now = revise_mod.deck_fingerprint(deck)
        if sha and sha != now:
            raise RuntimeError('这份 deck 在生成方案之后被改过（可能又生成了一次），'
                               '请重新生成修改方案')

        bad = revise_mod.check_deck_layouts(deck)
        if bad:
            raise RuntimeError(bad)

        _log(jid, '应用 %d 条修改…' % len(items))
        outline_patch: list = []
        if mode == 'rewrite':
            # 这两份只有**正文页**重做要用（`redo_slide` 按 `slides[]` 的下标回原文
            # 取条目）；封面/目录的重做只看 deck 本身。延迟加载：它们缺失或改名时，
            # 「只改封面」这种请求不该跟着一起失败。
            get_outline = _lazy_json(os.path.join(PLANS, stem + '.outline.json'))
            doc = _lazy_json(os.path.join(PLANS, stem + '.parsed.json'))

            out_deck = copy.deepcopy(deck)
            changed = []
            # **按顺序增量应用**：先把第 5 页写回，再算第 6 页的禁用版式 ——
            # 否则「第 5、6 页都重做」会同时选到同一个版式（相邻重复）
            for it in items:
                preview = it.get('preview')
                if not isinstance(preview, int):
                    _log(jid, '有一条没有页码，跳过', 'warning')
                    continue
                new = it.get('new')
                if preview not in (1, 2) and not (isinstance(new, dict)
                                                  and new.get('layout')):
                    # 方案里没有成品（浏览器漏传、或用户把新值改坏了）→ 现算一版。
                    # **模板页不走这条**：封面/目录的内容不在 `slides[]` 里，
                    # `redo_slide` 只认正文页（见 `revise.commit_rewrite`）。
                    new, why = revise_mod.redo_slide(out_deck, preview, get_outline(),
                                                     doc(), it.get('request') or '',
                                                     cfg, log=lambda m: _log(jid, m))
                    if new is None:
                        _log(jid, '第 %s 页重做失败：%s' % (preview, why), 'warning')
                        continue
                # **用用户确认过的那一版，不要重跑模型。** 重跑会得到另一个结果 ——
                # 用户在面板上看着 comparison_rows 点了「应用」，拿到的却是别的版式；
                # 而且要为此多花一次模型调用。（`new`/`ops` 来自浏览器，仍要校验。）
                # 分派收在 `commit_rewrite` 里：封面/目录写 deck 顶层并带回大纲补丁，
                # 正文页整页替换 —— 这里**不能**自己写 `slides[preview-3]`。
                got = revise_mod.commit_rewrite(out_deck, preview, new, it.get('ops'))
                if got['reason']:
                    _log(jid, '第 %s 页未应用：%s' % (preview, got['reason']),
                         'warning')
                    continue
                outline_patch.extend(got['outline'])
                if got['changed']:
                    changed.append(preview)
                else:
                    _log(jid, '第 %d 页与原来一样，没有变化' % preview, 'info')
        else:
            out_deck, rep = revise_mod.apply_revision(deck, items,
                                                      log=lambda m: _log(jid, m))
            for r in rep['items']:
                if r['status'] == 'reject':
                    _log(jid, '第 %s 页未应用：%s' % (r['preview'], r['reason']),
                         'warning')
            changed = [r['preview'] for r in rep['items'] if r['status'] != 'reject']
            outline_patch = rep.get('outline') or []
            if outline_patch:
                _log(jid, '这些字段会同步回大纲：%s'
                     % '、'.join(p['field'] for p in outline_patch))

        if not changed:
            raise RuntimeError('没有一条可以应用')

        pptx = os.path.join(SAMPLES, '%s.pptx' % stem)
        template = cfg_mod.template_path()

        def build_qa(spec):
            build_mod.build(spec, template, pptx, fill_toc=True,
                            on_log=lambda m: _log(jid, m))
            return geometry.analyse(pptx)

        # 预演构建：只为看**改动的页**有没有撑破版面。build + 几何约 1–2 秒，
        # 贵的是渲染（8–10 秒/页），所以每次都能跑。
        _log(jid, '预演构建（检查改动的页有没有撑破版面）…', 'active')
        issues = revise_mod.check_by_build(out_deck, build_qa, set(changed))
        for pg, why in sorted(issues.items()):
            if pg < revise_mod.PAGE_OFFSET:
                # 封面/目录在几何检查的 skip 名单里，正常报不出来；真报出来也不能按
                # `slides[pg-3]` 去取 —— 那是负下标。模板页的文案长度由
                # `check_template_new` 与 `_toc_line` 守，这里只把话说出来。
                _log(jid, '第 %d 页（模板页）有版面问题，这里不处理：%s'
                     % (pg, '；'.join(why)), 'warning')
                continue
            # 溢出是**可恢复**的，交给现成的 `repair_slide`（它冻结 layout、
            # 只压短），不要跑 `repair_deck` —— 那会挑**所有**溢出页、
            # 把用户没让改的页一起重写。
            _log(jid, '第 %d 页有版面问题，压一下文案：%s' % (pg, '；'.join(why)),
                 'warning')
            try:
                fixed = repair_mod.repair_slide(
                    out_deck['slides'][pg - revise_mod.PAGE_OFFSET], why, cfg,
                    log=lambda m: _log(jid, m))
                out_deck['slides'][pg - revise_mod.PAGE_OFFSET] = fixed
            except Exception as e:
                _log(jid, '第 %d 页压文案失败：%s' % (pg, e), 'warning')

        spec_out = os.path.join(PLANS, '%s.deck.revised.json' % stem)
        pipeline.save_json(out_deck, spec_out)
        build_mod.build(out_deck, template, pptx, fill_toc=True,
                        on_log=lambda m: _log(jid, m))
        rep_final = geometry.analyse(pptx)

        with runlog.stage('render') as st:
            work = os.path.join(VISUAL, stem)
            _log(jid, '重渲改动的 %d 页预览…' % len(changed), 'active')
            shot = _render(pptx, work, out_deck, snap_tag=rid or 'rev',
                           deck_path=spec_out)
            st.update(pages=len(shot['pages']), rendered=len(shot['rendered']))
        _log(jid, '预览图 %d 张（本次重渲 %d 张）'
             % (len(shot['pages']), len(shot['rendered'])), 'success')
        # 没渲上的页必须说出来：那些页的图现在与 deck 不一致，而界面、几何检查
        # 都看不出来。`_render` 已保证它们不会被记成「最新」（下次会重试）。
        missing = sorted(set(changed) - set(shot['rendered']))
        if missing:
            _log(jid, '有 %d 页没能重渲（%s）—— 页面上这几张仍是旧图，'
                      '检查 officecli 是否可用' % (len(missing), missing), 'warning')
        if not shot.get('manifest_ok', True):
            _log(jid, '渲染清单没写进去 —— 下次会整份重渲一遍（约 %d 页）'
                 % len(shot['pages']), 'warning')

        # 大纲侧要同步：目录条目与封面标题都是**从大纲派生的**（pipeline.py:1529/1535），
        # 不同步的话用户下一次点「生成」会把刚改的标题静默冲掉。两种模式都会产生
        # 补丁：微调由 `apply_revision` 报出来，重做里封面/目录那几条由
        # `commit_rewrite` 收在上面（正文页重做没有大纲侧要同步的东西）。
        if outline_patch:
            opath = os.path.join(PLANS, stem + '.outline.json')
            try:
                cur = pipeline.load_json(opath)
                pipeline.save_json(
                    revise_mod.apply_outline_patch(cur, outline_patch), opath)
                _log(jid, '已同步回大纲：%s'
                     % '、'.join(p['field'] for p in outline_patch))
            except Exception as e:
                _log(jid, '同步回大纲失败（%s）—— 下次重新生成会丢掉这几处改动'
                     % e, 'warning')

        n_led, ledger_why = _append_ledger(stem, dict(rid=rid, mode=mode,
                                                      changed=changed,
                                                      deck_sha=now, spec=spec_out,
                                                      at=time.strftime('%Y-%m-%dT%H:%M:%S')))
        if ledger_why:
            _log(jid, '台账没写进去（%s）—— 这份 deck 的人工修改将无法追溯' % ledger_why,
                 'warning')
        else:
            _log(jid, '修订台账已有 %d 笔' % n_led)
        runlog.note('revise', mode=mode, changed=sorted(changed),
                    rendered=len(shot['rendered']), spec=spec_out,
                    error=rep_final['summary']['error'],
                    warn=rep_final['summary']['warn'])
        runlog.attach_pptx(pptx)
        # 「改前」快照的文件名 —— `_render` 在覆盖旧图之前留的底，给前端做
        # 「改动前 / 改动后」对比用。放在同一个目录里，所以 `/preview/` 端点
        # 零改动就能取到（它走 `_safe_join(VISUAL, ...)`）。
        tagname = rid or 'rev'
        before = {str(pg): 'page-%02d-%s.png' % (pg, tagname)
                  for pg in shot['rendered']
                  if os.path.isfile(os.path.join(work, 'page-%02d-%s.png' % (pg, tagname)))}

        _finish(jid, result=dict(
            name=stem, pptx=pptx, deck=spec_out, deck_live=spec_out,
            pages=shot['pages'], rendered=shot['rendered'], changed=sorted(changed),
            before=before,
            # 前端必须把它合并进 `state.outline` —— `/api/generate` 的输入是
            # **页面上的那份大纲**，不是磁盘上的。只写盘的话，用户不刷新页面
            # 直接点「生成 PPT」，刚改的封面标题/目录条目会被覆盖回去。
            outline_patch=outline_patch,
            page_index=revise_mod.build_page_index(out_deck),
            summary=rep_final['summary'], slides=rep_final['slides'],
            download='/download/%s.pptx' % urllib.parse.quote(stem)))
    except Exception as e:
        traceback.print_exc()
        _log(jid, '失败：%s' % e, 'warning')
        _finish(jid, error='%s: %s' % (type(e).__name__, e))


def _write_confirmed_outline(outline: dict, name: str) -> str:
    """落盘「人工确认后」的大纲，返回路径。

    原本这段在主线程里带 `try/except: pass` 吞掉失败 —— 结果是「落盘的那份」
    和「实际拿去规划的这份」可能不一致，且没人知道。现在失败会走日志。
    """
    path = os.path.join(PLANS, '%s.outline.json' % name)
    pipeline.save_json(outline, path)
    return path


def _deck_candidates(stem: str) -> list[str]:
    """这份 deck 可能落在哪几个文件上。

    修订产物排最前：它是**最近一次按页修订的结果**，也是渲染清单里记的那份。
    不列进来的话，一旦渲染清单丢了（`out/visual` 被清过），`_resolve_deck`
    就会退回原始 deck —— 用户刚改的那些内容会**静默消失**。
    """
    return [os.path.join(PLANS, stem + '.deck.revised.json'),
            os.path.join(PLANS, stem + '.deck.repaired.json'),
            os.path.join(PLANS, stem + '.deck.json')]


def _rendered_deck(stem: str) -> str | None:
    """渲染清单里记的那份 deck 路径 —— 这些预览图**就是从它渲出来的**。

    这是「哪份 deck 是 live」的**权威**来源：磁盘上可能同时躺着
    `deck.json` 与 `deck.repaired.json`，而较新的那份未必是渲染过的那份
    （实测 `Dify_介绍与实战` 的两份差了 50 分钟，较新那份从未被渲染）。
    """
    try:
        with open(os.path.join(VISUAL, stem, MANIFEST), 'r', encoding='utf-8') as f:
            got = json.load(f)
    except (OSError, ValueError):
        return None
    p = got.get('deck') if isinstance(got, dict) else None
    return p if isinstance(p, str) and p and os.path.isfile(p) else None


def _resolve_deck(stem: str) -> tuple[str | None, dict | None]:
    """`<name>` → 实际该改的那份 deck：(路径, 内容)；都没有则 (None, None)。

    优先渲染清单里记的那份（`_rendered_deck`）—— 那才是屏幕上这些图的来源，
    修订打歪的后果是「我只改了第 5 页标题，第 8、11 页的文案也一起变回去了」。

    拿不到清单时退回「两份里 mtime 较新的那份」。这只是**兜底**：`run_generate`
    的顺序是先存 plain、再跑修复、存 repaired，所以正常跑完时 repaired 天然更新；
    但若有一次规划跑完、修复阶段没跑成（`run.py plan` 单独跑、或修复中途崩了），
    plain 就会更新 —— 而它从未被渲染过，按它修订会让没改动的页图文不符。
    这也是前端要把 `deck_live` 回传的原因：那条路才是确定的。
    """
    recorded = _rendered_deck(stem)
    good = [p for p in _deck_candidates(stem) if os.path.isfile(p)]
    if recorded and recorded not in good:
        good.insert(0, recorded)
    if not good:
        return None, None
    path = recorded or (max(good, key=lambda p: os.stat(p).st_mtime_ns)
                        if len(good) > 1 else good[0])
    try:
        return path, pipeline.load_json(path)
    except (ValueError, OSError) as e:
        print('  [warn] 读不了 deck %s：%s' % (path, e))
        return None, None


def _revisions_path(stem: str) -> str:
    return os.path.join(PLANS, stem + '.revisions.json')


def _officecli_ok() -> bool:
    from pptgen.qa import visual
    return bool(visual.find_officecli())


def _preview_sig() -> str:
    """预览渲染的「配方」指纹，进 `.pptx-stamp` 用。

    目前只有分辨率。改了它，所有已渲染的图都会失效重渲一次。
    """
    w, h = cfg_mod.preview_size()
    return '%dx%d' % (w, h)


# ── 按页渲染指纹 ────────────────────────────────────────────────
# 这一整块只为一件事：**改一页只该重渲那一页**。
#
# 早先的指纹是**整目录级**的（`.pptx-stamp` 里一行 `mtime:size:分辨率`）：pptx 一变
# 就把所有 `page-*.png` 删掉重来。而按页修订每次都要 `build.build()` 全量重建
# pptx（build.py:179-229 没有增量能力），于是「改一页」= 重渲 18 页 ≈ 3 分钟
# （实测每页 8–10 秒，`config.py:142` 的注释也记着这个数）。
#
# 现在按页算，而且算的是**这一页的像素由什么决定**（`_page_payload`），不再是
# pptx 文件的时间戳 —— 后者回答的是「整份 deck 有没有动」，前者才是
# 「这一页要不要重画」。两者在「只改一页」时差 1 页 vs 18 页。
#
# 当年那次事故（11:25 跑完，页面上 17 张预览全是 09:23 那一版的 15 页内容，
# 用户按预览里的页码反馈问题、指的根本不是这一版）的教训在这里以另一种形式
# 保留：**页数变少时多出来的旧图必须删**（见 `_render_plan` 的 `drop`），
# 否则页面上会多出几张上一版的图。
_PAGE_PNG_RE = re.compile(r'^page-(\d\d)\.png$')
MANIFEST = '.pages.json'


def _page_payload(spec: dict, pg: int, n: int):
    """这一页渲染出来的像素由什么决定。

        pg == 1        封面：标题 + **生效后的**副标题
        pg == 2        目录：toc
        3 <= pg < n    正文/章节分隔页：`spec['slides'][pg-3]`
        pg == n        封底：模板原样，只有「它是封底」这一件事
    """
    if pg == 1:
        # 副标题必须用**生效后**的值：deck.json 里通常没有 `subtitle`，
        # `build.fill_cover` 会兜底成 `today_cn()`（build.py:212）。只哈希
        # `spec.get('subtitle')` 的话，兜底值变了而指纹没变 —— 与当年
        # 「改了分辨率旧图不重渲」是同一类静默失效。
        # （`today_cn()` 是**月份**粒度，所以这个兜底值跨月才会变。）
        return {'cover': spec.get('title') or '',
                'subtitle': spec.get('subtitle') or build_mod.today_cn()}
    if pg == 2:
        return {'toc': spec.get('toc') or []}
    if pg == n:
        return {'back': True}
    slides = spec.get('slides') or []
    i = pg - revise_mod.PAGE_OFFSET
    if not (0 <= i < len(slides)):
        return {'missing': pg}
    # `page` 必须剔掉：`build.build()` 会**原地**往每个 slide 写 `sl['page'] = i+3`
    # （build.py:194），于是「跑过一次 build 的内存 deck」有它、刚 load 进来的
    # 盘上那份也可能有或没有。它是派生值、不是内容，进了指纹就会让同内容的两份
    # spec 算出不同指纹，白白整目录重渲一遍。
    return {k: v for k, v in slides[i].items()
            if k != 'page' and not k.startswith('_')}


def _page_digest(spec: dict, pg: int, n: int, sig: str) -> str:
    blob = json.dumps(_page_payload(spec, pg, n), ensure_ascii=False,
                      sort_keys=True, separators=(',', ':'))
    # 分辨率（`sig`）必须进指纹：pptx 没动、我们改了 `PPTGEN_PREVIEW_WIDTH` 时，
    # 只看内容会认为「没变化」而留着旧的 1280 图 —— 页面上看不出区别，只是糊一点，
    # 于是「提高了清晰度」这件事悄悄没生效。`tests/test_runlog.py:291` 钉的就是这条。
    return hashlib.sha1(('%s|%s' % (blob, sig)).encode('utf-8')).hexdigest()


def _load_manifest(work: str) -> dict:
    try:
        with open(os.path.join(work, MANIFEST), 'r', encoding='utf-8') as f:
            got = json.load(f)
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}      # 没有 / 坏了 / 不是对象 → 当「全部过期」，自愈


def _save_manifest(work: str, m: dict) -> bool:
    """原子写：崩在中途也不能留下半份清单（那会让下次白渲一整遍）。

    返回是否写成功。**失败必须能看见** —— 写不进去的后果是下一次、再下一次
    都整份重渲（实测一次 18 页 ≈ 220 秒），而日志上只表现为「怎么又渲了 18 页」，
    完全看不出原因。
    """
    tmp = os.path.join(work, MANIFEST + '.tmp')
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(m, f, ensure_ascii=False, indent=1)
        os.replace(tmp, os.path.join(work, MANIFEST))
        return True
    except OSError as e:
        print('  [warn] 写渲染清单失败：%s' % e)
        return False


def _render_plan(work: str, digests: dict[int, str], manifest: dict, *,
                 sig: str = '', template_sha: str = '') -> dict:
    """谁该渲、谁该删、谁保留 —— **纯函数**（只看目录列表，不调 officecli）。

    做成纯函数是为了让「谁该重渲」能在单测里全覆盖：渲染每页要 8–10 秒，
    而这段判断正是增量渲染的正确性所在，不能只靠肉眼验收。

    → {'render': [pg...], 'drop': [文件名...], 'keep': [文件名...],
       'stale_contact_sheet': bool, 'full_reason': str}
    """
    old = manifest.get('pages') if isinstance(manifest.get('pages'), dict) else {}
    full_reason = ''
    if not old:
        full_reason = '没有渲染清单（首次渲染，或清单没写进去）'
    elif manifest.get('sig') != sig:
        full_reason = '渲染配方变了（%s → %s）' % (manifest.get('sig'), sig)
    elif manifest.get('template_sha') != template_sha:
        full_reason = '模板换过了'
    if full_reason:
        old = {}
    try:
        have = {int(m.group(1)): m.group(0)
                for m in (_PAGE_PNG_RE.match(f) for f in os.listdir(work)) if m}
    except OSError:
        have = {}

    render: list[int] = []
    keep: list[str] = []
    for pg, dg in sorted(digests.items()):
        if pg in have and old.get(str(pg)) == dg:
            keep.append('page-%02d.png' % pg)
        else:
            render.append(pg)
    # digests 里没有的页码 = 这一版页数变少了。**旧图必须删**，否则页面上会多出
    # 几张上一版的图，用户按预览页码反馈时又指错页。
    drop = [f for pg, f in sorted(have.items()) if pg not in digests]
    return dict(render=render, drop=drop, keep=keep,
                stale_contact_sheet=bool(render or drop),
                full_reason=full_reason)


def _render(pptx: str, work: str, spec: dict, *, snap_tag: str = '',
            deck_path: str = '') -> dict:
    """渲染预览图。→ {'pages': [...], 'rendered': [pg...], 'dropped': [...]}。

    按页指纹决定该重渲哪些页；没变的页**连碰都不碰**（`page-NN.png` 的 mtime 不变）。

    `snap_tag` 非空时，被重渲的页在渲染**之前**先把旧图复制成
    `page-NN-<snap_tag>.png` —— 「改前/改后」对比要用它。快照就放同一个目录：
    `/preview/` 走 `_safe_join(VISUAL, rest)`（server.py 的路由），取它零改动。
    """
    from pptx import Presentation
    from pptgen.qa import visual
    os.makedirs(work, exist_ok=True)
    n = len(Presentation(pptx).slides)
    sig = _preview_sig()
    digests = {pg: _page_digest(spec, pg, n, sig) for pg in range(1, n + 1)}
    plan = _render_plan(work, digests, _load_manifest(work), sig=sig,
                        template_sha=runlog_mod.sha256(cfg_mod.template_path()) or '')

    if not visual.find_officecli():
        return dict(pages=[], rendered=[], dropped=[])

    w, h = cfg_mod.preview_size()
    if plan['full_reason']:
        # 为什么整份要重渲，必须说出来 —— 否则日志上只有「怎么又渲了 18 页」
        print('  [render] 整份重渲：%s' % plan['full_reason'])
    rendered: list[int] = []
    for pg in plan['render']:
        fname = 'page-%02d.png' % pg
        out = os.path.join(work, fname)
        if snap_tag and os.path.isfile(out):
            # 先留底再覆盖：指纹一变，旧图就再也拿不回来了
            try:
                shutil.copyfile(out, os.path.join(
                    work, 'page-%02d-%s.png' % (pg, snap_tag)))
            except OSError:
                pass
        visual.render_pages(pptx, work, [pg], native=True, width=w, height=h)
        if os.path.isfile(out):
            rendered.append(pg)
    for f in plan['drop']:
        try:
            os.remove(os.path.join(work, f))
        except OSError:
            pass

    # 联系表要整份都渲过才可信。任何一页变了就先让它失效，别留一张跟当前
    # deck 不一致的联系表 —— 那正是「预览看着成功，其实是旧的」那一类错误。
    sheet = os.path.join(work, 'contact-sheet.png')
    if plan['stale_contact_sheet']:
        try:
            os.remove(sheet)
        except OSError:
            pass
    if not os.path.isfile(sheet) and 1 in rendered:
        visual.render_contact_sheet(pptx, sheet)

    # 清单只记**确实是最新**的那些页：
    #   · 本次渲成功了的（`fresh`）
    #   · 本次压根不需要渲的（digest 本来就匹配）
    # **渲失败的那几页绝不能记** —— 记了就等于声称「这页的图是最新的」，
    # 它再也不会被重渲，用户会一直看着旧图，而几何检查、日志全都正常。
    # 这与 `.pptx-stamp` 时代那次「预览看着成功，其实是旧的」是同一类错误。
    fresh = set(rendered)
    attempted = set(plan['render'])
    pages_map: dict[str, str] = {}
    for pg, dg in digests.items():
        if not os.path.isfile(os.path.join(work, 'page-%02d.png' % pg)):
            continue
        if pg in fresh or pg not in attempted:
            pages_map[str(pg)] = dg
    # 清单里的 `deck` 回答「这些图到底是哪份 spec 渲的」—— `_resolve_deck` 用它
    # 兜底：磁盘上可能同时躺着 deck.json / deck.repaired.json / deck.revised.json，
    # 较新的那份未必是渲染过的那份（实测有差 50 分钟的）。
    man_ok = _save_manifest(work, dict(
        sig=sig, template_sha=runlog_mod.sha256(cfg_mod.template_path()) or '',
        n=n, deck=deck_path or '', pages=pages_map))
    pages = ['page-%02d.png' % pg for pg in sorted(digests)
             if os.path.isfile(os.path.join(work, 'page-%02d.png' % pg))]
    return dict(pages=pages, rendered=rendered, dropped=plan['drop'],
                full_reason=plan['full_reason'], manifest_ok=man_ok)


def run_outline(jid: str, src: str, data: bytes | None = None,
                name: str | None = None, source_origin: str = ROOT_DIR,
                original_name: str | None = None):
    """解析 → 大纲。

    上传的文件优先用内存里的字节解析（`data` 不为 None 时），落盘那份只是
    持久化副本；这两个来源互为兜底，谁在都能跑。
    """
    try:
        name = name or os.path.basename(src)
        stem = os.path.splitext(name)[0][:40]
        # 日志文件夹在**这一刻**就建、源文件同时归档 —— 不能等生成阶段：
        # 前端「新建」会清空 out/uploads/，而且来源的判定只有此刻是确定的。
        runlog = runlog_mod.RunLog(stem, origin='web', command='outline')
        with JOBS_LOCK:
            if jid in JOBS:
                JOBS[jid]['runlog'] = runlog
        runlog.put('config', runlog_mod.config_snapshot())
        runlog.attach_source(src, origin=source_origin, original_name=original_name)
        _log(jid, '本次流程日志：%s' % runlog.dir, 'info')

        with runlog.stage('parse') as st:
            _log(jid, '解析源文档：%s' % name, 'active')
            doc = (parse_mod.parse_bytes(data, name) if data is not None
                   else parse_mod.parse(src))
            stem = os.path.splitext(name)[0][:40]
            parsed_path = os.path.join(PLANS, stem + '.parsed.json')
            pipeline.save_json(doc, parsed_path)
            sk = doc.get('structure') or {}
            st.update(blocks=doc['stats']['blocks'], chars=doc['stats']['chars'])
            runlog.note('document', title=doc.get('title'), kind=doc.get('kind'),
                        blocks=doc['stats']['blocks'], chars=doc['stats']['chars'],
                        parsed_json=parsed_path,
                        structure={'method': sk.get('method'),
                                   'chapters': len(sk.get('chapters') or []),
                                   'chapter_names': [c['name'] for c in sk.get('chapters') or []]})
            _log(jid, '解析完成：%d 个内容块 / %d 字'
                 % (doc['stats']['blocks'], doc['stats']['chars']),
                 'success', detail=name)

        with runlog.stage('outline') as st:
            _log(jid, '生成大纲（调用模型）…', 'active')
            outline = pipeline.make_outline(doc, on_log=lambda m: _log(jid, m))
            outline_path = os.path.join(PLANS, stem + '.outline.json')
            pipeline.save_json(outline, outline_path)
            with open(os.path.splitext(outline_path)[0] + '.md', 'w', encoding='utf-8') as f:
                f.write(pipeline.outline_preview(outline) + '\n')
            om = outline.get('_meta') or {}
            st.update(pages=outline.get('page_count'))
            runlog.note('outline', generated_by=om.get('generated_by'),
                        model=om.get('model'),
                        sections=len(outline.get('sections') or []),
                        pages=outline.get('page_count'),
                        chapter_names=[s['name'] for s in outline.get('sections') or []],
                        warnings=om.get('warnings') or [])
            runlog.attach_file('outline_json', outline_path)
        _write_sidecar(stem, runlog, src, source_origin, name)
        meta = outline.get('_meta') or {}
        if meta.get('generated_by') == 'llm':
            _log(jid, '大纲完成：%d 页正文（%d 章）'
                 % (outline.get('page_count', 0), len(outline.get('sections') or [])),
                 'success')
        else:
            # 兜底产物照样是一份「看起来正常」的大纲，不打警告没人看得出来。
            _log(jid, '大纲完成，但**走的是确定性兜底**：%s'
                 % '；'.join(meta.get('warnings') or ['模型未产出']), 'warning')

        _finish(jid, result=dict(outline=outline, outline_path=outline_path,
                                 parsed_path=parsed_path, stem=stem, source=name,
                                 source_origin=source_origin,
                                 log_dir=runlog.dir,
                                 preview=pipeline.outline_preview(outline)))
    except Exception as e:
        traceback.print_exc()
        _log(jid, '失败：%s' % e, 'warning')
        _finish(jid, error='%s: %s' % (type(e).__name__, e))


# ── 流程日志的接续 ────────────────────────────────────────────
# 大纲与生成是两次独立的任务（两个 job、两个线程），但属于**同一次流程**，
# 日志要落在同一个文件夹里。用一个 sidecar 把接续点写在服务器自己的目录下：
#
#   out/plans/<stem>.run.json  →  {"run_dir": …, "source": {...}}
#
# 为什么不把 run_dir 塞进 outline 的 `_meta` 让前端回传：
#   ① `_meta` 只在大纲层存在，历史产物里根本没有（磁盘上就有三份没有的）；
#   ② 前端那个 JSON 文本域是**明确邀请用户编辑的**，删掉它只要一次按键；
#   ③ 那等于让浏览器持有「这次流程用哪个源」的权威，而它随时可以漂移；
#   ④ 客户端回传路径还得额外做一层安全校验，而 sidecar 是服务器自己写的。
def _sidecar_path(stem: str) -> str:
    return os.path.join(PLANS, stem + '.run.json')


def _write_sidecar(stem: str, runlog, src: str, origin: str, name: str):
    try:
        pipeline.save_json({'run_dir': runlog.dir, 'started_at':
                            runlog.started.isoformat(timespec='seconds'),
                            'source': {'path': src, 'name': name, 'origin': origin}},
                           _sidecar_path(stem))
    except Exception as e:
        # 写不了 sidecar 只影响「两次请求能不能并到一个文件夹」，不该中断流程
        print('  [warn] 写 sidecar 失败：%s' % e)


# 中间产物的后缀。`os.path.splitext` **只剥一层** —— `x.parsed.json` 会得到
# `x.parsed`，拿去拼 sidecar 就永远找不到文件（踩过）。
# `.revised` 与 `.revise-<rid>` 是按页修订的两份产物（落盘的修订稿、方案草稿），
# **必须一起列进来**：漏了它们的后果不是「找不到文件」那么显眼 ——
# `_stem_of('x.deck.revised.json')` 会返回 `x.deck.revised`，于是渲染目录、
# 台账、日志文件夹全部换成另一套名字（`out/visual/x.deck.revised/`、
# `x.deck.revised.revisions.json`），表现为「预览图整份重渲了、台账却像没写」。
_STAGE_SUFFIX = re.compile(r'\.(parsed|outline|deck|repaired|revised|revise-[0-9a-f]+)$')


def _stem_of(path: str) -> str:
    """`x.parsed.json` / `x.deck.repaired.json` → `x`。

    要**反复剥**：后缀可能是叠加的（`x.deck.repaired.json`），
    只替换一次会留下 `x.deck`。
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    while True:
        stripped = _STAGE_SUFFIX.sub('', stem)
        if stripped == stem:
            return stem
        stem = stripped


def _read_sidecar(parsed_path: str) -> dict | None:
    path = _sidecar_path(_stem_of(parsed_path))
    if not os.path.isfile(path):
        return None
    try:
        return pipeline.load_json(path)
    except ValueError:
        return None


# ══════════════════════════════════════════════════════════════
# HTTP
# ══════════════════════════════════════════════════════════════
class Handler(BaseHTTPRequestHandler):
    server_version = 'pptgen'

    def log_message(self, fmt, *args):
        sys.stderr.write('  %s\n' % (fmt % args))

    # ── 工具 ──────────────────────────────────────────────────
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get('Content-Length') or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        # 浏览器一律发 UTF-8；但 Windows 下用 curl 从 cmd 里调试时，中文文件名
        # 会被按 GBK 编码。宽容解码一次，免得调试时踩坑。
        for enc in ('utf-8', 'gbk'):
            try:
                return json.loads(raw.decode(enc))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        raise ValueError('请求体既不是 UTF-8 也不是 GBK 的合法 JSON')

    def _file(self, path, ctype=None, download=False):
        if not path or not os.path.isfile(path):
            return self._json({'error': 'not found'}, 404)
        ctype = ctype or mimetypes.guess_type(path)[0] or 'application/octet-stream'
        with open(path, 'rb') as f:
            data = f.read()
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        if download:
            # `http.server` 用 latin-1 编码**整条** header（`send_header` 里
            # `.encode('latin-1', 'strict')`），而中文文件名不是 latin-1 —— 直接写
            # `filename="中文.pptx"` 会抛 UnicodeEncodeError。它是 `send_response`
            # 之后抛的，此时响应头还没 flush，于是连接被直接掐断，浏览器拿到
            # ERR_EMPTY_RESPONSE / 报错页（实测点「下载」必现）。
            # 按 RFC 5987 给 `filename*`（百分号编码，纯 ASCII），再留一个 ASCII
            # 兜底给老客户端。
            fn = os.path.basename(path)
            stem, ext = os.path.splitext(fn)
            ascii_fn = (stem.encode('ascii', 'ignore').decode('ascii').strip() or 'download')
            if ext.isascii():
                ascii_fn += ext
            self.send_header('Content-Disposition',
                             "attachment; filename=\"%s\"; filename*=UTF-8''%s"
                             % (ascii_fn, urllib.parse.quote(fn, safe='')))
        self.end_headers()
        self.wfile.write(data)

    def _read_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.rfile.read(min(1 << 20, n - len(buf)))
            if not chunk:
                break
            buf += chunk
        return bytes(buf)

    def _upload(self, u):
        """接一个原始二进制的上传。

        用裸 body 而不是 multipart：本服务的定位是「只用标准库」，自己解析
        boundary / part 头 / 二进制切分要五十来行且容易出错。浏览器侧就是
        `fetch('/api/upload?name=' + encodeURIComponent(file.name),
                {method:'POST', body: file})`；curl 侧是 `--data-binary @文件`。
        """
        # 原始文件名（消毒之前）只留给日志用 —— 界面和路径一律用消毒后的名字
        raw_name = (urllib.parse.parse_qs(u.query).get('name') or [''])[0]
        original = os.path.basename(urllib.parse.unquote(raw_name).replace('\\', '/'))
        try:
            name = _sanitize_name(raw_name)
        except ValueError as e:
            return self._json({'error': str(e)}, 400)

        n = int(self.headers.get('Content-Length') or 0)
        if n <= 0:
            return self._json({'error': '空请求体'}, 400)
        if n > MAX_UPLOAD:
            return self._json({'error': '文件太大：%.1f MB，上限 %d MB'
                               % (n / 1048576.0, MAX_UPLOAD // 1048576)}, 413)
        data = self._read_exact(n)
        if len(data) != n:
            return self._json({'error': '请求体不完整：收到 %d / %d 字节'
                               % (len(data), n)}, 400)

        # 早失败：本机 DLP 加密的文件，读它的一方（浏览器/curl）拿到的可能是密文。
        # 与其收下来、等到解析时才炸，不如当场说清楚 —— 顺带避免在
        # out/uploads/ 里堆一堆解不开的文件。
        if parse_mod.is_dlp(data):
            return self._json(
                {'error': '「%s」读出来是 DLP 密文，无法解析。%s'
                          % (name, parse_mod.DLP_HINT)}, 400)

        os.makedirs(UPLOADS, exist_ok=True)
        path = _unique_path(name)
        with open(path, 'wb') as f:
            f.write(data)

        # 落盘那份的同时也留一份在内存：解析优先用内存，磁盘负责持久化。
        # 两条路互为兜底（内存被挤掉/重启后，退回磁盘照样能解析）。
        uid = uuid.uuid4().hex[:12]
        with UPLOAD_LOCK:
            # `original_name` 是 `_sanitize_name` **之前**的原始文件名，只用于写进
            # 流程日志的 run.json。它绝不能当路径分量 —— 消毒函数存在的全部理由
            # 就是不信任它。要拿它只能在这一刻，过了这里就没了。
            UPLOAD_MEM[uid] = dict(id=uid, name=os.path.basename(path), path=path,
                                   size=len(data), data=data, mtime=time.time(),
                                   original_name=original)
            _evict_mem()
        return self._json(dict(id=uid, name=os.path.basename(path), size=len(data)))

    def _upload_shot(self, u):
        """接一张**版式截图**（原始二进制 body），存下并起一个识别任务。

        为什么不复用 `/api/upload`：那个入口按 `SOURCE_EXT` 白名单收敛扩展名
        （.pptx/.docx/.pdf/…），图片一律被拒 —— 它是「源文档」的入口。
        截图是另一类输入，单独一个入口比放宽白名单清楚。
        """
        raw = (urllib.parse.parse_qs(u.query).get('name') or [''])[0]
        base = os.path.basename(urllib.parse.unquote(raw).replace('\\', '/'))
        ext = os.path.splitext(base)[1].lower()
        if ext not in SHOT_EXT:
            return self._json({'error': '请上传图片（%s）' % '、'.join(SHOT_EXT)}, 400)
        # 只留安全字符：文件名会当路径分量，也可能进日志
        base = re.sub(r'[^\w.\-]+', '_', base)[:60] or ('shot' + ext)

        n = int(self.headers.get('Content-Length') or 0)
        if n <= 0:
            return self._json({'error': '空请求体'}, 400)
        if n > MAX_UPLOAD:
            return self._json({'error': '图片太大：%.1f MB，上限 %d MB'
                               % (n / 1048576.0, MAX_UPLOAD // 1048576)}, 413)
        data = self._read_exact(n)
        if len(data) != n:
            return self._json({'error': '请求体不完整'}, 400)

        os.makedirs(SHOTS, exist_ok=True)
        path = _unique_path(os.path.join(SHOTS, base))
        with open(path, 'wb') as f:
            f.write(data)

        jid = _job_new()
        # 识别不用内容策略（那份覆盖只影响大纲/规划），所以不起 `_spawn`
        threading.Thread(target=run_recognize_layout,
                         args=(jid, path, os.path.basename(path)), daemon=True).start()
        return self._json({'job_id': jid, 'name': os.path.basename(path)})

    # ── 路由 ──────────────────────────────────────────────────
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = urllib.parse.unquote(u.path)

        if p in ('/', '/index.html'):
            return self._file(os.path.join(WEB, 'index.html'), 'text/html; charset=utf-8')
        if p in ('/layouts', '/layouts.html'):
            # 版式管理页：查看图鉴 / 增删启停 / 上传截图识别新版式。
            # 独立页面而不是塞进主页 —— 主页是一条「选源 → 大纲 → 生成 → 按页改」
            # 的流水线，版式管理跟那条线没有前后关系。
            return self._file(os.path.join(WEB, 'layouts.html'),
                              'text/html; charset=utf-8')
        if p.startswith('/static/'):
            f = _safe_join(WEB, p[len('/static/'):])
            return (self._file(f) if f else self._json({'error': 'not found'}, 404))

        if p == '/api/config':
            cfg_mod.load_env()
            llm, vis = cfg_mod.llm_config(), cfg_mod.vision_config()
            lo, hi = cfg_mod.page_range()
            return self._json(dict(
                llm=(llm.model if llm else None),
                vision=(vis.model if vis else None),
                pages=[lo, hi], mode=cfg_mod.content_mode(),
                template=os.path.basename(cfg_mod.template_path()),
                officecli=_officecli_ok(),
                max_upload=MAX_UPLOAD,
                log=cfg_mod.log_enabled(), log_dir=LOGS,
                exts=list(SOURCE_EXT)))

        if p == '/api/sources':
            out = []
            for f in sorted(os.listdir(ROOT)):
                if os.path.splitext(f)[1].lower() not in SOURCE_EXT:
                    continue
                full = os.path.join(ROOT, f)
                if os.path.isfile(full):
                    out.append(dict(name=f, size=os.path.getsize(full), kind='root',
                                    mtime=os.path.getmtime(full)))
            out.extend(_list_uploads())
            return self._json(out)

        if p.startswith('/api/job/'):
            jid = p.rsplit('/', 1)[-1]
            with JOBS_LOCK:
                j = JOBS.get(jid)
            if not j:
                return self._json({'error': 'no such job'}, 404)
            # JOBS 里挂着内部对象（runlog），不能整个丢给 json.dumps。
            # 要回给前端的只有下面这几个键。
            return self._json({k: v for k, v in j.items() if not k.startswith('_')
                               and k != 'runlog'})

        if p == '/api/layouts':
            # 版式清单：内置 + 自定义，带来源、启用状态、预览图 URL 与元数据。
            # 页面上的「查看 / 启用禁用 / 删除」全从这一份渲染。
            #
            # 打开页面时**重读一次目录**（`force=True`）：注册表平时只在进程启动时
            # 加载，手工放进 `layouts_custom/` 或手工改过的 JSON 否则要重启才生效。
            # 这是用户主动打开页面这个动作带来的副作用，不是后台轮询，代价可忽略。
            layout_store.load_all(force=True)
            items = []
            custom = {m.get('name'): m for m in layout_store.list_metas()}
            for name in layout_spec.names():
                info = layout_store.info(name)
                info['preview'] = _preview_url(name)
                if info['source'] == 'custom':
                    meta = custom.get(name) or {}
                    info['blocks'] = len(meta.get('blocks') or [])
                    info['origin'] = (meta.get('origin') or {}).get('from') or 'image'
                    info['notes'] = meta.get('_notes') or meta.get('_problems') or []
                items.append(info)
            return self._json(dict(
                items=items, disabled=sorted(layout_spec.disabled_names()),
                dir=layout_store.directory(),
                vision=bool(cfg_mod.vision_config()),
                template_ok=os.path.isfile(cfg_mod.template_path()),
                officecli=bool(visual_mod.find_officecli())))

        if p.startswith('/api/layout-draft/'):
            rid = p.rsplit('/', 1)[-1]
            got = _load_draft(rid)
            if got is None:
                return self._json({'error': 'no such draft'}, 404)
            # 草稿里的预览图可能是上一轮留下的，每次读都按当前文件重新判定
            got['preview'] = _draft_preview_url(rid) if got.get('ok') else None
            return self._json(got)

        if p.startswith('/api/deck/'):
            # 前端的卡片标签、「要不要提示会丢人工修改」、以及提案阶段的页码表都靠这一份。
            # 页码映射只在 revise.build_page_index 里实现一次，前端只显示不算。
            stem = _stem_of(urllib.parse.unquote(p[len('/api/deck/'):]))
            if not _safe_join(PLANS, stem + '.deck.json'):
                return self._json({'error': 'bad name'}, 400)
            path, deck = _resolve_deck(stem)
            if deck is None:
                return self._json({'error': 'no such deck'}, 404)
            revs: list = []
            try:
                if os.path.isfile(_revisions_path(stem)):
                    revs = pipeline.load_json(_revisions_path(stem)).get('revisions') or []
            except (ValueError, OSError):
                pass            # 台账读不了不该让整页打不开
            return self._json(dict(
                name=stem, deck=path, sha=revise_mod.deck_fingerprint(deck),
                title=deck.get('title') or '', toc=deck.get('toc') or [],
                page_index=revise_mod.build_page_index(deck),
                revisions=revs))

        if p.startswith('/api/revision/'):
            # 取回一份修订方案草稿。存在的理由是**刷新页面后还能接上**：
            # job 只活在内存里，轮询一断前端就什么都拿不到了。
            rid = re.sub(r'[^\w-]', '', p.rsplit('/', 1)[-1])[:32]
            if not rid:
                return self._json({'error': 'bad id'}, 400)
            try:
                hits = [f for f in os.listdir(PLANS)
                        if f.endswith('.revise-%s.json' % rid)]
            except OSError:
                hits = []
            if not hits:
                return self._json({'error': 'no such revision'}, 404)
            return self._json(pipeline.load_json(os.path.join(PLANS, hits[0])))

        if p.startswith('/preview/'):
            f = _safe_join(VISUAL, p[len('/preview/'):])
            return (self._file(f, 'image/png') if f
                    else self._json({'error': 'not found'}, 404))

        if p.startswith('/download/'):
            f = _safe_join(SAMPLES, os.path.basename(p[len('/download/'):]))
            return (self._file(f, download=True) if f
                    else self._json({'error': 'not found'}, 404))

        return self._json({'error': 'not found'}, 404)

    def do_DELETE(self):
        p = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)

        if p.startswith('/api/layouts/'):
            # 只对**自定义**版式开放：内置版式的定义是 layouts.py 里的代码，
            # 删不掉也不该删（layout_store.unregister 会拦下来）。
            name = os.path.basename(p[len('/api/layouts/'):].replace('\\', '/'))
            try:
                removed = layout_store.remove_meta(name)
            except ValueError as e:
                return self._json({'error': str(e)}, 403)
            if not removed:
                return self._json({'error': '没有这套版式：%s' % name}, 404)
            for leftover in (_preview_file(name),):
                # 预览图跟着删：留着会变成「版式没了但图鉴里还有一张图」
                if leftover and os.path.isfile(leftover):
                    os.remove(leftover)
            return self._json(dict(removed=True, name=name))

        if not p.startswith('/api/uploads/'):
            return self._json({'error': 'not found'}, 404)

        base = os.path.basename(p[len('/api/uploads/'):].replace('\\', '/'))
        removed = False
        with UPLOAD_LOCK:
            for uid, e in list(UPLOAD_MEM.items()):
                if e['name'] == base:
                    UPLOAD_MEM.pop(uid, None)
                    removed = True
        f = _safe_join(UPLOADS, base)
        if f and os.path.isfile(f):
            os.remove(f)
            removed = True
        return self._json(dict(removed=removed, name=base))

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        p = urllib.parse.unquote(u.path)

        # 上传先于 JSON 解析 —— 它的 body 是原始二进制，不是 JSON
        if p == '/api/upload':
            return self._upload(u)
        if p == '/api/layouts/recognize':
            return self._upload_shot(u)

        try:
            body = self._body()
        except Exception as e:
            return self._json({'error': 'bad json: %s' % e}, 400)

        if p == '/api/outline':
            # 两种来源：刚上传的（upload_id）或根目录/上传目录里的既有文档（src）
            uid = (body.get('upload_id') or '').strip()
            data, original = None, None
            if uid:
                with UPLOAD_LOCK:
                    e = UPLOAD_MEM.get(uid)
                if e:
                    # 内存副本命中 —— 确定是上传来的
                    src, data, name = e['path'], e['data'], e['name']
                    origin, original = UPLOADS_DIR, e.get('original_name')
                else:
                    # 内存副本可能已被挤掉（或服务重启过），退回磁盘那份。
                    # 这条路上根目录的同名文件会遮蔽上传的那份，所以来源
                    # 以 `_find_source` 实际命中的目录为准，别按用户意图猜。
                    hit = _find_source(body.get('name') or '')
                    if not hit:
                        return self._json(
                            {'error': '上传已失效，请重新上传该文件'}, 400)
                    src, origin = hit
                    name = os.path.basename(src)
            else:
                hit = _find_source(body.get('src') or '')
                if not hit:
                    return self._json(
                        {'error': '文件不存在：%s' % (body.get('src') or '')}, 400)
                src, origin = hit
                name = os.path.basename(src)
            jid = _job_new()
            _spawn(run_outline, (jid, src, data, name, origin, original),
                   cfg_mod.pick_content_mode(body.get('mode')))
            return self._json({'job_id': jid})

        if p == '/api/generate':
            outline = body.get('outline')
            parsed_path = body.get('parsed_path') or ''
            name = re.sub(r'[^\w一-龥.-]', '_', body.get('name') or 'deck')
            rounds = int(body.get('rounds') or 3)
            if not isinstance(outline, dict):
                return self._json({'error': '缺少 outline'}, 400)
            safe = _safe_join(PLANS, os.path.basename(parsed_path.replace('\\', '/')))
            if not safe or not os.path.isfile(safe):
                return self._json({'error': '解析结果不存在：%s' % parsed_path}, 400)
            parsed_path = safe
            # 用户改过的大纲由 `run_generate` 落盘并归档进流程日志 —— 那段原来在这里
            # 带着 `except: pass`，会把失败悄悄吞掉，导致「落盘的那份」和「实际拿去
            # 规划的这份」不一致而没人知道。
            jid = _job_new()
            _spawn(run_generate, (jid, outline, parsed_path, name, rounds),
                   cfg_mod.pick_content_mode(body.get('mode')))
            return self._json({'job_id': jid})

        if p == '/api/revise':
            deck = (body.get('deck') or '').strip()
            mode = body.get('mode') or 'patch'
            text = (body.get('text') or '').strip()
            if not deck:
                return self._json({'error': '缺少 deck'}, 400)
            if not text:
                return self._json({'error': '请写下要改什么'}, 400)
            if mode not in ('patch', 'rewrite'):
                return self._json({'error': 'mode 只能是 patch 或 rewrite'}, 400)
            jid = _job_new()
            threading.Thread(target=run_revise, args=(jid, deck, mode, text),
                             daemon=True).start()
            return self._json({'job_id': jid})

        if p == '/api/revise/apply':
            deck = (body.get('deck') or '').strip()
            mode = body.get('mode') or 'patch'
            items = body.get('items')
            if not deck:
                return self._json({'error': '缺少 deck'}, 400)
            if not isinstance(items, list) or not items:
                return self._json({'error': '没有要应用的条目'}, 400)
            jid = _job_new()
            threading.Thread(target=run_apply,
                             args=(jid, deck, mode, items, body.get('sha') or '',
                                   body.get('rid') or ''), daemon=True).start()
            return self._json({'job_id': jid})

        if p == '/api/layouts/state':
            name = (body.get('name') or '').strip()
            try:
                layout_store.set_enabled(name, bool(body.get('enabled')))
            except ValueError as e:
                return self._json({'error': str(e)}, 400)
            return self._json(dict(name=name, enabled=layout_spec.is_enabled(name)))

        if p == '/api/layouts/adopt':
            rid = (body.get('rid') or '').strip()
            d = _load_draft(rid)
            if not d or not d.get('meta'):
                return self._json({'error': '识别草稿不存在或已过期，请重新识别'}, 404)
            name = d['meta'].get('name') or ''
            try:
                layout_store.save_meta(d['meta'])
            except ValueError as e:
                return self._json({'error': str(e)}, 400)
            # 试片图搬进版式自己的预览位，页面与图鉴从此有图可看
            src = _safe_join(VISUAL, '_layouts/_draft-%s/trial/page-%02d.png' % (rid, PAGE))
            dst = _preview_file(name)
            if src and dst and os.path.isfile(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(src, dst)
            _drop_draft(rid)
            return self._json(dict(name=name, enabled=True,
                                   preview=_preview_url(name)))

        if p == '/api/layouts/discard':
            rid = (body.get('rid') or '').strip()
            _drop_draft(rid)                     # 试片目录留着无妨，预览图那条路会忽略它
            return self._json(dict(discarded=True))

        if p == '/api/layouts/previews':
            jid = _job_new()
            threading.Thread(target=run_layout_previews, args=(jid,),
                             daemon=True).start()
            return self._json({'job_id': jid})

        return self._json({'error': 'not found'}, 404)


# ══════════════════════════════════════════════════════════════
# 版式管理：识别截图 → 草稿 → 采用/放弃；预览图；启停与删除
# ══════════════════════════════════════════════════════════════

def _preview_file(name: str) -> str | None:
    return _safe_join(LAYOUT_VISUAL, '%s/page-%02d.png' % (name, PAGE))


def _preview_url(name: str) -> str | None:
    """版式预览图的 URL（图还没渲出来就是 None —— 前端据此显示「生成预览图」）。"""
    f = _preview_file(name)
    if f and os.path.isfile(f):
        return '/preview/_layouts/%s/page-%02d.png' % (name, PAGE)
    return None


def _draft_path(rid: str) -> str | None:
    rid = re.sub(r'[^\w-]', '', rid or '')[:24]
    if not rid:
        return None
    return _safe_join(PLANS, DRAFT_SUFFIX % rid)


def _draft_work(rid: str) -> str:
    return _safe_join(VISUAL, '_layouts/_draft-%s' % rid) or ''


def _draft_preview_url(rid: str) -> str | None:
    f = _safe_join(VISUAL, '_layouts/_draft-%s/trial/page-%02d.png' % (rid, PAGE))
    if f and os.path.isfile(f):
        return '/preview/_layouts/_draft-%s/trial/page-%02d.png' % (rid, PAGE)
    return None


def _save_draft(rid: str, payload: dict) -> None:
    p = _draft_path(rid)
    if not p:
        return
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _load_draft(rid: str) -> dict | None:
    p = _draft_path(rid)
    if not p or not os.path.isfile(p):
        return None
    try:
        with open(p, encoding='utf-8') as f:
            got = json.load(f)
        return got if isinstance(got, dict) else None
    except (OSError, ValueError):
        return None


def _drop_draft(rid: str) -> None:
    p = _draft_path(rid)
    if p and os.path.isfile(p):
        os.remove(p)


def run_recognize_layout(jid: str, image_path: str, shot_name: str):
    """识别一张版式截图 → 草稿。在后台线程里跑。

    草稿**落盘**（`out/plans/layout-<rid>.json`），不只在内存 job 里 —— 识别要
    十几秒、还可能重试两轮，用户刷新一下页面草稿就没了会很难受。这条补偿模式
    与 `/api/revision/<rid>` 一致。
    """
    rid = uuid.uuid4().hex[:10]
    work = _draft_work(rid)
    try:
        runlog = runlog_mod.RunLog('layout-%s' % rid, origin='web', command='layouts')
        with JOBS_LOCK:
            JOBS[jid]['runlog'] = runlog
        runlog.put('config', runlog_mod.config_snapshot())
        _log(jid, '本次流程日志：%s' % runlog.dir)
        with runlog.stage('recognize') as st:
            _log(jid, '识别版式截图（调用视觉模型，可能要十几秒）…', 'active')
            draft = recognize_mod.recognize(image_path, work,
                                            on_log=lambda m: _log(jid, m))
            st.update(ok=draft['ok'], rounds=len(draft['attempts']))
        meta = draft.get('meta') or {}
        trial = draft.get('trial') or {}
        _save_draft(rid, dict(
            rid=rid, shot=shot_name, ok=draft['ok'], reason=draft['reason'],
            meta=meta, verify=draft.get('verify'), attempts=draft['attempts'],
            geometry=(trial.get('geometry') or {}).get('summary'),
            truncations=[list(t) for t in (trial.get('truncations') or [])],
            preview=_draft_preview_url(rid) if draft['ok'] else None,
        ))
        if draft['ok']:
            _log(jid, '识别通过：%s' % meta.get('name'), 'success',
                 detail='试片几何 %s' % ((trial.get('geometry') or {}).get('summary')))
        else:
            _log(jid, draft['reason'], 'warning')
        _finish(jid, result=dict(rid=rid, ok=draft['ok'], name=meta.get('name'),
                                 reason=draft['reason']))
    except Exception as e:                      # noqa: BLE001 —— 与其它任务同样的收尾
        traceback.print_exc()
        _log(jid, '失败：%s' % e, 'warning')
        _finish(jid, error='%s: %s' % (type(e).__name__, e))


def _layout_sample(name: str) -> dict | None:
    """取一套版式的样板 spec（内置的来自 samples，自定义的来自它自己的 sample）。

    自定义版式的 spec 要**带上 blocks**：它的渲染函数是同一个声明式渲染器，
    而区块在注册表里 —— 试片/预览这条路走 `spec['blocks']`，不必先注册。
    """
    sp = layout_spec.REGISTRY.get(name)
    if sp is None:
        return None
    if sp.source == 'builtin':
        return samples.for_layout(name)
    for meta in layout_store.list_metas():
        if meta.get('name') == name and isinstance(meta.get('sample'), dict):
            sl = dict(meta['sample'])
            sl['layout'] = name
            sl['blocks'] = meta.get('blocks') or []
            return sl
    return None


def run_layout_previews(jid: str):
    """给还没有预览图的版式渲预览图（一次 build + 一次逐页渲）。

    19 套内置版式 + 自定义版式一起渲，产物落到 `out/visual/_layouts/<名字>/`，
    缓存住 —— 这是版式页「查看」那一半的数据来源。
    """
    try:
        todo = [n for n in layout_spec.names() if not _preview_url(n)]
        have = [n for n in todo if _layout_sample(n)]
        _log(jid, '需要生成预览图的版式：%d 套（其余已有缓存）' % len(have))
        if not have:
            _finish(jid, result=dict(rendered=0))
            return
        work = os.path.join(LAYOUT_VISUAL, '_preview_build')
        os.makedirs(work, exist_ok=True)
        slides = [s for s in (_layout_sample(n) for n in have) if s]
        names = [s['layout'] for s in slides]
        pptx = os.path.join(work, 'layouts.pptx')
        _log(jid, '先渲一版含全部样例的 deck…', 'active')
        build_mod.build(dict(slides=slides, toc=[]), cfg_mod.template_path(), pptx)
        # 逐页渲、逐页落位：officecli 一页要十几秒，19 页连着渲完再拷贝的话
        # 用户要对着空页面干等几分钟、中间看不到任何进展（这条实测过）。
        rendered = 0
        for i, name in enumerate(names):
            _log(jid, '预览图 %d/%d：%s' % (i + 1, len(names), name), 'active')
            try:
                got = visual_mod.render_pages(pptx, work, pages=[PAGE + i])
            except RuntimeError as e:               # officecli 缺失：一次都渲不出来
                _log(jid, '渲染器不可用：%s' % e, 'warning')
                break
            src, dst = got.get(PAGE + i), _preview_file(name)
            if not src or not dst:
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            rendered += 1
        _log(jid, '预览图完成：%d / %d' % (rendered, len(names)),
             'success' if rendered else 'warning')
        _finish(jid, result=dict(rendered=rendered, total=len(names)))
    except Exception as e:                      # noqa: BLE001
        traceback.print_exc()
        _log(jid, '预览图生成失败：%s' % e, 'warning')
        _finish(jid, error='%s: %s' % (type(e).__name__, e))


def main():
    ap = argparse.ArgumentParser(description='文档转 PPT —— 本地 Web 前端')
    ap.add_argument('--port', type=int, default=8000)
    ap.add_argument('--host', default='127.0.0.1')
    a = ap.parse_args()
    cfg_mod.load_env()
    for d in (SAMPLES, PLANS, REPORTS, VISUAL, UPLOADS, LOGS, LAYOUT_VISUAL, SHOTS):
        os.makedirs(d, exist_ok=True)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print('文档转 PPT —— 前端已启动')
    print('  打开浏览器访问  http://%s:%d' % (a.host, a.port))
    print('  Ctrl+C 停止')
    print()
    print(cfg_mod.summary())
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止')


if __name__ == '__main__':
    main()
