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
from pptgen import parse as parse_mod          # noqa: E402
from pptgen import pipeline                    # noqa: E402
from pptgen import repair as repair_mod        # noqa: E402
from pptgen import runlog as runlog_mod        # noqa: E402
from pptgen.qa import geometry                 # noqa: E402

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
            build_mod.build(spec, cfg_mod.template_path(), pptx, fill_toc=True)
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
            pipeline.save_json(deck, os.path.join(PLANS, '%s.deck.repaired.json' % name))

            # 用最终 spec 重建一次，确保落盘的是修复后的版本
            build_mod.build(deck, cfg_mod.template_path(), pptx, fill_toc=True)
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
            _log(jid, '渲染每页预览图…', 'active')
            pages = _render(pptx, os.path.join(VISUAL, name))
            st.update(pages=len(pages))
            runlog.note('render', pages=len(pages),
                        visual_dir=os.path.join(VISUAL, name))

        _finish(jid, result=dict(
            name=name, pptx=pptx, deck=deck_path, pages=pages,
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


def _write_confirmed_outline(outline: dict, name: str) -> str:
    """落盘「人工确认后」的大纲，返回路径。

    原本这段在主线程里带 `try/except: pass` 吞掉失败 —— 结果是「落盘的那份」
    和「实际拿去规划的这份」可能不一致，且没人知道。现在失败会走日志。
    """
    path = os.path.join(PLANS, '%s.outline.json' % name)
    pipeline.save_json(outline, path)
    return path


def _officecli_ok() -> bool:
    from pptgen.qa import visual
    return bool(visual.find_officecli())


def _drop_stale_renders(work: str, pptx: str) -> None:
    """deck 换了就把上一份的渲染图清掉。

    `work` 是按 deck 名分的（`out/visual/<stem>/`），而同一个源文档反复生成时
    deck 名不变 —— 于是下面那句「已有 page-NN.png 就跳过渲染」会把**上一次**的图
    留在原地：页码对不上、页数不同时还会多出几张旧图。

    踩过：11:25 那次跑完，页面上 17 张预览全是 09:23 那一版的 15 页内容，
    用户按预览里的页码反馈问题，指的根本不是这一版。**预览看着成功，其实是旧的。**
    """
    st = os.stat(pptx)
    stamp = '%d:%d' % (st.st_mtime_ns, st.st_size)
    mark = os.path.join(work, '.pptx-stamp')
    try:
        with open(mark, 'r', encoding='utf-8') as f:
            if f.read().strip() == stamp:
                return
    except OSError:
        pass
    for f in os.listdir(work):
        if f == 'contact-sheet.png' or (f.startswith('page-') and f.endswith('.png')):
            try:
                os.remove(os.path.join(work, f))
            except OSError:
                pass
    try:
        with open(mark, 'w', encoding='utf-8') as f:
            f.write(stamp)
    except OSError:
        pass    # 写不了标记只是下次多渲染一遍，不该让整条链路失败


def _render(pptx: str, work: str) -> list[str]:
    from pptx import Presentation
    from pptgen.qa import visual
    if not visual.find_officecli():
        return []
    os.makedirs(work, exist_ok=True)
    _drop_stale_renders(work, pptx)
    n = len(Presentation(pptx).slides)
    got = []
    for pg in range(1, n + 1):
        out = os.path.join(work, 'page-%02d.png' % pg)
        if not os.path.isfile(out):
            visual.render_pages(pptx, work, [pg], native=True)
        if os.path.isfile(out):
            got.append('page-%02d.png' % pg)
        if pg == 1:
            visual.render_contact_sheet(pptx, os.path.join(work, 'contact-sheet.png'))
    return got


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
_STAGE_SUFFIX = re.compile(r'\.(parsed|outline|deck|repaired)$')


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

    # ── 路由 ──────────────────────────────────────────────────
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = urllib.parse.unquote(u.path)

        if p in ('/', '/index.html'):
            return self._file(os.path.join(WEB, 'index.html'), 'text/html; charset=utf-8')
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
            threading.Thread(target=run_outline,
                             args=(jid, src, data, name, origin, original),
                             daemon=True).start()
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
            threading.Thread(target=run_generate,
                             args=(jid, outline, parsed_path, name, rounds),
                             daemon=True).start()
            return self._json({'job_id': jid})

        return self._json({'error': 'not found'}, 404)


def main():
    ap = argparse.ArgumentParser(description='文档转 PPT —— 本地 Web 前端')
    ap.add_argument('--port', type=int, default=8000)
    ap.add_argument('--host', default='127.0.0.1')
    a = ap.parse_args()
    cfg_mod.load_env()
    for d in (SAMPLES, PLANS, REPORTS, VISUAL, UPLOADS, LOGS):
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
