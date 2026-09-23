# -*- coding: utf-8 -*-
"""版式图鉴：把样例 spec 渲成图，拼成一张可以直接发到公网的 HTML 页。

    python gallery.py              # 重建 pptx → 渲图 → 拼 HTML
    python gallery.py --no-render  # 只重拼 HTML（图还在，省一次 officecli）

产物（都在 `out/gallery/`，整个目录可以原样发布）：

    layouts.pptx        样板 deck（封面 + 目录 + 样例正文 + 封底）。样例条数由
                        FIXTURES 决定，现为 21 条 / 内置 19 套版式 ——
                        split_main_aside 与 timeline_vertical 各有两条样例
    pages/page-NN.png   逐页渲染图
    index.html          图鉴页，图片走相对路径

**这份图鉴只收内置版式。** 自定义版式（`layouts_custom/*.json`）不进这里：
它们是**本机的版式库资产**，而这份 HTML 是「可以原样发到公网」的东西。
自定义版式的预览图在 Web 的「版式管理」页（`/layouts`）。

**样例不是另写的一套演示内容。** 取自 `pptgen/samples.py` 的 `FIXTURES` ——
那是唯一一份按容量声明逐套写好的样例，`TestRenderAll` 每次回归都拿它跑几何检查。
所以图鉴里的每一页都**已经过了几何自检**：框没给小、字没被 `_fit()` 静默截断，
否则回归测试先红。图鉴跟着回归走，不会与版式漂移。

**分组也不是另写的一份版式清单。** 版式清单的唯一真相来源是
`layout_spec.REGISTRY`，这里只按每套版式的**主职意图**（`intents[0]`）归类，
再给那 12 个意图配一个显示用的短标签。`layouts.py` 的模块注释里有一份
人工分组（结构骨架 / 陈述 / 指标 / …），README 里又抄了一遍 —— 再抄第三遍
就是这个仓库反复在修的漂移。
"""
from __future__ import annotations
import argparse
import html
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, 'src'))

from pptgen import build, config, layout_spec          # noqa: E402
from pptgen.qa import visual                           # noqa: E402
from pptgen.samples import FIXTURES                    # noqa: E402

# 封面/目录各占一页，正文从第 3 页起（build 里 `sl['page'] = i + 3`）
FIRST_CONTENT_PAGE = 3

# 意图 → 显示用短标签。只给 `layout_spec.INTENTS` 里已有的键配标签，
# 运行时断言覆盖完整，漏一个就报错，不会静默变成空白分组。
GROUP_LABELS = {
    '':             '结构骨架',    # roles=('section',)，无意图（按位置插入）
    'statement':    '陈述',
    'definition':   '概念',
    'enumeration':  '并列',
    'comparison':   '对照',
    'process':      '流程',
    'timeline':     '时间线',
    'hierarchy':    '层级',
    'quantitative': '数据与指标',
    'status':       '状态',
    'summary':      '结论',
    'quote':        '引语',
}


def _short_intent(intent: str) -> str:
    """`INTENT_LABELS` 里是「并列列举（若干同级要点）」，取括号前那截做标签。"""
    if not intent:
        return ''
    return layout_spec.INTENT_LABELS[intent].split('（')[0]


def _group_key(name: str) -> str:
    spec = layout_spec.REGISTRY[name]
    return spec.intents[0] if spec.intents else ''


def _specimens():
    """→ [(序号, 版式名, 变体序号, spec)]，序号即 REGISTRY 中的位置。

    第 4 项是 spec 在 FIXTURES 里的下标 —— 渲染页码由它推出（正文第 i 页 =
    `FIRST_CONTENT_PAGE + i`），不靠 `FIXTURES.index()` 反查。
    """
    order = {n: i + 1 for i, n in enumerate(layout_spec.names())}
    seen: dict[str, int] = {}
    out = []
    for fi, (_, spec) in enumerate(FIXTURES):
        name = spec['layout']
        variant = seen.get(name, 0)
        seen[name] = variant + 1
        out.append((order[name], name, variant, spec, fi))
    return out


