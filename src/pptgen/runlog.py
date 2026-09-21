# -*- coding: utf-8 -*-
"""文件日志：一次流程 = 一个自包含的文件夹。

```
out/logs/2026-09-21-14-30-Dify 介绍与实战/
├── run.json     本次流程的关键值（每个阶段结束都增量落盘）
├── run.log      完整流水（CLI 的 stdout / Web 的任务日志）
├── <成品>.pptx   生成的 pptx（复制进来）
└── <源文件>      仅当源是**上传**的才放；用根目录文件时不放
```

为什么要有这个：在这之前**跑一次流程事后几乎无法追溯** —— Web 的日志在内存里
（服务重启就没），CLI 只有 stdout，落盘的只有产物。结果是一份质量很差的大纲
摆在那里，却从任何留存物上都看不出「它其实是兜底产物、模型压根没跑」。

两条贯穿始终的设计约束：

1. **写日志绝不能拖垮管线。** 磁盘满、目录被删、权限不足 —— 所有落盘操作自己吞异常，
   只在 stderr 吼一句。日志是附属品，不是主流程的一部分。
2. **记录必须可信。** 宁可多花 8MB 复制一份 pptx，也不做硬链接之类的「优化」——
   `build.py` 是原地截断写（`prs.save`），硬链接会让下一次同名构建改掉已归档的历史产物，
   日志里那份就变成了真假混在一起的东西。
"""
from __future__ import annotations
import contextlib
import datetime
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time

from . import config

# Windows 保留名：拿它们当目录名会建不出来或行为诡异。
_RESERVED = {'con', 'prn', 'aux', 'nul'} | {
    '%s%d' % (p, i) for p in ('com', 'lpt') for i in range(1, 10)}

# 与 run.py 的 `_stem` 对齐
_MAX_NAME = 40


def safe_task_name(name: str) -> str:
    """把任务名洗成能安全当目录名的样子。

    只用于**目录名**。原始文件名（可能带路径、带各种怪字符）不允许走这里当路径用。
    """
    t = re.sub(r'[\\/:*?"<>|\r\n\t]+', '_', (name or '').strip())
    t = t.strip(' .')                       # Windows 会静默吃掉尾部的点和空格
    if not t or t in ('.', '..') or t.lower() in _RESERVED:
        t = 'run'
    return t[:_MAX_NAME]


def sha256(path: str) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def file_info(path: str) -> dict:
    """一个文件的轻量指纹 —— 不复制时靠它留痕。"""
    try:
        st = os.stat(path)
    except OSError:
        return {'path': path, 'exists': False}
    return {'path': path, 'exists': True, 'size': st.st_size,
            'mtime': datetime.datetime.fromtimestamp(st.st_mtime).isoformat(
                timespec='seconds'),
            'sha256': sha256(path)}


