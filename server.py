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
from pptgen.qa import geometry                 # noqa: E402

WEB = os.path.join(ROOT, 'web')
SAMPLES = os.path.join(ROOT, 'out', 'samples')
PLANS = os.path.join(ROOT, 'out', 'plans')
REPORTS = os.path.join(ROOT, 'out', 'reports')
VISUAL = os.path.join(ROOT, 'out', 'visual')

SOURCE_EXT = ('.pptx', '.docx', '.pdf', '.txt', '.md', '.markdown')
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


# ══════════════════════════════════════════════════════════════
# 后台任务
# ══════════════════════════════════════════════════════════════
def _job_new() -> str:
    jid = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[jid] = dict(id=jid, status='running', log=[], result=None, error=None)
    return jid


def _log(jid: str, msg: str):
    with JOBS_LOCK:
        if jid in JOBS:
            JOBS[jid]['log'].append(msg)


def _finish(jid: str, result=None, error=None):
    with JOBS_LOCK:
        if jid in JOBS:
            JOBS[jid]['status'] = 'failed' if error else 'done'
            JOBS[jid]['result'] = result
            JOBS[jid]['error'] = error


def run_generate(jid: str, outline: dict, parsed_path: str, name: str, rounds: int):
    """规划 → 修复回环 → 渲染。在后台线程里跑。"""
    try:
        _log(jid, '读取解析结果…')
        doc = pipeline.load_json(parsed_path)

        _log(jid, '规划每页版式与内容（调用模型）…')
        # 把 pipeline 内部的进度/失败原因也送进任务日志，否则前端只能看到沉默
        plan = pipeline.make_plan(outline, doc, on_log=lambda m: _log(jid, m))
        deck_path = os.path.join(PLANS, '%s.deck.json' % name)
        pipeline.save_json(plan, deck_path)
        counts = {}
        for s in plan['slides']:
            counts[s['layout']] = counts.get(s['layout'], 0) + 1
        _log(jid, '规划完成：%d 页，版式分布 %s' % (len(plan['slides']), counts))

        pptx = os.path.join(SAMPLES, '%s.pptx' % name)

        def build_qa(spec):
            build_mod.build(spec, cfg_mod.template_path(), pptx, fill_toc=True)
            rep = geometry.analyse(pptx)
            s = rep['summary']
            _log(jid, '几何检查：%d error / %d warn' % (s['error'], s['warn']))
            return rep

        _log(jid, '开始修复回环（最多 %d 轮）…' % rounds)
        deck = plan                      # ← 之前这里漏了赋值，直接传未定义的 deck
        deck, final = repair_mod.repair_deck(deck, build_qa, rounds=rounds,
                                             verbose=False)
        pipeline.save_json(deck, os.path.join(PLANS, '%s.deck.repaired.json' % name))

        # 用最终 spec 重建一次，确保落盘的是修复后的版本
        build_mod.build(deck, cfg_mod.template_path(), pptx, fill_toc=True)
        rep = geometry.analyse(pptx)
        pipeline.save_json(rep, os.path.join(REPORTS, '%s.geometry.json' % name))
        with open(os.path.join(REPORTS, '%s.geometry.md' % name), 'w',
                  encoding='utf-8') as f:
            f.write('# 几何 QA 报告\n\n```\n' + geometry.format_report(rep) + '\n```\n')

        _log(jid, '渲染每页预览图…')
        pages = _render(pptx, os.path.join(VISUAL, name))

        _finish(jid, result=dict(
            name=name, pptx=pptx, deck=deck_path, pages=pages,
            summary=rep['summary'], slides=rep['slides'],
            download='/download/%s.pptx' % urllib.parse.quote(name)))
        _log(jid, '完成。')
    except Exception as e:
        traceback.print_exc()
        _log(jid, '失败：%s' % e)
        _finish(jid, error='%s: %s' % (type(e).__name__, e))


def _render(pptx: str, work: str) -> list[str]:
    from pptgen.qa import visual
    exe = visual.find_officecli()
    if not exe:
        return []
    os.makedirs(work, exist_ok=True)
    n = len(__import__('pptx').Presentation(pptx).slides)
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