def _sections():
    """按主职意图分组，组序取首次出现的顺序。"""
    groups: dict[str, list] = {}
    for item in _specimens():
        groups.setdefault(_group_key(item[1]), []).append(item)
    return list(groups.items())


# ══════════════════════════════════════════════════════════════
# 渲图
# ══════════════════════════════════════════════════════════════
def render_deck(out_dir: str, force: bool = True) -> tuple[str, dict]:
    tpl = config.template_path()
    if not os.path.isfile(tpl):
        raise SystemExit('模板不存在：%s' % tpl)

    pptx = os.path.join(out_dir, 'layouts.pptx')
    slides = [dict(spec) for _, spec in FIXTURES]
    build.build(dict(slides=slides,
                     toc=['01 初识 Dify', '02 为什么选 Dify', '03 Dify 能做什么'],
                     title='内容版式图鉴',
                     subtitle='%d 套版式 · 各一页样例' % len(layout_spec.names())),
                tpl, pptx)
    print('已生成 %s（%d 页正文）' % (pptx, len(slides)))

    pages_dir = os.path.join(out_dir, 'pages')
    pages = list(range(FIRST_CONTENT_PAGE, FIRST_CONTENT_PAGE + len(slides)))
    if not force and all(os.path.isfile(os.path.join(pages_dir, 'page-%02d.png' % p))
                         for p in pages):
        print('图已存在，跳过渲染（--no-render）')
    else:
        got = visual.render_pages(pptx, pages_dir, pages=pages)
        missing = [p for p in pages if p not in got]
        if missing:
            raise SystemExit('有 %d 页没渲出来：%s' % (len(missing), missing))
        print('已渲染 %d 张' % len(got))
    return pptx, {p: os.path.join(pages_dir, 'page-%02d.png' % p) for p in pages}


