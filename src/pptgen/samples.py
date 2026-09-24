# -*- coding: utf-8 -*-
"""版式样例 spec —— 回归网、版式图鉴、页面预览共用的一批。

每套版式一份**按容量声明写**的样例：这些数字不是随手填的，它们贴着
`layout_spec` 里该版式的 `min/max_items` 与 `item_chars`，所以几何检查必须干净。

这份清单原先放在 `tests/test_layouts.py` 里，`gallery.py` 靠往 `sys.path` 里插
`tests/` 才 import 得到它。Web 端要展示内置版式的预览图时，同一个需求出现了第三次 ——
所以上提到 `src/`，三处共用一份：

    tests/test_layouts.py   TestRenderAll 拿它跑几何回归
    gallery.py              渲成 `out/gallery/` 的图鉴页
    server.py               渲成版式管理页的内置版式预览

**新增版式必须同时在这里加一份样例**，否则 `TestRenderAll.test_fixtures_cover_every_layout`
会红 —— 这条断言是「版式清单与样例清单不许漂移」的守卫。同理，新增了样例但几何检查
不干净，`test_no_geometry_errors` / `test_no_text_overflow` / `test_no_silent_truncation`
会先红，图鉴跟着回归走，不会与版式漂移。
"""

def _kicker(t='01 初识 Dify'):
    return dict(kicker=t)


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
                {'size': 16, 'color': 'DARK', 'bold': True})]])),

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

    # 时间线两条：7 步守「奇数条时左侧多一个」的左右分配，8 步守条数上限
    # （8 步时 ystep 被压到 0.569"，是全库最挤的一页 —— 溢出一旦发生就在这里）。
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

    ('timeline_vertical(n=8)', dict(
        layout='timeline_vertical', title='八步走完内容生产流水线',
        steps=[dict(name='选题', desc='从评论与搜索词里挑题'),
               dict(name='收集素材', desc='抓取竞品笔记与图片'),
               dict(name='拟定标题', desc='生成五个备选标题'),
               dict(name='撰写正文', desc='按结构生成正文草稿'),
               dict(name='生成配图', desc='按标题生成封面图'),
               dict(name='取图 URL', desc='上传后回填图片地址'),
               dict(name='组装结果', desc='标题正文封面合并成稿'),
               dict(name='发布归档', desc='推送至草稿箱并记录')],
        **_kicker('04 实战三 · 内容流水线'))),

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
               ('  接入企业知识库，让回答有据可依。', {'size': 16, 'color': 'MUTED'})]])),
]


def for_layout(name: str) -> dict | None:
    """取某套版式的**第一份**样例 spec（`quadrant` 这类只有一份，够用）。

    自定义版式的试片与内置版式的预览图都靠它 —— 这两条路都要「拿一份样例渲一页」，
    与其各写一份挑选逻辑，不如共用这一个。找不到返回 None。

    返回的是**副本**：调用方（`build.build`）会往 spec 里塞 `page` 键，
    直接给出原对象会把那份样例改脏，回归网下一轮就跑在被污染的数据上。
    """
    for _, spec in FIXTURES:
        if spec.get('layout') == name:
            return dict(spec)
    return None