class RunLog:
    """一次流程的落盘记录。

    `enabled=False`（或 `PPTGEN_LOG=0`）时所有方法都是空操作 ——
    调用点因此不用写 `if rl:`。
    """

    def __init__(self, task: str, *, origin: str = 'cli', command: str = '',
                 root: str | None = None, enabled: bool | None = None):
        # 时间戳**在这里冻结**。惰性取会让跨分钟的任务在不同阶段拿到不同分钟，
        # 甚至 10:30:59 开始、10:31:10 才第一次写日志时取到 10:31。
        self.started = datetime.datetime.now()
        self.task = safe_task_name(task)
        self.origin = origin
        self.command = command
        self.root = root or config.log_root()
        self.enabled = config.log_enabled() if enabled is None else enabled
        self._dir: str | None = None
        self._fh = None
        self._lock = threading.Lock()
        self._data: dict = {
            'task': self.task, 'origin': origin, 'command': command,
            'started': self.started.isoformat(timespec='seconds'),
            'finished': None, 'duration_sec': None, 'ok': None, 'error': None,
            'stages': [],
        }

    # ── 目录 ──────────────────────────────────────────────────
    @property
    def dir(self) -> str | None:
        """惰性建目录。建不出来就返回 None，所有落盘随之变空操作。"""
        if not self.enabled:
            return None
        if self._dir:
            return self._dir
        with self._lock:
            if self._dir:
                return self._dir
            try:
                os.makedirs(self.root, exist_ok=True)
                self._dir = self._claim('%s-%s' % (
                    self.started.strftime('%Y-%m-%d-%H-%M'), self.task))
            except OSError as e:
                self._warn('建日志目录失败：%s' % e)
                self.enabled = False
        return self._dir

    def _claim(self, stem: str) -> str:
        """抢占目录名。

        必须用 `exist_ok=False` 逐个试 —— `ThreadingHTTPServer` 是多线程的，
        两个并发任务同一分钟、同一个任务名会**同时**看到「目录不存在」；
        用 `exist_ok=True` 的话它们会写进同一个文件夹，互相覆盖对方记录。
        """
        for i in range(1, 100):
            cand = os.path.join(self.root, stem if i == 1 else '%s-%d' % (stem, i))
            try:
                os.makedirs(cand, exist_ok=False)
                return cand
            except FileExistsError:
                continue
        raise OSError('同名日志目录过多：%s' % stem)

    def open_at(self, path: str) -> bool:
        """续写一个已存在的流程文件夹（Web 的第二个请求用）。

        `path` 必须落在日志根目录之下 —— 它可能来自服务器自己写的 sidecar，
        而 sidecar 是磁盘上的文件，不该无条件信任。
        """
        if not self.enabled:
            return False
        if not _under(self.root, path):
            self._warn('日志目录不在根之下，拒绝：%s' % path)
            return False
        if not os.path.isdir(path):
            return False
        self._dir = path
        try:
            with open(os.path.join(path, 'run.json'), encoding='utf-8-sig') as f:
                old = json.load(f)
            # 保留上一段（大纲阶段）写的东西，只覆盖本次的身份字段
            old.update(command=self.command, ok=None, error=None, finished=None)
            old.setdefault('attempts', []).append(
                {'command': self.command, 'at': self.started.isoformat(timespec='seconds')})
            self._data = old
        except (OSError, ValueError):
            self._data.setdefault('attempts', []).append(
                {'command': self.command, 'at': self.started.isoformat(timespec='seconds')})
        return True

    # ── 写 ────────────────────────────────────────────────────
    def log(self, msg: str):
        """往 run.log 追加一行。"""
        if not self.enabled or not msg:
            return
        d = self.dir
        if not d:
            return
        try:
            if self._fh is None:
                # 行缓冲：tee 逐行喂进来，别攒着，崩溃时最后几行最有用
                self._fh = open(os.path.join(d, 'run.log'), 'a',
                                encoding='utf-8', buffering=1)
            self._fh.write('[%s] %s\n' % (
                datetime.datetime.now().strftime('%H:%M:%S'), msg.rstrip('\n')))
        except (OSError, ValueError) as e:
            self._warn('写 run.log 失败：%s' % e)

    def note(self, section: str, **kv):
        """把一组值合并进 run.json 的某个分组。"""
        if not self.enabled:
            return
        cur = self._data.get(section)
        self._data[section] = dict(cur, **kv) if isinstance(cur, dict) else dict(kv)
        if not self._write():
            # 写不进去就回滚。留着坏值在内存里的话，**之后每一次写都会失败**，
            # 记录会永久停在最后一个好版本上 —— 后面的阶段全丢。
            if cur is None:
                self._data.pop(section, None)
            else:
                self._data[section] = cur

    def put(self, key, value):
        """写一个顶层键。"""
        if not self.enabled:
            return
        missing, old = key not in self._data, self._data.get(key)
        self._data[key] = value
        if not self._write() and not missing:
            self._data[key] = old

    @contextlib.contextmanager
    def stage(self, name: str):
        """一个阶段。自动记耗时，失败时把异常留在**这一阶段**上。

        这样 `cmd_full` 中途炸了，能一眼看出是 parse / outline / plan 哪一段断的，
        而不是只知道「整体失败」。
        """
        rec = {'name': name, 'started': datetime.datetime.now().isoformat(timespec='seconds')}
        if self.enabled:
            self._data.setdefault('stages', []).append(rec)
        t0 = time.time()
        try:
            yield rec
            rec['ok'] = True
        except BaseException as e:
            rec['ok'] = False
            rec['error'] = '%s: %s' % (type(e).__name__, e)
            rec['duration_sec'] = round(time.time() - t0, 2)
            self._write()
            raise
        rec['duration_sec'] = round(time.time() - t0, 2)
        self._write()

    # ── 归档 ──────────────────────────────────────────────────
    def attach_source(self, path: str, *, origin: str = 'root',
                      original_name: str | None = None):
        """归档源文件。

        **只有上传来的才复制** —— 用根目录文件时源就在仓库里，没必要再存一份
        （用户明确要求的就是这个规则）。非上传的仍然记 size/mtime/sha256，
        事后能确认「那次用的到底是哪一份」。
        """
        if not self.enabled:
            return
        info = file_info(path)
        info['name'] = os.path.basename(path)
        info['origin'] = origin
        if original_name:
            # 上传时的原始文件名（`_sanitize_name` 之前的）。**只能进 JSON**，
            # 永远不许当路径分量用 —— 消毒函数存在的全部理由就是不信任它。
            info['original_name'] = original_name
        if origin != 'upload':
            info['copied'] = False
            self.note('source', **info)
            return
        cap = config.log_max_copy_mb() * 1024 * 1024
        if info.get('size', 0) > cap:
            info['copied'] = False
            info['note'] = '超过 %dMB，未复制' % config.log_max_copy_mb()
        else:
            info['archived_as'] = self._copy_in(path, os.path.basename(path))
            info['copied'] = info['archived_as'] is not None
        self.note('source', **info)

    def attach_pptx(self, path: str):
        """归档成品 pptx。只在 build **成功之后**调用。"""
        if not self.enabled:
            return
        info = file_info(path)
        info['name'] = os.path.basename(path)
        if config.log_copy_pptx() and info.get('size', 0) <= config.log_max_copy_mb() * 1024 * 1024:
            info['archived_as'] = self._copy_in(path, os.path.basename(path))
            info['copied'] = info['archived_as'] is not None
        else:
            info['copied'] = False
        self.note('artifacts', pptx=info)

    def attach_file(self, key: str, path: str):
        """归档一个小的中间产物（如确认后的大纲），或只记路径。"""
        if not self.enabled:
            return
        info = file_info(path)
        info['name'] = os.path.basename(path)
        info['copied'] = False
        if info.get('exists') and info.get('size', 0) <= 4 * 1024 * 1024:
            info['archived_as'] = self._copy_in(path, os.path.basename(path))
            info['copied'] = info['archived_as'] is not None
        self.note('artifacts', **{key: info})

    def _copy_in(self, src: str, filename: str) -> str | None:
        """把文件复制进日志文件夹，返回**实际落地的文件名**。

        同名会加 `-2`：源文件和成品常常同名（都叫 `<stem>.pptx`），所以调用方
        必须把返回值记进 run.json，否则事后分不清哪个是喂进去的、哪个是产出的。
        绝不覆盖已有记录。
        """
        d = self.dir
        if not d:
            return None
        dst = os.path.join(d, safe_task_name(filename))
        stem, ext = os.path.splitext(dst)
        for i in range(1, 100):
            cand = dst if i == 1 else '%s-%d%s' % (stem, i, ext)
            if os.path.exists(cand):
                continue
            # Windows 上可能撞 PermissionError（officecli 还在渲染时持有句柄）
            for attempt in range(3):
                try:
                    shutil.copy2(src, cand)
                    self._data.setdefault('files', []).append(os.path.basename(cand))
                    return os.path.basename(cand)
                except OSError as e:
                    last = e
                    time.sleep(0.1 * (attempt + 1))
            self._warn('复制 %s 失败：%s' % (src, last))
            return None
        return None

    # ── 收尾 ──────────────────────────────────────────────────
    def finish(self, ok: bool = True, error: str | None = None):
        if not self.enabled:
            return
        end = datetime.datetime.now()
        self._data.update(finished=end.isoformat(timespec='seconds'),
                          duration_sec=round((end - self.started).total_seconds(), 2),
                          ok=bool(ok), error=error)
        self._write()
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def _write(self) -> bool:
        """原子写：临时文件 + `os.replace`。返回是否成功。

        **不能直接 json.dump 到目标路径** —— 进程在 dump 中途死掉（模型 OOM、
        任务管理器结束、Ctrl-C 打在窗口里）会留下截断的、解析不了的 run.json，
        而那恰恰是最需要它可读的时刻。
        """
        d = self.dir
        if not d:
            return False
        tmp = os.path.join(d, 'run.json.tmp')
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, os.path.join(d, 'run.json'))
            return True
        except (OSError, TypeError, ValueError) as e:
            self._warn('写 run.json 失败：%s' % e)
            with contextlib.suppress(OSError):
                os.remove(tmp)
            return False

    @staticmethod
    def _warn(msg: str):
        try:
            sys.stderr.write('  [runlog] %s\n' % msg)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════