# ══════════════════════════════════════════════════════════════
# HTML
# ══════════════════════════════════════════════════════════════
# 配色直接取版式系统自己的令牌（src/pptgen/tokens.py）—— 图鉴页和幻灯片是
# 同一套系统：发丝线（RULE）是主导的结构手段，RED 只在少数几处出现。
CSS = """
:root{
  --ground:#FFFFFF;
  --surface:#EEF3F8;        /* TINT —— 版式系统里的浅色带 */
  --ink:#231F20;            /* DARK */
  --muted:#646463;          /* MUTED */
  --faint:#807F83;          /* GREY */
  --rule:#C9C9C9;           /* RULE —— 发丝线 */
  --rule-soft:#E6E9EC;
  --accent:#D31245;         /* RED —— 全页只打单焦点 */
  --slide:#FFFFFF;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --ground:#171416;
    --surface:#231F20;
    --ink:#EDEAEA;
    --muted:#9E9B9B;
    --faint:#8A8788;
    --rule:#3C3739;
    --rule-soft:#2A2627;
    --accent:#FF4D77;
    --slide:#FFFFFF;
  }
}
:root[data-theme="dark"]{
  --ground:#171416;
  --surface:#231F20;
  --ink:#EDEAEA;
  --muted:#9E9B9B;
  --faint:#8A8788;
  --rule:#3C3739;
  --rule-soft:#2A2627;
  --accent:#FF4D77;
  --slide:#FFFFFF;
}

*,*::before,*::after{box-sizing:border-box}

body{
  margin:0;
  background:var(--ground);
  color:var(--ink);
  font-family:"Noto Sans SC","PingFang SC","Microsoft YaHei",system-ui,sans-serif;
  font-size:15px;
  line-height:1.7;
  -webkit-font-smoothing:antialiased;
}

.wrap{
  max-width:1240px;
  margin:0 auto;
  padding-inline:20px;
  padding-block:56px 72px;
}

.mono{
  font-family:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-variant-numeric:tabular-nums;
}

/* ── 页头 ─────────────────────────────────────────────── */
.eyebrow{
  font-size:12px;
  letter-spacing:.14em;
  text-transform:uppercase;
  color:var(--faint);
  margin:0 0 18px;
}
/* 页头唯一的红 —— 呼应模板封面那条红带，全页的单一焦点 */
.eyebrow::before{
  content:"";
  display:inline-block;
  width:22px;
  height:2px;
  background:var(--accent);
  vertical-align:middle;
  margin-right:10px;
}
h1{
  font-size:clamp(30px,4.4vw,44px);
  line-height:1.14;
  font-weight:700;
  letter-spacing:-.01em;
  margin:0 0 20px;
  text-wrap:balance;
}
.lede{
  max-width:62ch;
  margin:0;
  color:var(--muted);
}
.lede strong{color:var(--ink);font-weight:600}
.lede .mono{font-size:.92em;color:var(--ink)}

/* ── 索引条 ───────────────────────────────────────────── */
/* 索引条坐在 TINT 浅色带上 —— 版式系统自己的「通栏浅色带」手段，只在这里用一次 */
.index{
  margin:40px 0 0;
  padding:20px 22px;
  background:var(--surface);
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(min(190px,100%),1fr));
  gap:2px 28px;
}
.index a{
  display:flex;
  align-items:baseline;
  gap:10px;
  padding:6px 0;
  color:inherit;
  text-decoration:none;
  border-bottom:1px solid var(--rule);
}
.index a:hover .index-label{color:var(--accent)}
.index a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.index-n{
  font-size:12px;
  color:var(--faint);
  min-width:1.6em;
}
.index-label{font-size:14px}
.index-count{margin-left:auto;font-size:12px;color:var(--faint)}

/* ── 分组 ─────────────────────────────────────────────── */
.group{margin-top:64px}
.group-head{
  display:flex;
  align-items:baseline;
  gap:14px;
  padding-bottom:10px;
  border-bottom:1px solid var(--ink);
}
.group-head h2{
  font-size:19px;
  font-weight:600;
  letter-spacing:.01em;
  margin:0;
}
.group-head .group-intent{
  font-size:12px;
  color:var(--faint);
}
.group-head .group-count{
  margin-left:auto;
  font-size:12px;
  color:var(--faint);
}

/* ── 图版网格：用发丝线分格，不用圆角卡片 ─────────────── */
.grid{
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(min(420px,100%),1fr));
  gap:0;
}
.spec{
  margin:0;
  padding:28px 0 32px;
  border-bottom:1px solid var(--rule-soft);
}
/* 两列时给奇数位右侧补一条竖发丝线 —— 与幻灯片里的分栏线同一种手段。
   断点跟 auto-fill 对齐：容器宽 = 视口 - 40px 的左右边距，两列要 ≥ 840px，
   即视口 ≥ 880px。写成 900px 会在 880–899 之间出现「两列但没有分隔线」。 */
@media (min-width:880px){
  .spec{padding-inline:26px}
  .spec:nth-child(odd){padding-left:0;border-right:1px solid var(--rule-soft)}
  .spec:nth-child(even){padding-right:0}
}

.shot{
  display:block;
  width:100%;
  height:auto;
  aspect-ratio:16/9;
  object-fit:contain;
  background:var(--slide);
  border:1px solid var(--rule);
}

.cap{padding-top:16px}
.cap-top{
  display:flex;
  align-items:baseline;
  gap:10px;
  margin-bottom:8px;
}
.cap-n{
  font-size:12px;
  color:var(--accent);
}
.cap-name{
  font-size:14.5px;
  font-weight:600;
  letter-spacing:-.01em;
}
.cap-variant{
  font-size:11px;
  color:var(--faint);
  border:1px solid var(--rule);
  padding:1px 6px;
}
.cap-sig{
  margin:0 0 10px;
  font-size:14px;
  color:var(--muted);
  line-height:1.65;
}
.cap-best{
  margin:0 0 14px;
  font-size:13px;
  color:var(--muted);
  line-height:1.6;
}
.cap-best b{
  font-weight:600;
  color:var(--faint);
  margin-right:6px;
  font-size:11.5px;
  letter-spacing:.08em;
}
.cap-meta{
  display:flex;
  flex-wrap:wrap;
  gap:6px 8px;
  align-items:center;
}
.chip{
  font-size:11.5px;
  color:var(--muted);
  border:1px solid var(--rule);
  padding:2px 8px;
  white-space:nowrap;
}
.cap-cap{
  margin-left:auto;
  font-size:11.5px;
  color:var(--faint);
  white-space:nowrap;
}

/* ── 页脚 ─────────────────────────────────────────────── */
.foot{
  margin-top:64px;
  padding-top:22px;
  border-top:1px solid var(--rule);
  font-size:13px;
  color:var(--muted);
  max-width:70ch;
}
.foot p{margin:0 0 10px}
.foot .mono{color:var(--ink);font-size:.92em}

@media (max-width:520px){
  .wrap{padding-block:36px 48px}
  .cap-cap{margin-left:0;width:100%}
}
"""


