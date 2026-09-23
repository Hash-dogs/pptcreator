# -*- coding: utf-8 -*-
"""OpenAI 兼容的模型调用（文本 + 视觉 + JSON 模式），只用标准库。

支持 OpenAI / 智谱 / DeepSeek / 通义 / 本地 vLLM / Azure OpenAI
（Azure 的 URL 与鉴权头由 `config.LLMConfig` 处理）。
"""
from __future__ import annotations
import base64
import json
import os
import time
import urllib.error
import urllib.request

from . import config


class LLMError(RuntimeError):
    pass


def _post(payload: dict, cfg: config.LLMConfig, timeout: int, retry: int) -> dict:
    data = json.dumps(payload).encode('utf-8')
    last = None
    for attempt in range(retry + 1):
        req = urllib.request.Request(cfg.url, data=data, headers=cfg.headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', 'replace')[:400]
            last = 'HTTP %s: %s' % (e.code, body)
            # 4xx（除 429）重试没意义
            if 400 <= e.code < 500 and e.code != 429:
                break
        except Exception as e:                       # 超时 / 连接失败
            last = '%s: %s' % (type(e).__name__, e)
        if attempt < retry:
            time.sleep(1.5 * (attempt + 1))
    raise LLMError('模型调用失败（%s）：%s' % (cfg.model, last))


def _content_of(resp: dict) -> str:
    try:
        msg = resp['choices'][0]['message']
    except (KeyError, IndexError, TypeError):
        raise LLMError('返回结构不符合预期: %s' % json.dumps(resp)[:300])
    content = (msg.get('content') or '').strip()
    if content:
        return content
    # 推理模型（如 deepseek-flash / deepseek-reasoner）会把大量 token 花在
    # reasoning_content 上。max_tokens 给小了，推理就会把额度耗尽、content 为空。
    # 静默返回空字符串比报错更糟 —— 上游会拿到空大纲继续往下跑。
    usage = resp.get('usage') or {}
    rt = (usage.get('completion_tokens_details') or {}).get('reasoning_tokens')
    if msg.get('reasoning_content') or rt:
        raise LLMError(
            '模型只产出了推理内容，正文为空：推理占用了 %s 个 token（max_tokens 给 '
            '少了）。把 .env 里的 PPTGEN_MAX_TOKENS 调大（建议 ≥8000），'
            '或换用非推理模型（如 deepseek-chat）。' % (rt if rt is not None else '多'))
    raise LLMError('模型返回了空内容: %s' % json.dumps(resp)[:300])


def default_max_tokens() -> int:
    """推理模型会把大量 token 花在 reasoning 上，默认额度要留足。"""
    return config.get_int('PPTGEN_MAX_TOKENS', 8000)


def ask_text(prompt: str, cfg: config.LLMConfig | None = None,
             system: str | None = None, *, json_mode: bool = False,
             max_tokens: int | None = None, temperature: float = 0.3) -> str:
    cfg = cfg or config.llm_config()
    if cfg is None:
        raise LLMError('未配置文本模型（PPTGEN_LLM_*）')
    msgs = []
    if system:
        msgs.append({'role': 'system', 'content': system})
    msgs.append({'role': 'user', 'content': prompt})
    payload = {'model': cfg.model, 'messages': msgs, 'temperature': temperature,
               'max_tokens': max_tokens or default_max_tokens()}
    if json_mode:
        payload['response_format'] = {'type': 'json_object'}
    return _content_of(_post(payload, cfg, config.get_int('PPTGEN_TIMEOUT', 180),
                             config.get_int('PPTGEN_RETRY', 2)))


def parse_json(raw: str) -> dict:
    """宽容解析模型返回的 JSON。

    四种形态都要吃下来（前两种是实测撞到的）：

    ① **服务端把 `response_format` 回显进正文**：`{"type": "json_object"}\\n{真正的 JSON}`。
       实测某兼容层这么干过。取「最外层大括号」会把这个回显和真正的对象**跨成一个**
       非法 JSON，于是整个解析失败 —— 识别链路的第一轮就是这么挂的。
    ② 正文里**两个对象拼在一起**（回显之外还可能有前言后语）。
    ③ 带 ``` 围栏（有的模型无视 json_mode）。
    ④ 裸数组（包一层 `items`）。

    做法：从每个 `{` 起用 `raw_decode` 试，取**第一个解析得出、且不止含 `type` 的
    对象** —— 「只含 type 的对象」正是回显的形状，跳过它继续找。
    """
    txt = (raw or '').strip()
    if txt.startswith('```'):
        txt = txt.split('\n', 1)[1] if '\n' in txt else txt
        txt = txt.rsplit('```', 1)[0]

    dec = json.JSONDecoder()
    # 裸数组**先试**：它可能是一串对象，而下面「取第一个 `{`」会把后面的全丢掉
    # （静默丢数据比解析失败更糟）。只有当 `[` 出现在第一个 `{` **之前**才这么判 ——
    # 否则回显对象里的 `{` 会被误当成数组的开头。
    ia, ib = txt.find('['), txt.rfind(']')
    io = txt.find('{')
    if ia >= 0 and ib > ia and (io < 0 or ia < io):
        try:
            arr = json.loads(txt[ia:ib + 1])
        except json.JSONDecodeError:
            arr = None
        if isinstance(arr, list):
            return {'items': arr}
    i = 0
    while i < len(txt):
        j = txt.find('{', i)
        if j < 0:
            break
        try:
            obj, end = dec.raw_decode(txt[j:])
        except ValueError:
            i = j + 1                       # 这个 `{` 起头解不出来，换下一个
            continue
        if isinstance(obj, dict) and set(obj) - {'type'}:
            return obj
        i = j + max(end, 1)                 # 只含 type 的回显对象：跳过，接着找
    raise LLMError('模型未返回可解析的 JSON：%s' % raw[:400])


def ask_json(prompt: str, cfg: config.LLMConfig | None = None,
             system: str | None = None, **kw) -> dict:
    """要求模型输出 JSON，并做一次宽容解析（剥 ``` 围栏 / 截取最外层大括号）。"""
    return parse_json(ask_text(prompt, cfg, system, json_mode=True, **kw))


# 图片扩展名 → MIME。**不能一律当 PNG**：用户上传的版式截图多是 jpg/png/webp，
# data URL 里写错类型，有的服务端会直接报「不支持的图片格式」。
_MIME = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
         '.webp': 'image/webp', '.gif': 'image/gif', '.bmp': 'image/bmp'}