# stdout 分流（**只给 CLI 用**）
# ══════════════════════════════════════════════════════════════
class _Tee:
    """把写到 stdout/stderr 的内容同时喂给 run.log。

    按行攒：`print` 是把正文和换行**分两次** write 的，不攒的话每条日志
    都会碎成两行。退出时要把残留的半个行 flush 掉。
    """

    def __init__(self, stream, rl: 'RunLog'):
        self._s, self._rl, self._buf = stream, rl, ''

    def write(self, text):
        try:
            self._s.write(text)
        except Exception:
            pass
        try:
            self._buf += text
            while '\n' in self._buf:
                line, self._buf = self._buf.split('\n', 1)
                self._rl.log(line)
        except Exception:
            pass
        return len(text)

    def flush(self):
        if self._buf:
            self._rl.log(self._buf)
            self._buf = ''
        with contextlib.suppress(Exception):
            self._s.flush()

    def __getattr__(self, name):
        # encoding / isatty / fileno 之类原样透传。下划线开头的一律不转发，
        # 否则 `self._s` 缺失时会无限递归。
        if name.startswith('_'):
            raise AttributeError(name)
        return getattr(self._s, name)


@contextlib.contextmanager
def tee(rl: 'RunLog | None'):
    """CLI 专用：把控制台输出同时写进 run.log。

    好处是不用去改散落各处的 `print`，而且**异常 traceback 也一起留下** ——
    失败现场最需要的就是它。

    反直觉的加分项：`run.py` 把控制台 reconfigure 成了 `errors='replace'`，
    编码不兼容的字会丢；而 run.log 用 UTF-8 写，**不丢**。日志比控制台更完整。

    **服务器绝对不能这么干**：`server.py` 的 `log_message` 把每个 HTTP 请求
    都写进 `sys.stderr`，并发任务的日志会互相污染。
    """
    if rl is None or not rl.enabled:
        yield
        return
    out, err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = _Tee(out, rl), _Tee(err, rl)
    try:
        yield
    finally:
        for cur, orig in ((sys.stdout, out), (sys.stderr, err)):
            with contextlib.suppress(Exception):
                cur.flush()
        sys.stdout, sys.stderr = out, err