def _esc(s: str) -> str:
    return html.escape(s or '', quote=True)


def _chips(spec: layout_spec.LayoutSpec) -> str:
    if not spec.intents:
        return '<span class="chip">按位置插入</span>'
    return ''.join('<span class="chip">%s</span>' % _esc(_short_intent(i))
                   for i in spec.intents)


def _card(n: int, name: str, variant: int, img: str) -> str:
    meta = layout_spec.REGISTRY[name]        # 文案取自注册表，不取自 fixture
    rng = meta.item_range()
    variant_tag = ('<span class="cap-variant">变体 %d</span>' % (variant + 1)
                   if variant else '')
    return '''
        <figure class="spec">
          <img class="shot" src="%s" width="1280" height="720"
               alt="%s 版式样例">
          <figcaption class="cap">
            <div class="cap-top">
              <span class="cap-n mono">%02d</span>
              <span class="cap-name mono">%s</span>%s
            </div>
            <p class="cap-sig">%s</p>
            <p class="cap-best"><b>用在</b>%s</p>
            <div class="cap-meta">%s%s</div>
          </figcaption>
        </figure>''' % (
        _esc(img), _esc(name), n, _esc(name), variant_tag,
        _esc(meta.signature),
        _esc(meta.best_for),
        _chips(meta),
        '<span class="cap-cap mono">%s</span>' % _esc(rng) if rng else '',
    )