def run_outline(jid: str, src: str):
    try:
        src = os.path.normpath(src)
        _log(jid, '解析源文档：%s' % os.path.basename(src))
        doc = parse_mod.parse(src)
        stem = os.path.splitext(os.path.basename(src))[0][:40]
        parsed_path = os.path.join(PLANS, stem + '.parsed.json')
        pipeline.save_json(doc, parsed_path)
        _log(jid, '解析完成：%d 个内容块 / %d 字'
             % (doc['stats']['blocks'], doc['stats']['chars']))

        _log(jid, '生成大纲（调用模型）…')
        outline = pipeline.make_outline(doc)
        outline_path = os.path.join(PLANS, stem + '.outline.json')
        pipeline.save_json(outline, outline_path)
        with open(os.path.splitext(outline_path)[0] + '.md', 'w', encoding='utf-8') as f:
            f.write(pipeline.outline_preview(outline) + '\n')
        _log(jid, '大纲完成：%d 页正文' % outline['page_count'])

        _finish(jid, result=dict(outline=outline, outline_path=outline_path,
                                 parsed_path=parsed_path, stem=stem,
                                 preview=pipeline.outline_preview(outline)))
    except Exception as e:
        traceback.print_exc()
        _log(jid, '失败：%s' % e)
        _finish(jid, error='%s: %s' % (type(e).__name__, e))


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
        if not os.path.isfile(path):
            return self._json({'error': 'not found: %s' % path}, 404)
        ctype = ctype or mimetypes.guess_type(path)[0] or 'application/octet-stream'
        with open(path, 'rb') as f:
            data = f.read()
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        if download:
            self.send_header('Content-Disposition',
                             'attachment; filename="%s"' % os.path.basename(path))
        self.end_headers()
        self.wfile.write(data)

    # ── 路由 ──────────────────────────────────────────────────
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = urllib.parse.unquote(u.path)

        if p in ('/', '/index.html'):
            return self._file(os.path.join(WEB, 'index.html'), 'text/html; charset=utf-8')
        if p.startswith('/static/'):
            return self._file(os.path.join(WEB, p[len('/static/'):]))

        if p == '/api/config':
            cfg_mod.load_env()
            llm, vis = cfg_mod.llm_config(), cfg_mod.vision_config()
            lo, hi = cfg_mod.page_range()
            return self._json(dict(
                llm=(llm.model if llm else None),
                vision=(vis.model if vis else None),
                pages=[lo, hi], mode=cfg_mod.content_mode(),
                template=os.path.basename(cfg_mod.template_path()),
                officecli=bool(__import__('pptgen.qa.visual', fromlist=['x']).find_officecli())))

        if p == '/api/sources':
            out = []
            for f in sorted(os.listdir(ROOT)):
                if os.path.splitext(f)[1].lower() in SOURCE_EXT:
                    out.append(dict(name=f, size=os.path.getsize(os.path.join(ROOT, f))))
            return self._json(out)

        if p.startswith('/api/job/'):
            jid = p.rsplit('/', 1)[-1]
            with JOBS_LOCK:
                j = JOBS.get(jid)
            if not j:
                return self._json({'error': 'no such job'}, 404)
            return self._json(j)

        if p.startswith('/preview/'):
            rel = p[len('/preview/'):]
            return self._file(os.path.join(VISUAL, rel.lstrip('/')), 'image/png')

        if p.startswith('/download/'):
            nm = p[len('/download/'):]
            safe = os.path.basename(nm)
            return self._file(os.path.join(SAMPLES, safe), download=True)

        return self._json({'error': 'not found'}, 404)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        try:
            body = self._body()
        except Exception as e:
            return self._json({'error': 'bad json: %s' % e}, 400)

        if p == '/api/outline':
            src = body.get('src') or ''
            src = src if os.path.isabs(src) else os.path.join(ROOT, src)
            if not os.path.isfile(src):
                return self._json({'error': '文件不存在：%s' % src}, 400)
            jid = _job_new()
            threading.Thread(target=run_outline, args=(jid, src), daemon=True).start()
            return self._json({'job_id': jid})

        if p == '/api/generate':
            outline = body.get('outline')
            parsed_path = body.get('parsed_path') or ''
            name = re.sub(r'[^\w一-龥.-]', '_', body.get('name') or 'deck')
            rounds = int(body.get('rounds') or 3)
            if not isinstance(outline, dict):
                return self._json({'error': '缺少 outline'}, 400)
            if not os.path.isfile(parsed_path):
                return self._json({'error': '解析结果不存在：%s' % parsed_path}, 400)
            # 保存用户改过的大纲（人工确认的落点）
            try:
                pipeline.save_json(outline, os.path.join(PLANS, name + '.outline.json'))
            except Exception:
                pass
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
    for d in (SAMPLES, PLANS, REPORTS, VISUAL):
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