# ══════════════════════════════════════════════════════════════
# 作用域：CLI 的嵌套命令共用同一个文件夹
# ══════════════════════════════════════════════════════════════
_local = threading.local()


def active() -> RunLog | None:
    return getattr(_local, 'run', None)


@contextlib.contextmanager
def stage(name: str):
    """当前作用域的一个阶段。**没有作用域时也安全**（yield 一个空 dict），
    这样调用点不用写 `if rl:`。"""
    rl = active()
    if rl is None:
        yield {}
        return
    with rl.stage(name) as rec:
        yield rec


def note(section: str, **kv):
    """往当前作用域记一组值。没有作用域时什么都不做。"""
    rl = active()
    if rl is not None:
        rl.note(section, **kv)


def attach_pptx(path: str):
    rl = active()
    if rl is not None:
        rl.attach_pptx(path)


def attach_source(path: str, **kw):
    rl = active()
    if rl is not None:
        rl.attach_source(path, **kw)


def attach_file(key: str, path: str):
    rl = active()
    if rl is not None:
        rl.attach_file(key, path)


@contextlib.contextmanager
def scope(task: str, *, origin: str = 'cli', command: str = '',
          root: str | None = None):
    """一次顶层调用的作用域。

    **只在最外层建文件夹**：`cmd_full` 是手工拼 `Namespace` 直接调
    `cmd_parse` / `cmd_outline` / `cmd_plan`… 的，不会再走一遍入口，
    所以入口处一个 scope 就覆盖了全部嵌套调用。

    用线程局部而不是全局：Web 的并发任务互不干扰。`finally` 里必须清掉，
    否则线程复用（连接池、将来的线程池）会把上一个任务的 RunLog 泄漏给下一个。
    """
    outer = active()
    if outer is not None:
        yield outer
        return
    rl = RunLog(task, origin=origin, command=command, root=root)
    _local.run = rl
    ok, err = True, None
    try:
        yield rl
    except BaseException as e:
        # SystemExit / KeyboardInterrupt 也走这里 —— 用户会 Ctrl-C 一个跑了几分钟的
        # 模型调用（run.py 里还有两处真的 raise SystemExit）。记完**原样重抛**。
        ok, err = False, '%s: %s' % (type(e).__name__, e)
        raise
    finally:
        _local.run = None
        with contextlib.suppress(Exception):
            rl.finish(ok=ok, error=err)


