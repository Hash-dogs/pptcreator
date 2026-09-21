# -*- coding: utf-8 -*-
"""版式回归网：每套版式各渲染一页，逐页过 build 自检与几何检查。

为什么需要它：在这之前，20 个渲染函数里只有 `statement` 被间接触达，而且那条
断言检查的是 spec 字段、**不是真实渲染**（`test_structure.py`）。新增或修改版式
时，唯一的结构性防护是 `build.selfcheck()`（zip/rId 层面）与 `qa/geometry.py`
（包围盒层面），二者都不遍历 `LAYOUT_NAMES` —— 写错一个坐标不会有任何东西报警。

测三件事：
  1. 注册表与渲染器一一对应（`@layout` 元数据没漏、没有孤儿渲染函数）
  2. 每套版式的样例 spec 都**装得进**自己的容量声明（几何检查干净）
  3. 依赖模板的版式（`data_table` / 图表）在模板存在时才跑
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'src'))

from unittest import mock                                # noqa: E402

import pptx                                              # noqa: E402

from pptgen import build, layouts, layout_spec, pipeline, tokens  # noqa: E402
from pptgen import config                                # noqa: E402
from pptgen.qa import geometry                           # noqa: E402

SRC = 'Source: 《Dify 介绍与实战》§1.1'


def _kicker(t='01 初识 Dify'):
    return dict(kicker=t, source=SRC)


# 每套版式一份**按容量声明写**的样例。这些数字不是随手填的：它们贴着
# layout_spec 里该版式的 min/max_items 与 item_chars，所以几何检查必须干净。
FIXTURES = [
    ('section_divider', dict(
        layout='section_divider', num='03', title='Dify 能做什么',
        lead='三类应用场景、四大应用类型，以及产品版本与定价。', **_kicker())),

    ('statement', dict(
        layout='statement', kicker='开篇',
        lines=[[('把复杂流程留给平台，', {})],
               [('把', {}), ('创新', {'hl': True}), ('留给业务。', {})]],
        body=[[('Dify 是一个开源的大语言模型应用开发平台，'
                '融合后端即服务与 LLMOps 的理念。', {'size': 16, 'color': 'MUTED'})],
              [('它把构建 LLM 应用所需的技术栈一次性备齐。',
                {'size': 16, 'color': 'DARK', 'bold': True})]],
        source=SRC)),

    ('stat_hero', dict(
        layout='stat_hero', title='Dify 的位置：一组数字',
        hero=dict(num='50,186', unit=' 颗'),
        claim=[[('langgenius/dify 的 GitHub Star 数（2024-10-31）',
                 {'size': 16, 'color': 'DARK'})],
               [('从开源项目成长为生产级 LLM 应用平台的标志性节点。',
                 {'size': 16, 'color': 'MUTED'})]],
        stats=[dict(num='60,000+', label='GPTs 发布前就已创建的应用数'),
               dict(num='3 个', label='实战案例'),
               dict(num='14 种', label='知识库支持的文档格式')],
        **_kicker('转折点'))),

    ('kpi_grid', dict(
        layout='kpi_grid', title='一组指标同时看', columns=3,
        items=[dict(num='50,186', unit='颗', label='GitHub Star', note='2024-10-31'),
               dict(num='14', unit='种', label='文档格式', note='单文件 ≤15MB'),
               dict(num='200', unit='条', label='赠送额度', note='注册即赠'),
               dict(num='59', unit='$', label='专业版月费', note='或 $590 / 年'),
               dict(num='5,000', unit='条', label='月消息额度', note='专业版'),
               dict(num='50', unit='个', label='应用数量', note='专业版上限')],
        **_kicker('03 Dify 能做什么'))),

    ('definition', dict(
        layout='definition', title='什么是 Dify',
        term='Dify', formula='= Define  +  Modify',
        lead='持续地定义和改进你的 AI 应用。',
        body='平台内置构建 LLM 应用所需的完整技术栈：数百个模型的支持、'
             '直观的 Prompt 编排界面、高质量的 RAG 引擎、稳健的 Agent 框架与灵活的工作流。',
        aside=[('社区对它的描述：', {'size': 13, 'color': 'MUTED'}),
               ('简单、克制、快速迭代。', {'size': 13, 'color': 'DARK', 'bold': True})],
        **_kicker())),

    ('numbered_columns', dict(
        layout='numbered_columns', title='核心理念：九个关键词', columns=3,
        items=[dict(name='开源', desc='自由访问、修改与扩展平台功能'),
               dict(name='低代码', desc='可视化界面 + 模块化设计'),
               dict(name='模块化设计', desc='每个模块功能与接口清晰'),
               dict(name='全面模型支持', desc='无缝集成数百个模型'),
               dict(name='功能组件丰富', desc='覆盖原型到生产的全流程'),
               dict(name='可观测性', desc='LLMOps 监控日志与性能')],
        **_kicker())),

    ('tinted_bands', dict(
        layout='tinted_bands', title='三类典型应用场景',
        bands=[dict(name='创业团队', desc='用 Dify 构建 MVP 拿到投资，低代码特性大幅缩短从创意到产品的周期。'),
               dict(name='现有业务', desc='通过 RESTful API 把 AI 能力接入现有业务，实现 Prompt 与业务代码解耦。'),
               dict(name='大型企业', desc='作为企业内部 LLM 网关部署，加速生成式 AI 普及并实现集中治理。')],
        **_kicker('03 Dify 能做什么'))),

    ('quadrant', dict(
        layout='quadrant', title='AI 应用开发，难在哪？',
        items=[dict(name='数据获取与管理', desc='标注良好的数据集既昂贵又耗时，噪声与偏差会直接影响模型可靠性。'),
               dict(name='灵活部署与集成', desc='需要权衡可扩展性、响应时间与算力消耗，并建立反馈闭环。'),
               dict(name='数据隐私与安全', desc='既要保护隐私又要高效利用数据，同时防范对抗样本攻击。'),
               dict(name='持续更新与迭代', desc='需要实时监测系统性能，并根据新数据与新需求持续调整。')],
        **_kicker('02 为什么选 Dify'))),

    ('comparison_rows', dict(
        layout='comparison_rows', title='设计初衷：把复杂流程留给平台',
        col_a='传统方式', col_b='Dify 的方式',
        rows=[dict(dim='数据导入', a='每种格式都要单独对接，导入前先做格式转换',
                   b='支持多种数据格式无缝导入'),
              dict(dim='数据清洗', a='需要编写脚本处理噪声、缺失与偏差',
                   b='图形化界面完成数据的清洗与归类'),
              dict(dim='建模分析', a='手动设置与编程，建模耗时且结果不直观',
                   b='模块化流程，自动完成建模与可视化'),
              dict(dim='结果输出', a='结果分散在多份文件里，需多步操作',
                   b='提供多样化输出接口，自动生成报告')],
        **_kicker())),

    ('split_main_aside', dict(
        layout='split_main_aside', title='应用之外：五项辅助能力与版本对照',
        aside_title='版本对照',
        items=[dict(name='知识库管理', desc='支持文档导入与向量化，为应用提供专业领域知识'),
               dict(name='标注系统', desc='可人工编辑高质量答案，提升回复准确度'),
               dict(name='内容审核', desc='支持敏感词过滤，确保对话安全合规'),
               dict(name='数据分析', desc='提供详细的使用数据与效果分析')],
        aside_table=dict(header=['版本', '月费'],
                         rows=[['Sandbox', '免费'], ['Professional', '$59'],
                               ['Team', '$159']]),
        **_kicker('03 Dify 能做什么'))),

    ('split_main_aside(stats)', dict(
        layout='split_main_aside', title='价值（一）：开发效率',
        aside_title='关键数据',
        items=[dict(name='简化开发流程', desc='数据集管理、可视化编排与应用运营工具把复杂度收进平台'),
               dict(name='支持多种模型', desc='兼容 GPT 系列、Mistral、Llama 3 等多种厂商模型'),
               dict(name='模块化组件', desc='工作流、RAG 管道、智能代理覆盖从原型到生产全过程')],
        aside_stats=[dict(num='50,186', label='GitHub Star'),
                     dict(num='14 种', label='知识库文档格式'),
                     dict(num='200 条', label='注册赠送额度')],
        **_kicker())),

    ('phase_grouped_flow', dict(
        layout='phase_grouped_flow', title='8 节点工作流跑通内容生产线',
        phases=[dict(name='输入与标题',
                     nodes=['Start 收集主题/背景/语气',
                            '小红书标题 · chatgpt-4o-latest']),
                dict(name='正文与配图',
                     nodes=['小红书正文 · 爆款写作 Prompt',
                            '封面前言 · 封面文案',
                            'HTTP 生图 · POST 生成封面图']),
                dict(name='处理与输出',
                     nodes=['代码节点 · 提取封面图 URL',
                            '模板转换 · 组装输出',
                            'End 输出标题、正文与封面图'])],
        **_kicker('04 实战三 · 小红书运营一条龙'))),

    ('process_chain', dict(
        layout='process_chain', title='五步搭出第一个聊天助手',
        steps=[dict(num='01', name='创建应用', desc='登录 Dify Cloud\n选「基础编排」'),
               dict(num='02', name='编写提示词', desc='点右上角「生成」\n可让 AI 代写'),
               dict(num='03', name='配置模型', desc='选模型，调温度\nTop P / Top K'),
               dict(num='04', name='应用调试', desc='预览区检查输出\n按需调整参数'),
               dict(num='05', name='应用发布', desc='运行网页 / 嵌入\n网站 / 访问 API')],
        note=[('基础编排不修改内置提示，最适合新手起步', {'size': 15, 'color': 'DARK', 'bold': True})],
        **_kicker('04 实战一 · 聊天助手'))),

    ('timeline_vertical', dict(
        layout='timeline_vertical', title='用 Dify 搭企业知识库的七个步骤',
        steps=[dict(name='整理文档', desc='把内部资料整理成文档'),
               dict(name='创建知识库', desc='打开知识库页面并上传文档'),
               dict(name='文本分段', desc='右侧可预览分段情况'),
               dict(name='索引方式', desc='高质量或经济两档'),
               dict(name='编写 Prompt', desc='把需求优化成结构化提示词'),
               dict(name='引用知识库', desc='上下文 → 添加 → 选中知识库'),
               dict(name='调试与发布', desc='对比助手输出与文档原文')],
        **_kicker('04 实战二 · 企业知识库'))),

    ('node_flow', dict(
        layout='node_flow', title='工作流总览：8 个节点跑通生产线',
        nodes=['Start 收集', '生成标题', '生成正文', '生成前言',
               '生成封面图', '取图片 URL', '组装结果', 'End 结束'],
        note='可视化节点编排：LLM 生成 + HTTP 生图 + 代码处理 + 模板组装。',
        **_kicker('04 实战三'))),

    ('layered_stack', dict(
        layout='layered_stack', title='Dify 的技术栈分层',
        layers=[dict(name='接入层', modules=['WebApp', 'API', '嵌入网站']),
                dict(name='编排层', modules=['Prompt 编排', '工作流', 'Agent 框架']),
                dict(name='能力层', modules=['RAG 引擎', '模型管理', '插件系统']),
                dict(name='基础层', modules=['向量库', '对象存储', '可观测性'])],
        note='每一层都可以单独替换，业务代码不必跟着改。',
        **_kicker('03 Dify 能做什么'))),

    ('data_table', dict(
        layout='data_table', title='四大应用类型与代表案例',
        header=['类型', '形态与能力', '典型场景', '代表案例'],
        col_widths=[1.85, 3.60, 3.00, 2.88],
        rows=[['文本生成型', '表单 + 结果，支持流式返回', '翻译、改写、摘要', 'Copy.ai、Jasper'],
              ['对话型', '多轮对话，具备对话记忆', '智能客服、教育辅导', 'Ada、Duolingo'],
              ['Agent 助手', '配置工具，自主完成任务分解', '财务分析、报告撰写', 'AutoGPT'],
              ['工作流应用', '多步骤对话与批处理自动化', '复杂业务流程', 'n8n.io']],
        **_kicker('03 Dify 能做什么'))),

    ('metric_trend', dict(
        layout='metric_trend', title='GitHub Star 的增长曲线',
        chart=dict(type='line', labels=['2023-03', '2023-09', '2024-03', '2024-06', '2024-10'],
                   series=[dict(name='Star 数', values=[12000, 24000, 38000, 45000, 50186])]),
        takeaways=[[('两年内从 1.2 万增长到 5 万颗，进入开源项目第一梯队。',
                     {'size': 13, 'color': 'MUTED'})],
                   [('2024 年增速放缓，进入平台期。', {'size': 13, 'color': 'MUTED'})]],
        **_kicker('转折点'))),

    ('progress_checklist', dict(
        layout='progress_checklist', title='落地进展与风险', progress='进度',
        items=[dict(name='文档整理', desc='内部资料已按 Q&A 形式重写', status='done', progress='100%'),
               dict(name='知识库搭建', desc='14 种格式均已验证可导入', status='done', progress='100%'),
               dict(name='聊天助手上线', desc='基础编排已跑通，正在调提示词', status='doing', progress='60%'),
               dict(name='工作流编排', desc='8 节点流程已串通，待压测', status='doing', progress='40%'),
               dict(name='权限与合规', desc='私有化部署方案尚未通过评审', status='risk'),
               dict(name='全员培训', desc='等前三项稳定后启动', status='todo')],
        **_kicker('05 落地要点'))),

    ('executive_summary', dict(
        layout='executive_summary', title='落地要点与下一步',
        thesis='复杂度随业务需要渐进，而不是一次到位。',
        points=[dict(num='01', text='第一步：参照官方文档跑通构建流程，先做出一个能用的聊天助手。'),
                dict(num='02', text='第二步：接入企业知识库，让回答有据可依，把散落的文档变成可检索资产。'),
                dict(num='03', text='第三步：用工作流把重复的内容生产串成流水线，让 AI 进入日常业务。'),
                dict(num='04', text='第四步：建立效果评估与提示词迭代机制，避免上线即停滞。')],
        **_kicker('05 落地要点'))),

    ('quote', dict(
        layout='quote', kicker='05 落地要点',
        quote=[[('复杂度随业务需要', {}), ('渐进', {'hl': True}), ('，', {})],
               [('而不是一次到位。', {})]],
        attribution='—— 基础编排起步 → RAG 增强 → 可视化工作流',
        body=[[('第一步', {'size': 16, 'color': 'DARK', 'bold': True}),
               ('  参照官方文档跑通构建流程。', {'size': 16, 'color': 'MUTED'})],
              [('第二步', {'size': 16, 'color': 'DARK', 'bold': True}),
               ('  接入企业知识库，让回答有据可依。', {'size': 16, 'color': 'MUTED'})]],
        source=SRC)),
]


def _template():
    p = config.template_path()
    return p if os.path.isfile(p) else None


class TestRegistry(unittest.TestCase):
    def test_renderers_and_specs_match(self):
        """每个渲染函数都有元数据，每条元数据都有渲染函数。"""
        self.assertEqual(sorted(layouts.LAYOUTS), sorted(layout_spec.REGISTRY))

    def test_twenty_layouts(self):
        self.assertEqual(len(layouts.LAYOUTS), 20, '版式数应为 20')

    def test_layout_spec_alone_is_not_empty(self):
        """只 import layout_spec 也该看到 20 套版式。

        注册是 layouts.py 的 `@layout` 装饰器做的，所以这里靠 `_ensure_loaded()`
        把那次 import 推迟到第一次访问 —— 否则「必须先 import layouts」会变成
        一条隐形约定，调用方拿到空目录还找不到原因。
        """
        import subprocess
        code = ('import sys; sys.path.insert(0, %r);'
                'from pptgen import layout_spec;'
                'print(len(layout_spec.names()), len(layout_spec.candidates("content","status")))'
                % os.path.join(ROOT, 'src'))
        out = subprocess.run([sys.executable, '-c', code],
                             capture_output=True, encoding='utf-8')
        self.assertEqual('20 1', (out.stdout or '').strip(), out.stderr)

    def test_every_layout_is_reachable_by_intent(self):
        """每套版式都要能被某个 (role, intent) 组合选到，否则它永远不会被使用。"""
        for name, sp in layout_spec.REGISTRY.items():
            hit = False
            for role in sp.roles:
                for intent in (sp.intents or ('',)):
                    got = layout_spec.candidates(role, intent)
                    if any(x.name == name for x in got):
                        hit = True
                        break
                if hit:
                    break
            self.assertTrue(hit, '%s 没有任何 (role, intent) 能选到它' % name)

    def test_intents_are_known(self):
        for sp in layout_spec.REGISTRY.values():
            for it in sp.intents:
                self.assertIn(it, layout_spec.INTENTS, sp.name)
            for r in sp.roles:
                self.assertIn(r, layout_spec.ROLES, sp.name)

    def test_fallbacks_exist(self):
        for sp in layout_spec.REGISTRY.values():
            for fb in sp.fallback:
                self.assertIn(fb, layout_spec.REGISTRY,
                              '%s 的降级目标 %s 不存在' % (sp.name, fb))

    def test_catalog_and_capacity_nonempty(self):
        for sp in layout_spec.REGISTRY.values():
            self.assertTrue(sp.catalog.strip(), '%s 缺 catalog' % sp.name)
            self.assertTrue(sp.capacity.strip(), '%s 缺 capacity' % sp.name)

    def test_catalog_text_lists_every_layout(self):
        txt = layout_spec.catalog_text()
        for name in layouts.LAYOUT_NAMES:
            self.assertIn(name, txt)


class TestCapacityFilter(unittest.TestCase):
    """容量前置校验：这是「标签被静默截断」的事前防线。"""

    @staticmethod
    def _shape(n=None, table=False, numbers=False, item=0, total=0):
        return dict(known=True, n_items=n or 0, has_table=table,
                    has_numbers=numbers, max_item_chars=item, total_chars=total)

    def test_source_item_length_does_not_filter_candidates(self):
        """**源文档条目的长度不能用来筛候选。**

        源条目天然是一整句（实测 40–120 字），而版式的单条预算是针对**成品**的
        （22 字）—— 模型的工作正是把长句压短。早先拿源条目长度去卡候选，20 套被
        筛得只剩 statement，13 页内容全塌成一种版式。单条字数改为事后校验成品：
        见 `pipeline.overflow_reason`。
        """
        for item in (25, 80, 200):
            got = [c.name for c in layout_spec.candidates(
                'content', 'process', self._shape(n=8, item=item, total=900))]
            self.assertIn('phase_grouped_flow', got,
                          '源条目 %d 字不该把候选筛光' % item)

    def test_overflow_reason_flags_long_node_labels(self):
        """成品超容量要被拦下来 —— 这正是 `_fit()` 静默截断的事前防线。

        实测 `node_flow` 那 8 个节点里 6 个被截成 `小红书正文 · 爆款写作…`，
        而几何报告是干净的（截断消除了溢出）。
        """
        long_nodes = dict(layout='node_flow',
                          nodes=['小红书标题 · chatgpt-4o-latest'] * 8)
        self.assertIn('单条', pipeline.overflow_reason(long_nodes))

    def test_overflow_reason_passes_short_labels(self):
        ok = dict(layout='node_flow', nodes=['Start 收集', '生成标题', '生成正文',
                                             '生成前言', '生成封面图', '取 URL',
                                             '组装结果', 'End 结束'])
        self.assertEqual('', pipeline.overflow_reason(ok))

    def test_overflow_reason_flags_too_many_items(self):
        many = dict(layout='quadrant',
                    items=[dict(name='维度%d' % i, desc='说明') for i in range(5)])
        self.assertIn('超过上限', pipeline.overflow_reason(many))

    def test_five_metrics_rule_out_stat_hero(self):
        got = [c.name for c in layout_spec.candidates(
            'content', 'quantitative', self._shape(n=5, numbers=True, item=8))]
        self.assertNotIn('stat_hero', got)
        self.assertIn('kpi_grid', got)

    def test_quadrant_needs_exactly_four(self):
        for n in (3, 5):
            got = [c.name for c in layout_spec.candidates(
                'content', 'enumeration', self._shape(n=n, item=20))]
            self.assertNotIn('quadrant', got, 'n_items=%d 不该选象限' % n)

    def test_chart_needs_numbers(self):
        got = [c.name for c in layout_spec.candidates(
            'content', 'quantitative', self._shape(n=3, numbers=False, item=20))]
        self.assertNotIn('metric_trend', got)

    def test_unknown_shape_does_not_kill_everything(self):
        """anchor 取不到素材时 shape 全 0 —— 那是「没取到」，不是「内容为空」。"""
        got = layout_spec.candidates('content', 'enumeration', layout_spec.EMPTY_SHAPE)
        self.assertIn('numbered_columns', [c.name for c in got])


def _doc():
    """一份最小可用的 doc：两章、每章两页、页内各 4 条要点。"""
    blocks, chapters = [], []
    for ci, cname in enumerate(['01 第一章', '02 第二章']):
        blocks.append(dict(type='heading', level=1, text=cname,
                           role='divider', slide=ci * 3 + 1))
        pages = []
        for pi in (1, 2):
            pname = '页面 %d-%d' % (ci + 1, pi)
            start = len(blocks)
            blocks.append(dict(type='heading', level=2, text=pname, slide=ci * 3 + pi))
            for k in range(4):
                blocks.append(dict(type='bullets', slide=ci * 3 + pi,
                                   items=['要点 %d：一句说明文字' % k]))
            pages.append(dict(name=pname, start=start, end=len(blocks),
                              lead='', chars=40))
        chapters.append(dict(key='%02d' % (ci + 1), name=cname, subtitle='',
                             start=0, end=len(blocks), chars=80, pages=pages))
    return dict(source='t.pptx', kind='pptx', title='测试文档', blocks=blocks,
                structure=dict(chapters=chapters, method='divider', front_matter=[]))


class TestRichTextTolerance(unittest.TestCase):
    """富文本的写法得容错 —— 渲染层不该崩在模型输出上。

    实测模型对同一个字段给出过三种形态，第三种和第二种都会让渲染直接抛异常：
        [("文本", {"hl": true})]、[["文本", {"hl": true}]]、
        [{"text": "文本", "hl": true}]
    """

    def _text(self, paras):
        return ['|'.join(str(t) for t, _ in p) for p in paras]

    def test_standard_tuple_runs(self):
        got = layouts._paras([[('把', {}), ('创新', {'hl': True})]])
        self.assertEqual(['把|创新'], self._text(got))
        self.assertEqual(layouts.RED, got[0][1][1]['color'])
        self.assertTrue(got[0][1][1]['bold'])

    def test_single_paragraph_written_with_lists(self):
        """`[["文本", {"hl": true}]]` —— 用 list 而非 tuple。"""
        got = layouts._paras([['GitHub 60,000+ Star', {'hl': True}]])
        self.assertEqual(['GitHub 60,000+ Star'], self._text(got))
        self.assertEqual(layouts.RED, got[0][0][1]['color'])

    def test_runs_written_as_dicts(self):
        """`[{"text": …, "hl": …}]` —— 用 dict 描述 run。"""
        got = layouts._paras([[{'text': '定位：', 'hl': False},
                               {'text': '开源平台', 'hl': True}]])
        self.assertEqual(['定位：|开源平台'], self._text(got))
        self.assertEqual(layouts.RED, got[0][1][1]['color'])

    def test_bare_strings_are_separate_paragraphs(self):
        got = layouts._paras(['第一段', '第二段'])
        self.assertEqual(['第一段', '第二段'], self._text(got))

    def test_nested_paragraphs(self):
        got = layouts._paras([[{'text': 'a', 'hl': False}], [{'text': 'b', 'hl': False}]])
        self.assertEqual(['a', 'b'], self._text(got))

    def test_bare_string_and_dict(self):
        self.assertEqual(['一段'], self._text(layouts._paras('一段')))
        self.assertEqual(['一段'], self._text(layouts._paras({'text': '一段'})))

    def test_empty_is_empty(self):
        self.assertEqual([], layouts._paras([]))


class TestPlanStage(unittest.TestCase):
    """意图 → 候选 → 护栏。这一层决定「版式选得贴不贴内容」。"""

    @classmethod
    def setUpClass(cls):
        cls.doc = _doc()

    def _outline(self):
        # 强制走确定性路径：不配置文本模型
        with mock.patch.object(config, 'llm_config', lambda: None):
            return pipeline.make_outline(self.doc)

    def _plan(self):
        with mock.patch.object(config, 'llm_config', lambda: None):
            return pipeline.make_plan(self._outline(), self.doc)

    def test_dividers_only_when_budget_allows(self):
        """分隔页要占页数预算，装不下就完全不插（而不是砍正文）。"""
        n, lo, hi = pipeline._divider_budget(self.doc, 6, 10)
        self.assertEqual((2, 6, 8), (n, lo, hi))
        # 预算太紧（hi < 章数 × 2）→ 一页都不插
        self.assertEqual(0, pipeline._divider_budget(self.doc, 2, 3)[0])
        # 开关关掉 → 一页都不插
        with mock.patch.object(config, 'section_dividers', lambda: False):
            self.assertEqual(0, pipeline._divider_budget(self.doc, 6, 12)[0])

    def test_dividers_inserted_at_each_chapter_head(self):
        out = self._outline()
        self.assertEqual(2, out['divider_count'])
        for s in out['sections']:
            self.assertTrue(s['pages'][0].get('divider'), s['name'])
            self.assertEqual('section', s['pages'][0]['intent'])
        # 分隔页不计入正文页数
        self.assertEqual(4, out['page_count'])
        self.assertEqual(6, out['total_pages'])

    def test_every_page_gets_an_intent(self):
        out = self._outline()
        for s in out['sections']:
            for p in s['pages']:
                if p.get('divider'):
                    self.assertEqual('section', p['intent'])   # 结构页不走内容意图
                    continue
                self.assertIn(p.get('intent'), layout_spec.INTENTS, p['title'])

    def test_plan_has_no_adjacent_duplicate(self):
        """相邻页不得同版式 —— 实测这份 deck 的第 12、13 页是相邻的两个 node_flow。"""
        plan = self._plan()
        names = [s['layout'] for s in plan['slides']]
        dup = [i for i in range(1, len(names)) if names[i] == names[i - 1]]
        self.assertEqual([], dup, names)

    def test_dividers_survive_planning(self):
        plan = self._plan()
        divs = [s for s in plan['slides'] if s['layout'] == 'section_divider']
        self.assertEqual(2, len(divs))
        # 隔断页不给 kicker：它自己就是章节名，补一行小字等于写两遍
        for d in divs:
            self.assertFalse(d.get('kicker'))
            self.assertTrue(d.get('title'))

    def test_intent_maps_to_matching_layouts(self):
        """意图 → 候选：这是「选得贴内容」的执行点。"""
        n = dict(known=True, n_items=4, has_table=False, has_numbers=True,
                 max_item_chars=20, total_chars=120)
        want = {
            'status': 'progress_checklist',
            'hierarchy': 'layered_stack',
            'summary': 'executive_summary',
            'quote': 'quote',
            'comparison': 'comparison_rows',
            'quantitative': 'kpi_grid',
            'definition': 'definition',
        }
        for intent, layout in want.items():
            got = [c.name for c in layout_spec.candidates('content', intent, n)]
            self.assertIn(layout, got, 'intent=%s 的候选里没有 %s' % (intent, layout))

    def test_intent_filter_actually_filters(self):
        """意图不同的两页，候选集不该一样 —— 否则等于没筛。"""
        n = dict(known=True, n_items=4, has_table=False, has_numbers=True,
                 max_item_chars=20, total_chars=120)
        a = {c.name for c in layout_spec.candidates('content', 'status', n)}
        b = {c.name for c in layout_spec.candidates('content', 'process', n)}
        self.assertNotEqual(a, b)

    def test_section_role_only_gets_structure_layouts(self):
        got = [c.name for c in layout_spec.candidates('section', 'section')]
        self.assertEqual(['section_divider'], got)

    def test_named_layouts_actually_render(self):
        """每套版式的成品定义都能过容量自检（拿目录里的样例反查）。"""
        for name, spec in FIXTURES:
            self.assertEqual('', pipeline.overflow_reason(spec),
                             '%s 的样例自己就超容量' % name)


class TestRenderAll(unittest.TestCase):
    """每套版式渲染一页 → build 自检 + 几何检查。"""

    @classmethod
    def setUpClass(cls):
        tpl = _template()
        if tpl is None:
            raise unittest.SkipTest('模板文件不存在，跳过渲染回归')
        cls.tpl = tpl
        cls.tmp = tempfile.mkdtemp(prefix='pptgen-layouts-')
        slides = [dict(spec) for _, spec in FIXTURES]
        cls.path = os.path.join(cls.tmp, 'layouts.pptx')
        build.build(dict(slides=slides, toc=['01 初识 Dify']), tpl, cls.path)
        cls.rep = geometry.analyse(cls.path)

    def test_fixtures_cover_every_layout(self):
        covered = {s['layout'] for _, s in FIXTURES}
        self.assertEqual(covered, set(layouts.LAYOUT_NAMES),
                         '有版式没有样例：%s' % (set(layouts.LAYOUT_NAMES) - covered))

    def test_no_geometry_errors(self):
        errs = [i for i in self.rep['issues'] if i['severity'] == 'error']
        self.assertEqual([], errs, geometry.format_report(self.rep))

    def test_no_text_overflow(self):
        """样例 spec 是贴着容量声明写的，溢出说明版式自身的框给小了。"""
        bad = [i for i in self.rep['issues']
               if i['kind'] in ('text_overflow', 'text_overlap')]
        self.assertEqual([], bad, geometry.format_report(self.rep))

    def test_no_silent_truncation(self):
        """样例的内容都该放得下 —— 出现截断说明框宽/框高估错了。

        `_fit()` 的截断会消除溢出，所以几何检查看不见它（实测 node_flow 8 个
        节点里 6 个被截成残句而报告全绿）。这条断言把静默截断变成可见的失败。
        """
        from pptx import Presentation
        prs = Presentation(self.path)
        for slide in list(prs.slides)[2:-1]:      # 去掉公司封面与封底
            for sh in slide.shapes:
                if sh.has_text_frame:
                    for p in sh.text_frame.paragraphs:
                        for r in p.runs:
                            self.assertNotIn('…', r.text,
                                             '出现截断：%r' % r.text)


class TestWrapAndFit(unittest.TestCase):
    """标题的折行原语：**该折行就折行，别截断**。

    背景：模型把源文档的「章名　—　副题」整串当章节名（30+ 字），分隔页 40pt
    的大字框放不下，早先被 `_fit()` 截成 `01 初识 Dify　—　什么是 Di…`。
    而大纲是**可以被用户改长的**，所以渲染层必须自己会折行。
    """

    def test_long_divider_title_wraps_instead_of_truncating(self):
        title = '01 初识 Dify　—　什么是 Dify · 设计初衷 · 九大核心理念'
        tokens.take_truncations()
        lines, size = tokens.fit_block(title, 7.73, 1.60, sizes=(40, 36, 32, 28, 24))
        self.assertEqual([], tokens.take_truncations(), '折行不该产生截断')
        self.assertLessEqual(len(lines), 2)
        # 折行只在断点处吃掉空格，文字本身一个都不能少
        self.assertEqual(title.replace(' ', ''), ''.join(lines).replace(' ', ''))
        for ln in lines:
            self.assertNotIn('…', ln)
            self.assertLessEqual(tokens.text_w_in(ln, size), 7.73)

    def test_short_title_stays_one_line(self):
        _, size = tokens.fit_block('01 初识 Dify', 7.73, 1.60,
                                   sizes=(40, 36, 32, 28, 24))
        self.assertEqual(40, size)

    def test_bigger_type_beats_fewer_lines(self):
        """**字号优先**：能在两行内放下就用最大的字号，不为了一行把字压小。

        14 个汉字在 40pt 下差 0.44" 放不进一行 —— 这时取「40pt 折两行」，
        而不是「26pt 挤一行」（分隔页要靠大字号压住版面）。
        """
        lines, size = tokens.fit_block('一二三四五六七八九十十一十二十三十四',
                                       7.73, 1.60, sizes=(40, 36, 32))
        self.assertEqual(40, size)
        self.assertEqual(2, len(lines))

    def test_line_never_starts_with_punctuation(self):
        """行首不挂避头标点（`、` `，` `·`）。标点宁可吊在上一行末尾。"""
        text = '从三类落地场景到四类应用形态、五项辅助能力与三档版本定价'
        for ln in tokens.wrap_lines(text, 7.73, 36):
            self.assertNotIn(ln[0], tokens._NO_LINE_START, ln)

    def test_latin_word_is_not_split(self):
        for ln in tokens.wrap_lines('平台对比 GPT-4o 与 langgenius/dify 的能力', 4.2, 20):
            self.assertNotIn('Dif\n', ln + '\n')
            self.assertFalse(ln.endswith('langge'), ln)

    def test_absurd_title_truncates_and_records(self):
        """阶梯全试完（用户把标题改到极端长度）才截断，而且要**记下来**。"""
        tokens.take_truncations()
        lines, _ = tokens.fit_block('标' * 200, 7.73, 1.60,
                                    sizes=(40, 36, 32, 28, 24))
        self.assertEqual(2, len(lines))
        self.assertTrue(lines[-1].endswith('…'))
        cuts = tokens.take_truncations()
        self.assertEqual(1, len(cuts))
        self.assertEqual('标' * 200, cuts[0][0], '记录里要有完整原文')


class TestCoverAndAgenda(unittest.TestCase):
    """封面填标题 + 日期，目录页填真正的目录 —— 都**不覆盖模板自己的版式**。

    `spec['title']` 从 pipeline 一路传到 build 却从来没人消费，成品第一页
    只有一个 MEVION 机器图；`fill_agenda` 则把标题写成 28pt `DARK`，
    压在同色的深灰通栏上几乎看不见。
    """

    @classmethod
    def setUpClass(cls):
        tpl = _template()
        if tpl is None:
            raise unittest.SkipTest('模板文件不存在，跳过渲染回归')
        cls.tpl = tpl
        cls.tmp = tempfile.mkdtemp(prefix='pptgen-cover-')
        slides = [dict(FIXTURES[0][1]), dict(FIXTURES[1][1])]

        def make(name, title, toc):
            path = os.path.join(cls.tmp, name + '.pptx')
            build.build(dict(slides=[dict(s) for s in slides], toc=toc,
                             title=title), tpl, path)
            return pptx.Presentation(path)

        cls.prs = make('normal', 'Dify 介绍与实战：从初识到落地',
                       ['01 初识 Dify —— 什么是 Dify、设计初衷与九大核心理念',
                        '02 为什么选 Dify —— 挑战与价值'])
        cls.long = make('long', '一个非常长的标题' * 3, ['01 很长的章节名' * 4])
        cls.absurd = make('absurd', '一个非常长的标题' * 6, [])
        cls.empty = make('empty', '', [])

    def _ph(self, prs, page, idx):
        for sh in prs.slides[page].shapes:
            if sh.is_placeholder and sh.placeholder_format.idx == idx:
                return sh
        return None

    def test_cover_gets_the_deck_title(self):
        tf = self._ph(self.prs, 0, 0).text_frame
        self.assertEqual('Dify 介绍与实战：从初识到落地',
                         tf.text.replace('\n', ''))

    def test_cover_subtitle_is_the_date(self):
        got = self._ph(self.prs, 0, 1).text_frame.text.strip()
        self.assertRegex(got, r'^\d{4} 年 \d{1,2} 月$')

    def test_long_cover_title_wraps_without_ellipsis(self):
        """24 字的标题在封面上折两行放得下 —— 完全不该出现省略号。"""
        tf = self._ph(self.long, 0, 0).text_frame
        self.assertEqual(2, len(tf.paragraphs))
        self.assertNotIn('…', tf.text)
        self.assertEqual('一个非常长的标题' * 3, tf.text.replace('\n', ''))
        self.assertEqual(32, tf.paragraphs[0].runs[0].font.size.pt)

    def test_absurd_cover_title_truncates_but_says_so(self):
        """48 字（远超封面能放下的量）才截断，而且**记录在案**，不是静默的。"""
        tf = self._ph(self.absurd, 0, 0).text_frame
        self.assertEqual(2, len(tf.paragraphs))
        self.assertTrue(tf.text.endswith('…'))

    def test_cover_keeps_the_template_typography(self):
        """只设字号，颜色/对齐/字体全部继承 layout（模板封面是深色居中大字）。"""
        for p in self._ph(self.prs, 0, 0).text_frame.paragraphs:
            for r in p.runs:
                self.assertIsNotNone(r.font.size)
                self.assertIsNone(r.font.color.rgb if r.font.color
                                  and r.font.color.type is not None else None)
        # 副标题一个字号都不该设：24pt 加粗浅灰是 layout 给的
        for p in self._ph(self.prs, 0, 1).text_frame.paragraphs:
            for r in p.runs:
                self.assertIsNone(r.font.size)

    def test_agenda_gets_the_toc(self):
        tf = self._ph(self.prs, 1, 0).text_frame
        self.assertEqual('目录', tf.text)
        body = self._ph(self.prs, 1, 12).text_frame
        self.assertEqual(['01 初识 Dify —— 什么是 Dify、设计初衷与九大核心理念',
                          '02 为什么选 Dify —— 挑战与价值'],
                         [p.text for p in body.paragraphs])

    def test_agenda_keeps_the_template_typography(self):
        """目录标题（深灰通栏上的 40pt 白字）与条目（24pt + 品牌红圆点）
        都来自 layout —— 一个显式字号都不该有，否则就会重演「深色压深色」。"""
        for idx in (0, 12):
            for p in self._ph(self.prs, 1, idx).text_frame.paragraphs:
                for r in p.runs:
                    self.assertIsNone(r.font.size, '不应覆盖模板字号')
                    self.assertIsNone(r.font.color.rgb if r.font.color
                                      and r.font.color.type is not None else None)

    def test_template_sample_text_never_survives(self):
        """目录为空也要清空占位符 —— 否则成品上留着「议题一/议题二/议题三」。"""
        for idx in (0, 12):
            got = self._ph(self.empty, 1, idx).text_frame.text
            for junk in ('议题', '会议议程', 'Agenda Items'):
                self.assertNotIn(junk, got)

    def test_no_title_leaves_the_cover_alone(self):
        """spec 里没有 title（`--content <老模块>` 那条路径）就别动封面。"""
        self.assertEqual('', self._ph(self.empty, 0, 0).text_frame.text)


class TestTruncationIsReported(unittest.TestCase):
    """截断记录要真的被收走 —— 早先 `take_truncations()` 全仓库无人调用，
    「记下来写进日志」是一句假注释，长驻进程里还会跨 job 累积。"""

    def test_build_reports_truncations_through_on_log(self):
        tpl = _template()
        if tpl is None:
            raise unittest.SkipTest('模板文件不存在')
        tmp = tempfile.mkdtemp(prefix='pptgen-trunc-')
        # 来源行走 `fit_one_line`（单行定高 0.30"），长到一定程度必然被截断。
        spec = dict(FIXTURES[1][1])
        spec['source'] = 'Source: 《' + '很长的来源说明' * 8 + '》'
        seen = []
        build.build(dict(slides=[spec], toc=[]), tpl,
                    os.path.join(tmp, 'x.pptx'), on_log=seen.append)
        self.assertTrue(any('截断' in m for m in seen), seen)
        self.assertTrue(any('很长的来源说明' in m for m in seen), seen)


if __name__ == '__main__':
    unittest.main()