def _b64(path: str) -> str:
    with open(path, 'rb') as f:
        return base64.b64encode(f.read()).decode('ascii')


def _data_url(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return 'data:%s;base64,%s' % (_MIME.get(ext, 'image/png'), _b64(path))


def ask_vision(prompt: str, image_paths: list[str],
               cfg: config.LLMConfig | None = None, *,
               max_tokens: int | None = None, json_mode: bool = False) -> str:
    cfg = cfg or config.vision_config()
    if cfg is None:
        raise LLMError('未配置视觉模型（PPTGEN_VISION_*）')
    content = [{'type': 'text', 'text': prompt}]
    for p in image_paths:
        content.append({'type': 'image_url', 'image_url': {'url': _data_url(p)}})
    payload = {'model': cfg.model, 'max_tokens': max_tokens or default_max_tokens(),
               'messages': [{'role': 'user', 'content': content}]}
    if json_mode:
        payload['response_format'] = {'type': 'json_object'}
    return _content_of(_post(payload, cfg, config.get_int('PPTGEN_TIMEOUT', 180),
                             config.get_int('PPTGEN_RETRY', 2)))


def ask_vision_json(prompt: str, image_paths: list[str],
                    cfg: config.LLMConfig | None = None, **kw) -> dict:
    """视觉 + JSON：识别版式结构走这条（返回结构不符合预期时由调用方重试）。"""
    return parse_json(ask_vision(prompt, image_paths, cfg, json_mode=True, **kw))
