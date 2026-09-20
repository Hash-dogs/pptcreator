# -*- coding: utf-8 -*-
"""配置加载：读项目根目录的 `.env`，不额外依赖 python-dotenv。

设计原则：
  - **缺配置不报错**。文本模型没配 → 大纲/规划退回确定性模式；
    视觉模型没配 → 看图环节输出人工复核包。整条链路永远能跑完。
  - 视觉配置留空时可回退用文本模型的 base/key（同一家服务商常共用 key）。
"""
from __future__ import annotations
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENV_PATH = os.path.join(ROOT, '.env')


def load_env(path: str = ENV_PATH, override: bool = False) -> dict:
    """极简 .env 解析：KEY=VALUE，# 开头为注释，值两端的引号会被剥掉。"""
    out = {}
    if not os.path.isfile(path):
        return out
    with open(path, encoding='utf-8-sig') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if v == '':
                continue
            out[k] = v
            if override or k not in os.environ:
                os.environ[k] = v
    return out


def get(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key) or default


def get_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, '') or default)
    except ValueError:
        return default


def get_bool(key: str, default: bool = False) -> bool:
    v = (os.environ.get(key) or '').strip().lower()
    if v in ('1', 'true', 'yes', 'on'):
        return True
    if v in ('0', 'false', 'no', 'off'):
        return False
    return default


class LLMConfig:
    """一个 OpenAI 兼容端点的连接信息。"""

    def __init__(self, base: str, key: str, model: str,
                 api_version: str | None = None):
        self.base = base.rstrip('/')
        self.key = key
        self.model = model
        self.api_version = api_version

    @property
    def is_azure(self) -> bool:
        return bool(self.api_version) or '.openai.azure.com' in self.base

    @property
    def url(self) -> str:
        if self.is_azure:
            if '/chat/completions' in self.base:
                base = self.base
            else:
                base = self.base + '/chat/completions'
            sep = '&' if '?' in base else '?'
            return '%s%sapi-version=%s' % (base, sep, self.api_version or '2024-08-01-preview')
        return self.base + '/chat/completions'

    @property
    def headers(self) -> dict:
        if self.is_azure:
            return {'Content-Type': 'application/json', 'api-key': self.key}
        return {'Content-Type': 'application/json',
                'Authorization': 'Bearer ' + self.key}

    def __repr__(self):
        return '<LLMConfig %s @ %s>' % (self.model, self.base)


def llm_config() -> LLMConfig | None:
    base, key, model = (get('PPTGEN_LLM_BASE_URL'), get('PPTGEN_LLM_API_KEY'),
                        get('PPTGEN_LLM_MODEL'))
    if base and key and model:
        return LLMConfig(base, key, model, get('PPTGEN_LLM_API_VERSION'))
    return None


# 已知看不了图的模型名特征。配置里填了这些就直接判定「没有视觉能力」，
# 免得调用时返回一堆无法解析的文字、看起来像「看图通过」。
_NO_VISION = ('deepseek-chat', 'deepseek-flash', 'deepseek-reasoner',
              'deepseek-coder', 'gpt-3.5', 'text-davinci', 'o1-mini')


def vision_capable(model: str | None) -> bool:
    if not model:
        return False
    m = model.lower()
    return not any(m.startswith(p) for p in _NO_VISION)


def vision_config() -> LLMConfig | None:
    base = get('PPTGEN_VISION_BASE_URL')
    key = get('PPTGEN_VISION_API_KEY')
    model = get('PPTGEN_VISION_MODEL')
    if model and not vision_capable(model):
        return None
    if base and key and model:
        return LLMConfig(base, key, model, get('PPTGEN_VISION_API_VERSION'))
    # 回退：**仅在给了视觉模型名、但没给 base/key 时**复用文本模型的连接信息
    #（同家服务商常共用 key）。若连模型名都没有，就没有视觉能力可用 ——
    # 早先这里会直接返回文本模型配置，导致把看不见图的模型当视觉模型调用。
    if model and get_bool('PPTGEN_VISION_FALLBACK_TO_LLM', True):
        fb = llm_config()
        if fb is not None:
            return LLMConfig(fb.base, fb.key, model, fb.api_version)
    return None


def page_range() -> tuple[int, int]:
    lo = get_int('PPTGEN_MIN_PAGES', 13)
    hi = get_int('PPTGEN_MAX_PAGES', 18)
    return (lo, hi) if lo <= hi else (hi, lo)


def content_mode() -> str:
    m = (get('PPTGEN_CONTENT_MODE', 'balance') or 'balance').lower()
    return m if m in ('strict', 'balance', 'enrich') else 'balance'


def out_dir() -> str:
    d = get('PPTGEN_OUT', 'out') or 'out'
    return d if os.path.isabs(d) else os.path.join(ROOT, d)


def template_path() -> str:
    t = get('PPTGEN_TEMPLATE', '迈胜PPT模板.pptx') or '迈胜PPT模板.pptx'
    return t if os.path.isabs(t) else os.path.join(ROOT, t)


def summary() -> str:
    llm, vis = llm_config(), vision_config()
    lo, hi = page_range()
    return '\n'.join([
        '配置状态',
        '  文本模型 : %s' % (llm or '未配置（大纲/规划将走确定性模式）'),
        '  视觉模型 : %s' % (vis or '未配置（看图输出人工复核包）'),
        '  页数区间 : %d–%d' % (lo, hi),
        '  内容策略 : %s' % content_mode(),
        '  模板     : %s' % template_path(),
        '  输出目录 : %s' % out_dir(),
    ])