def write_html(out_dir: str, images: dict[int, str]) -> str:
    unknown = set(GROUP_LABELS) - ({''} | set(layout_spec.INTENTS))
    if unknown:
        raise SystemExit('GROUP_LABELS 里有 layout_spec.INTENTS 不认识的键：%s' % unknown)
    missing = [k for k in ('', *layout_spec.INTENTS) if k not in GROUP_LABELS]
    if missing:
        raise SystemExit('GROUP_LABELS 漏了意图：%s' % missing)

    sections = _sections()
    total = len(layout_spec.names())

    # 索引条
    nav = []
    for key, items in sections:
        nav.append('<a href="#g-%s"><span class="index-n mono">%02d</span>'
                   '<span class="index-label">%s</span>'
                   '<span class="index-count mono">%d</span></a>'
                   % (_esc(key or 'section'), items[0][0],
                      _esc(GROUP_LABELS[key]), len(items)))

    # 分组正文
    body = []
    for key, items in sections:
        cards = []
        for n, name, variant, _, fi in items:
            rel = os.path.relpath(images[FIRST_CONTENT_PAGE + fi],
                                  out_dir).replace('\\', '/')
            cards.append(_card(n, name, variant, rel))
        label = GROUP_LABELS[key]
        intent_note = ('<span class="group-intent mono">intent: %s</span>' % _esc(key)
                       if key else '<span class="group-intent">roles: section</span>')
        body.append('''
    <section class="group" id="g-%s">
      <div class="group-head">
        <h2>%s</h2>%s
        <span class="group-count mono">%d / %d</span>
      </div>
      <div class="grid">%s
      </div>
    </section>''' % (_esc(key or 'section'), _esc(label), intent_note,
                     len(items), total, ''.join(cards)))

    doc = '''<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>内容版式图鉴</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=Noto+Sans+SC:wght@400;500;700&display=swap">
<style>%s
</style>

<div class="wrap">

  <header>
    <p class="eyebrow mono">迈胜模板 · pptgen</p>
    <h1>内容版式图鉴</h1>
    <p class="lede">%d 套内容版式，每套一页真实样例。样例取自版式回归网
      <span class="mono">src/pptgen/samples.py</span> 的
      <span class="mono">FIXTURES</span> —— 与几何自检同一批 spec，
      所以每一页都保证框没给小、字没被静默截断。序号即
      <span class="mono">layout_spec.REGISTRY</span> 中的位置。</p>
    <nav class="index">%s
    </nav>
  </header>

  <main>%s
  </main>

  <footer class="foot">
    <p>版式清单、容量约束、意图映射都从
      <span class="mono">src/pptgen/layouts.py</span> 的
      <span class="mono">@layout(...)</span> 注册表派生，本页不另存一份。<br>
      样板 deck：<span class="mono">out/gallery/layouts.pptx</span> ——
      本机用 PowerPoint 打开，或 <span class="mono">python gallery.py</span> 重建。</p>
    <p>分组按每套版式的主职意图（<span class="mono">intents[0]</span>）归类。</p>
  </footer>

</div>
''' % (CSS, total, ''.join(nav), ''.join(body))

    path = os.path.join(out_dir, 'index.html')
    blob = doc.encode('utf-8')

    # 这页是**直接静态托管**的，没有平台帮忙注入 head。少了 charset，浏览器
    # 只能按系统区域猜 —— 中文 Windows 上猜 GBK，而文件是 UTF-8，于是整页乱码。
    # 字节全对、`file` 也报 UTF-8，只有浏览器渲染时才看得见，所以在这里挡死。
    if not doc.startswith('<meta charset="utf-8">'):
        raise SystemExit('index.html 开头必须是 <meta charset="utf-8">，否则中文乱码')
    if 'charset' not in doc[:200]:
        raise SystemExit('charset 声明不在 <head> 前 200 字节内，浏览器可能已开始解码')
    if blob.decode('utf-8') != doc:
        raise SystemExit('index.html 不是合法 UTF-8')

    with open(path, 'wb') as f:
        f.write(blob)
    print('已生成 %s（%.1f KB, UTF-8）' % (path, len(blob) / 1024))
    return path


def main():
    ap = argparse.ArgumentParser(description='版式图鉴 —— 样例渲成图 + 一张 HTML 页')
    ap.add_argument('--out', default=os.path.join(HERE, 'out', 'gallery'))
    ap.add_argument('--no-render', action='store_true',
                    help='复用已有 PNG，只重拼 HTML')
    a = ap.parse_args()

    config.load_env()
    out_dir = os.path.abspath(a.out)
    os.makedirs(os.path.join(out_dir, 'pages'), exist_ok=True)

    _, images = render_deck(out_dir, force=not a.no_render)
    write_html(out_dir, images)

    pages_dir = os.path.join(out_dir, 'pages')
    size = sum(os.path.getsize(os.path.join(pages_dir, f))
               for f in os.listdir(pages_dir) if f.endswith('.png'))
    print('图片合计 %.1f MB' % (size / 1024 / 1024))


if __name__ == '__main__':
    main()