# ══════════════════════════════════════════════════════════════
# 路径
# ══════════════════════════════════════════════════════════════
def _under(root: str, path: str) -> bool:
    """`path` 是否在 `root` 之下。

    必须 `realpath`（解析符号链接与 `..`）+ `normcase`（Windows 上 `C:\\` 与 `c:\\`
    直接比较是不相等的）。已有的 `server._safe_join` 用的是 abspath+normpath，
    不解析符号链接，不够用。
    """
    try:
        r = os.path.normcase(os.path.realpath(root))
        p = os.path.normcase(os.path.realpath(path))
    except (OSError, ValueError):
        return False
    return p != r and p.startswith(r + os.sep)


# ══════════════════════════════════════════════════════════════
# 配置快照
# ══════════════════════════════════════════════════════════════
def config_snapshot() -> dict:
    """当次生效的配置。**白名单**，逐项取。

    绝不能 `dict(os.environ)` 或 dump `.env` —— `.env` 就在仓库根，
    里面有 `PPTGEN_LLM_API_KEY` / `PPTGEN_VISION_API_KEY`。
    """
    lo, hi = config.page_range()
    tpl = config.template_path()
    return {
        'page_range': [lo, hi],
        'content_mode': config.content_mode(),
        'template': os.path.basename(tpl),
        'template_sha256': sha256(tpl),      # 模板换一份是最难查的「为什么产出变了」
        'outline_max_tokens': config.outline_max_tokens(),
        'plan_max_tokens': config.plan_max_tokens(),
    }
