# -*- coding: utf-8 -*-
"""《Dify 介绍与实战》内容 spec —— 由源 deck 的 25 页提炼为 17 页。

内容全部来自 `Dify 介绍与实战.pptx` 与 `Dify 介绍与实战.pdf`，
页码由 build 自动分配，此处不写 `page`。
"""

# 封面标题。`run.py build --content dify` 这条路以前没有 title，
# 于是封面两个占位符一直是空的 —— 成品第一页只有一个 MEVION 机器图。
TITLE = 'Dify 介绍与实战'

TOC = [
    '01  初识 Dify —— 什么是 Dify、设计初衷与九大核心理念',
    '02  为什么选 Dify —— AI 应用开发的四大挑战与七项价值',
    '03  Dify 能做什么 —— 三类场景、四大应用类型与版本对比',
    '04  开发实战 —— 聊天助手、企业知识库、小红书工作流',
    '05  落地要点与下一步',
]

SLIDES = [

    # ── 01 开篇 ─────────────────────────────────────────────
    dict(layout='statement', kicker='开篇',
         lines=[[('把复杂流程留给平台，', {})],
                [('把', {}), ('创新', {'hl': True}), ('留给业务。', {})]],
         body=[
             [('Dify 是一个开源的大语言模型（LLM）应用开发平台，融合后端即服务（BaaS）与 LLMOps 的理念。',
               {'size': 16, 'color': 'MUTED'})],
             [('它把构建 LLM 应用所需的技术栈一次性备齐——数百个模型的支持、Prompt 编排界面、'
               'RAG 引擎、Agent 框架与灵活的工作流。', {'size': 16, 'color': 'MUTED'})],
             [('目标是成为一个让用户少写代码、少动手的智能化平台。',
               {'size': 16, 'color': 'DARK', 'bold': True})],
         ],
         source='Source: Dify 介绍与实战 · §1.1'),

    # ── 02 关键数字 ─────────────────────────────────────────
    dict(layout='stat_hero', kicker='转折点', title='Dify 的位置：一组数字',
         hero=dict(num='50,186', unit=' 颗'),
         claim=[
             [('langgenius/dify 的 GitHub Star 数（2024-10-31）', {'size': 16, 'color': 'DARK'})],
             [('从开源项目成长为生产级 LLM 应用平台的标志性节点；'
               '背后是一个全职团队与活跃社区。', {'size': 16, 'color': 'MUTED'})],
         ],
         stats=[
             dict(num='60,000+', label='在 GPTs 发布前，就已在 Dify 创建了第一个应用'),
             dict(num='3 个', label='实战案例：聊天助手 / 企业知识库 / 工作流'),
             dict(num='14 种', label='知识库支持的文档格式，单文件不超过 15MB'),
         ],
         source='Source: star-history.com（2024-10-31）· Dify 介绍与实战'),

    # ── 03 什么是 Dify ──────────────────────────────────────
    dict(layout='definition', kicker='01 初识 Dify', title='什么是 Dify',
         term='Dify', formula='= Define  +  Modify',
         lead='持续地定义和改进你的 AI 应用。',
         body='平台内置构建 LLM 应用所需的完整技术栈：数百个模型的支持、直观的 Prompt 编排界面、'
              '高质量的 RAG 引擎、稳健的 Agent 框架与灵活的工作流，同时提供易用的界面与 API，'
              '让开发者不必重复造轮子。',
         aside=[('社区对它的描述：', {'size': 13, 'color': 'MUTED'}),
                ('简单、克制、快速迭代。', {'size': 13, 'color': 'DARK', 'bold': True})],
         source='Source: Dify 介绍与实战 · §1.1 什么是 Dify'),

    # ── 04 设计初衷 ─────────────────────────────────────────
    dict(layout='comparison_rows', kicker='01 初识 Dify', title='设计初衷：把复杂流程留给平台',
         col_a='传统方式', col_b='Dify 的方式',
         rows=[
             dict(dim='数据导入', a='每种数据格式都要单独对接，导入前先做格式转换',
                  b='支持多种数据格式无缝导入'),
             dict(dim='数据清洗', a='需要编写脚本处理噪声、缺失与偏差，步骤繁琐',
                  b='图形化界面完成数据的清洗与归类'),
             dict(dim='建模与分析', a='手动设置与编程，建模耗时、分析结果不直观',
                  b='模块化流程，自动完成建模与可视化'),
             dict(dim='结果输出', a='结果分散在多份文件里，需多步操作才能成报告',
                  b='提供多样化输出接口，自动生成报告'),
         ],
         source='Source: Dify 介绍与实战 · §1.1.2 设计初衷'),

    # ── 05 九个关键词 ───────────────────────────────────────
    dict(layout='numbered_columns', kicker='01 初识 Dify', title='核心理念：九个关键词',
         columns=3, row_h=1.60,
         items=[
             dict(name='开源', desc='自由访问、修改与扩展平台功能，数据隐私与安全可控'),
             dict(name='低代码 / 无代码', desc='可视化界面 + 模块化设计，无需深入了解技术细节'),
             dict(name='模块化设计', desc='每个模块功能与接口清晰，按需选用'),
             dict(name='全面模型支持', desc='无缝集成 GPT、Mistral、Llama 3 等数百个模型'),
             dict(name='功能组件丰富', desc='AI 工作流、RAG 管道、Agent、模型管理，覆盖原型到生产'),
             dict(name='单一 API 或 WebApp', desc='开箱即用，简化应用的部署与接入'),
             dict(name='可观测性', desc='LLMOps 监控日志与性能，据真实数据改进提示与模型'),
             dict(name='后端即服务', desc='所有功能均提供 API，轻松集成进现有业务逻辑'),
             dict(name='云服务与自托管', desc='既可使用云服务，也可部署在自己的环境中'),
         ],
         source='Source: Dify 介绍与实战 · §1.2 Dify 的核心理念'),

    # ── 06 四大挑战 ─────────────────────────────────────────
    dict(layout='quadrant', kicker='02 为什么选 Dify', title='AI 应用开发，难在哪？',
         items=[
             dict(name='数据获取与管理',
                  desc='特定领域里获得标注良好的数据集既昂贵又耗时；噪声、偏差与缺失数据会直接影响模型的可靠性与准确性。'),
             dict(name='灵活部署与集成',
                  desc='需要权衡可扩展性、响应时间与算力消耗，并在部署后建立数据反馈与模型迭代的闭环。'),
             dict(name='数据隐私与安全',
                  desc='既要保护隐私又要高效利用数据，同时防范对抗样本等针对 AI 系统本身的攻击。'),
             dict(name='持续更新与迭代',
                  desc='需要实时监测系统性能，并根据新数据与新需求持续调整，跟上快速演进的技术。'),
         ],
         source='Source: Dify 介绍与实战 · §2.1 人工智能应用开发的挑战'),

    # ── 07 价值·开发效率 ────────────────────────────────────
    dict(layout='comparison_rows', kicker='02 为什么选 Dify', title='价值（一）：开发效率',
         col_a='能力说明', col_b='举例',
         rows=[
             dict(dim='简化开发流程',
                  a='数据集管理、可视化提示编排与应用运营工具把复杂度收进平台，实现低代码甚至无代码开发',
                  b='智能客服机器人 —— 拖放组件定义处理步骤，无需编写后端代码'),
             dict(dim='支持多种模型',
                  a='兼容 GPT 系列、Mistral、Llama 3 等多种厂商模型，按需选择最合适的模型',
                  b='多语言翻译应用 —— 从 GPT 切换到 Mistral，无需重新对接 API'),
             dict(dim='模块化组件',
                  a='工作流、RAG 管道、智能代理、模型管理等组件覆盖从原型到生产的全过程',
                  b='内容创作平台 —— 工作流抓取新闻、RAG 精简排序、文本生成产出摘要'),
         ],
         source='Source: Dify 介绍与实战 · §2.2.1–2.2.3'),

    # ── 08 价值·生产与生态 ──────────────────────────────────
    dict(layout='tinted_bands', kicker='02 为什么选 Dify', title='价值（二）：生产就绪与生态',
         band_h=1.02, band_gap=0.18,
         bands=[
             dict(name='私有化部署与数据安全',
                  desc='部署在自有数据中心或专属云环境，满足合规要求并完全掌控数据。'),
             dict(name='活跃的开发者社区',
                  desc='分享经验、获取反馈、贡献代码，持续为平台注入创新动力。'),
             dict(name='降低 AI 应用开发门槛',
                  desc='AI 的变化被隔离在业务层之外，业务开发者只需关注自己的领域。'),
             dict(name='快速迭代与创新',
                  desc='Workflow 让 AI 工作流可视化，像搭积木一样构建复杂流程。'),
         ],
         source='Source: Dify 介绍与实战 · §2.2.4–2.2.7'),

    # ── 09 三类应用场景 ─────────────────────────────────────
    dict(layout='tinted_bands', kicker='03 Dify 能做什么', title='三类典型应用场景',
         band_h=1.42, band_gap=0.20,
         bands=[
             dict(name='创业团队', desc='用 Dify 构建 MVP 拿到投资，或通过 POC 赢得客户订单；低代码特性大幅缩短从创意到产品的周期。'),
             dict(name='现有业务', desc='通过 RESTful API 把 AI 能力接入现有业务，实现 Prompt 与业务代码解耦，并在管理界面追踪数据、成本与用量。'),
             dict(name='大型企业', desc='作为企业内部 LLM 网关部署，加速生成式 AI 普及，并实现集中治理与统一监管。'),
         ],
         source='Source: Dify 介绍与实战 · §3.1 应用场景概览'),

    # ── 10 四大应用类型 ─────────────────────────────────────
    dict(layout='data_table', kicker='03 Dify 能做什么', title='四大应用类型与代表案例',
         header=['类型', '形态与能力', '典型场景', '代表案例'],
         col_widths=[1.85, 3.60, 3.00, 2.88],
         rows=[
             ['文本生成型', '表单 + 结果，支持流式返回，可接入数据集与插件增强', '翻译、改写、摘要生成', 'Copy.ai、Jasper'],
             ['对话型', '多轮对话，具备对话记忆、AI 开场白与上下文理解', '智能客服、教育辅导', 'Ada、Duolingo Max'],
             ['Agent 智能助手', '配置搜索、计算、绘图等工具，自主完成任务分解与推理', '财务分析、报告撰写', 'AgentGPT、AutoGPT'],
             ['工作流应用', 'Chatflow 面向多步骤对话，Workflow 面向自动化批处理', '复杂业务流程自动化', 'Rasa、n8n.io'],
         ],
         source='Source: Dify 介绍与实战 · §3.2.1–3.2.4'),

    # ── 11 五项辅助能力 ─────────────────────────────────────
    dict(layout='numbered_columns', kicker='03 Dify 能做什么', title='应用之外：五项辅助能力',
         columns=3, row_h=1.55,
         items=[
             dict(name='知识库管理', desc='支持文档导入与向量化，为应用提供专业领域知识'),
             dict(name='标注系统', desc='可人工编辑高质量答案，提升回复准确度'),
             dict(name='内容审核', desc='支持敏感词过滤，确保对话安全合规'),
             dict(name='数据分析', desc='提供详细的使用数据与效果分析'),
             dict(name='API 集成', desc='支持通过 API 调用集成到其他系统'),
         ],
         source='Source: Dify 介绍与实战 · §3.2.5 其他辅助功能'),

    # ── 12 版本与定价 ───────────────────────────────────────
    dict(layout='data_table', kicker='03 Dify 能做什么', title='产品版本与定价',
         header=['对比项', 'Sandbox（试用）', 'Professional（个人 / 小团队）', 'Team（中型团队）'],
         col_widths=[2.00, 3.10, 3.60, 2.63],
         y=2.15, h=3.65, row_h=0.50, header_h=0.55,
         rows=[
             ['定价', '免费', '$59 / 月（或 $590 / 年）', '$159 / 月'],
             ['消息额度', '200 条（总计）', '5,000 条 / 月', '10,000 条 / 月'],
             ['团队成员', '1 人', '3 人', '50 人'],
             ['应用数量', '5 个', '50 个', '200 个'],
             ['存储空间', '50MB', '5GB', '20GB'],
             ['API 限额', '5,000 / 天', '不限', '不限'],
             ['日志历史', '30 天', '不限', '不限'],
         ],
         source='注册即赠 200 条 OpenAI 消息额度 · 学生与教育工作者免费 · 价格不含当地税费'),

    # ── 13 实战一：聊天助手五步 ─────────────────────────────
    dict(layout='process_chain', kicker='04 实战一 · 聊天助手',
         title='五步搭出第一个聊天助手',
         steps=[
             dict(num='01', name='创建应用', desc='登录 Dify Cloud\n选「基础编排」'),
             dict(num='02', name='编写提示词', desc='点右上角「生成」\n可让 AI 代写'),
             dict(num='03', name='配置模型', desc='选模型，调温度\nTop P / Top K'),
             dict(num='04', name='应用调试', desc='预览区检查输出\n按需调整参数'),
             dict(num='05', name='应用发布', desc='运行网页 / 嵌入\n网站 / 访问 API'),
         ],
         note=[('基础编排不修改内置提示，最适合新手起步', {'size': 15, 'color': 'DARK', 'bold': True}),
               ('；提示词生成器只需输入需求与说明，AI 即可产出可直接使用的提示词。',
                {'size': 15, 'color': 'MUTED'})],
         source='Source: Dify 介绍与实战 · §4.1 聊天助手'),

    # ── 14 模型参数 ─────────────────────────────────────────
    dict(layout='data_table', kicker='04 实战一 · 聊天助手', title='模型参数：一次说清怎么调',
         header=['参数', '范围', '作用与调参建议'],
         col_widths=[2.60, 1.00, 7.73],
         y=2.15, h=3.55, row_h=0.48, header_h=0.50,
         font=12.5,
         rows=[
             ['温度 Temperature', '0–1', '值越低越倾向高概率词，内容更稳但缺乏多样性；过高会发散、产生错误内容'],
             ['Top P', '0–1', '只从累计概率超过阈值 P 的候选词中采样；值越高，多样性与创意越高'],
             ['Top K', '0–1', '从概率最高的 K 个词中采样；值越高候选越多，多样性与创意越高'],
             ['存在惩罚 Presence', '0–1', '对已出现的 token 施加惩罚以降低重复；不确定时建议设为 0'],
             ['频率惩罚 Frequency', '0–1', 'token 每次出现时施加惩罚以降低重复；不确定时建议设为 0'],
             ['最大标记 Max tokens', '—', '最大生成长度，建议调大，并按不同模型的最大输出长度调整'],
         ],
         source='技巧：要唯一准确答案 → top-k / top-p / temperature 设为 0；要更多样性 → top-p 设 0.95'),

    # ── 15 实战二：知识库七步 ───────────────────────────────
    dict(layout='timeline_vertical', kicker='04 实战二 · 企业知识库',
         title='用 Dify 搭企业知识库的七个步骤',
         steps=[
             dict(name='整理文档', desc='把内部资料整理成文档；问答类内容做成 Q&A 形式效果更佳'),
             dict(name='创建知识库', desc='打开知识库页面，创建并上传资料文档'),
             dict(name='文本分段与清洗', desc='上传后进入该步骤，右侧可预览分段情况'),
             dict(name='索引方式', desc='高质量（Embedding，耗 token）或经济（离线向量引擎）'),
             dict(name='编写 Prompt', desc='用 PromptPerfect 把简单需求优化成结构化提示词'),
             dict(name='引用知识库', desc='「上下文 → 添加 → 选中知识库 → 点击添加」'),
             dict(name='调试与发布', desc='对比助手输出与企业文档原文，检查是否引用'),
         ],
         source='支持的格式：TXT / Markdown / PDF / HTML / XLSX / DOCX / CSV / PPTX / XML / EPUB 等 14 种'),

    # ── 16 实战三：工作流节点 ───────────────────────────────
    # 原先用 node_flow（8 个描边方框 + 折返连线）渲染。那套版式已删除，改投
    # phase_grouped_flow —— 它当初正是为了「节点多、标签长」才加的，用它承接
    # 这页内容自洽。
    # 分 3 组而不是 4 组：渲染器每组只吃 4 个节点（`ph['nodes'][:4]`），而框宽是
    # `(avail - gap)/k` —— **单节点组会被拉成一个通栏大框**，与相邻组不成比例。
    # 2/3/3 是回归网 fixture 已验证过的比例（同一批小红书工作流内容）。
    # 代价：这套版式没有 note 槽位，原来那句「LLM 生成 + HTTP 生图 + 代码处理 +
    # 模板组装」的概述没了，语义由组名承接。
    dict(layout='phase_grouped_flow', kicker='04 实战三 · 小红书运营一条龙',
         title='工作流总览：8 个节点跑通内容生产线',
         phases=[
             dict(name='输入与标题', nodes=['Start 收集信息', '生成标题']),
             dict(name='正文与配图', nodes=['生成正文', '生成封面前言', '生成封面图']),
             dict(name='取图与输出', nodes=['获取封面图 URL', '组装结果', 'End 结束']),
         ],
         source='Source: Dify 介绍与实战 · §4.3 小红书运营一条龙工作流'),

    # ── 17 结语 ─────────────────────────────────────────────
    dict(layout='quote', kicker='05 落地要点',
         quote=[[('复杂度随业务需要', {}), ('渐进', {'hl': True}), ('，', {})],
                [('而不是一次到位。', {})]],
         attribution='—— 基础编排起步 → RAG 增强 → 可视化工作流',
         body=[
             [('第一步', {'size': 16, 'color': 'DARK', 'bold': True}),
              ('  参照官方文档跑通构建流程，先做出一个能用的聊天助手。', {'size': 16, 'color': 'MUTED'})],
             [('第二步', {'size': 16, 'color': 'DARK', 'bold': True}),
              ('  接入企业知识库，让回答有据可依；用 RAG 把散落的文档变成可检索资产。',
               {'size': 16, 'color': 'MUTED'})],
             [('第三步', {'size': 16, 'color': 'DARK', 'bold': True}),
              ('  用工作流把重复的内容生产串成流水线，让 AI 真正进入日常业务。',
               {'size': 16, 'color': 'MUTED'})],
         ],
         source='Source: Dify 介绍与实战 · 全书要点 · Dify Next Steps'),
]
