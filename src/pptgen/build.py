# -*- coding: utf-8 -*-
"""把内容 spec 组装成 pptx：沿用公司模板的封面 / 目录 / 封底，中间插入生成的正文页。

⚠️ 核心工程约束（实测踩出来的）：

  **所有 `add_slide` 必须在删页之前完成。**

  python-pptx 的 `add_slide` 按「现有 slide 部件数量」推算新文件名。若先删掉模板里的
  内容页（部件数 4→3）再新增，新页会被命名成 `slide4.xml`，与既有封底**撞名** ——
  产出 zip 内出现两个同名条目、两个 rId 指向同一部件，PowerPoint 表现为「修复」或
  显示错页，而**模板封底会被静默覆盖丢失**。

  先加后删即可避免。`selfcheck()` 会把这条固化成可回归的断言。
"""
from __future__ import annotations
import collections
import os
import re
import zipfile

from pptx import Presentation
from pptx.util import Pt
from pptx.oxml.ns import qn

from .tokens import LAYOUT_BLANK, LAYOUT_TITLE_ONLY, EA, LAT, DARK, MUTED
from .layouts import render_slide


class BuildError(RuntimeError):
    pass


def _layout_by_name(prs, name):
    for l in prs.slide_layouts:
        if l.name == name:
            return l
    raise BuildError('layout %r not found in template; have: %s'
                     % (name, [l.name for l in prs.slide_layouts]))


def _set_text(shape, text, size=None, bold=None, color=None):
    tf = shape.text_frame
    tf.text = text
    for p in tf.paragraphs:
        for r in p.runs:
            if size:
                r.font.size = Pt(size)
            if bold is not None:
                r.font.bold = bold
            if color is not None:
                r.font.color.rgb = color
            r.font.name = LAT
            rPr = r._r.get_or_add_rPr()
            el = rPr.find(qn('a:ea'))
            if el is None:
                el = rPr.makeelement(qn('a:ea'), {})
                rPr.append(el)
            el.set('typeface', EA)


def fill_agenda(prs, items):
    """把目录页的占位符填成章节列表。items = [str, ...]"""
    slide = prs.slides[1]
    ph = [sh for sh in slide.shapes if sh.is_placeholder]
    title_ph = next((s for s in ph if s.placeholder_format.idx == 0), None)
    body_ph = next((s for s in ph if s.placeholder_format.idx != 0), None)
    if title_ph is not None:
        _set_text(title_ph, '目录', size=28, bold=True, color=DARK)
    if body_ph is not None:
        tf = body_ph.text_frame
        tf.text = items[0]
        for it in items[1:]:
            tf.add_paragraph().text = it
        for p in tf.paragraphs:
            p.space_after = Pt(8)
            for r in p.runs:
                r.font.size = Pt(16)
                r.font.color.rgb = MUTED
                r.font.name = LAT
                rPr = r._r.get_or_add_rPr()
                el = rPr.makeelement(qn('a:ea'), {})
                el.set('typeface', EA)
                rPr.append(el)


def build(spec: dict, template: str, out_path: str, *, fill_toc: bool = True) -> str:
    """spec = {"slides": [ {layout, page, ...}, ... ], "toc": [str, ...]}"""
    prs = Presentation(template)
    sldIdLst = prs.slides._sldIdLst
    n_orig = len(prs.slides)
    blank = _layout_by_name(prs, LAYOUT_BLANK)

    # ① 先全部新增（必须在删页之前！）
    #    页码自动分配：封面=1、目录=2，正文从 3 开始，封底不编号。
    for i, sl in enumerate(spec['slides']):
        sl['page'] = i + 3
        render_slide(prs.slides.add_slide(blank), sl)

    # ② 再删掉模板自带的空内容页（索引 2），保留封面 0 / 目录 1 / 封底 3
    if n_orig >= 4:
        ids = list(sldIdLst)
        dead = ids[2]
        prs.part.drop_rel(dead.get(qn('r:id')))
        sldIdLst.remove(dead)

    # ③ 重排：封面 / 目录 / 正文… / 封底
    ids = list(sldIdLst)
    order = [0, 1] + list(range(3, len(ids))) + [2]
    for e in list(sldIdLst):
        sldIdLst.remove(e)
    for i in order:
        sldIdLst.append(ids[i])

    if fill_toc and spec.get('toc'):
        fill_agenda(prs, spec['toc'])

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    prs.save(out_path)

    problems = selfcheck(out_path, expected_slides=len(ids))
    if problems:
        raise BuildError('结构自检未通过:\n  ' + '\n  '.join(problems))
    return out_path


def selfcheck(path: str, expected_slides: int | None = None) -> list[str]:
    """结构自检：zip 重名条目 / sldIdLst 目标唯一 / 图片 rel 可解析。"""
    problems: list[str] = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        dup = [n for n, c in collections.Counter(names).items() if c > 1]
        if dup:
            problems.append('zip 内存在重名条目（partname 撞名）: %s' % dup)

        pres = z.read('ppt/presentation.xml').decode('utf-8')
        rels = z.read('ppt/_rels/presentation.xml.rels').decode('utf-8')
        r2t = dict(re.findall(
            r'Id="(rId\d+)"[^>]*Target="slides/(slide\d+\.xml)"', rels))
        rids = re.findall(r'<p:sldId [^>]*r:id="(rId\d+)"', pres)
        targets = [r2t.get(r, '??') for r in rids]
        if len(set(targets)) != len(targets):
            problems.append('多个 sldId 指向同一个 slide 部件: %s' % targets)
        if expected_slides is not None and len(targets) != expected_slides:
            problems.append('页数不符: sldIdLst=%d, 期望=%d'
                            % (len(targets), expected_slides))
        for t in set(targets):
            if t != '??' and ('ppt/slides/' + t) not in names:
                problems.append('sldId 指向不存在的部件: %s' % t)

    # 图片 rel 断链（空框的成因）
    prs = Presentation(path)
    for i, slide in enumerate(prs.slides, 1):
        for rel in slide.part.rels.values():
            if rel.is_external:
                continue
            tp = getattr(rel, 'target_part', None)
            if tp is None:
                problems.append('s%d 存在无法解析的关系: %s' % (i, rel.rId))
            elif str(rel.reltype).endswith('/image') and not getattr(tp, 'blob', None):
                problems.append('s%d 图片关系 %s 的目标为空（会渲染成空框）' % (i, rel.rId))
    return problems
