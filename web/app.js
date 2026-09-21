/* 文档转 PPT —— 前端逻辑（原生 JS，无构建步骤）。
 *
 * 三段链路：选源 → 生成大纲 →〔页面上改〕→ 生成 PPT → 预览下载。
 * 长任务都跑在服务端后台线程里，这里轮询 /api/job/<id> 拿结构化进度 + 日志。
 */
const $ = (id) => document.getElementById(id);

const state = {
  mode: 'upload',        // upload | existing
  uploads: [],           // {key,name,size,id,status,error,removed}
  activeKey: null,
  rootSrc: '',           // 「选择已有文档」模式下选中的文件名
  outline: null,
  parsedPath: null,
  stem: null,
  cfg: null,
  busy: false,
  pollToken: 0,          // 作废过期轮询：换任务后旧 timer 自然失效
  statusTimer: null,
  statusMsg: '',
};

const MAX_FILES = 8;

/* ── 工具 ─────────────────────────────────────────────────── */
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

async function api(path, body, opts) {
  const o = Object.assign({}, opts);
  if (body !== undefined) {
    o.method = 'POST';
    o.headers = { 'Content-Type': 'application/json' };
    o.body = JSON.stringify(body);
  }
  const r = await fetch(path, o);
  let j;
  try {
    j = await r.json();
  } catch (_) {
    throw new Error('HTTP ' + r.status + '（响应不是 JSON）');
  }
  if (!r.ok || j.error) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}

function fmtSize(n) {
  if (!n && n !== 0) return '';
  return n < 1024 ? n + ' B'
    : n < 1048576 ? (n / 1024).toFixed(0) + ' KB'
      : (n / 1048576).toFixed(1) + ' MB';
}

function fmtDur(ms) {
  const s = Math.floor(ms / 1000);
  return s < 60 ? s + ' 秒' : Math.floor(s / 60) + ' 分 ' + (s % 60) + ' 秒';
}

/* 全应用唯一的状态通道：文案 + 加载态 + busy 互斥锁。
   加载中带一个「已用时」计时器 —— 单次模型调用最坏能沉默 9 分钟
   （PPTGEN_TIMEOUT × 重试），得让用户看得出它还活着。 */
/* tone: true / 'error' = 红；'warn' = 橙（如「这是兜底产物」——
   不是失败，但必须让人看见）。原有调用传的都是 true，继续有效。 */
function setStatus(msg, loading, tone) {
  state.statusMsg = msg;
  state.busy = !!loading;
  clearInterval(state.statusTimer);
  state.statusTimer = null;

  const el = $('statusBar');
  el.classList.toggle('loading', !!loading);
  el.classList.toggle('error', tone === true || tone === 'error');
  el.classList.toggle('warn', tone === 'warn');
  document.body.classList.toggle('is-busy', !!loading);

  if (loading) {
    const t0 = Date.now();
    const tick = () => { el.textContent = msg + '（已用 ' + fmtDur(Date.now() - t0) + '）'; };
    tick();
    state.statusTimer = setInterval(tick, 1000);
  } else {
    el.textContent = msg;
  }
}

const setError = (msg) => setStatus(msg, false, true);

/* ── 启动 ─────────────────────────────────────────────────── */
async function boot() {
  try {
    const c = await api('/api/config');
    state.cfg = c;
    $('acceptHint').textContent = '支持 ' + c.exts.join(' / ') + '，单个不超过 '
      + Math.round(c.max_upload / 1048576) + ' MB';
    $('files').accept = c.exts.join(',');
    $('cfg').innerHTML =
      '<div>文本模型 <b class="' + (c.llm ? '' : 'off') + '">'
      + esc(c.llm || '未配置') + '</b></div>'
      + '<div>视觉模型 <b class="' + (c.vision ? '' : 'off') + '">'
      + esc(c.vision || '未配置') + '</b></div>'
      + '<div>页数 ' + c.pages[0] + '–' + c.pages[1]
      + ' · 渲染 <b class="' + (c.officecli ? '' : 'off') + '">'
      + (c.officecli ? 'officecli' : '缺失') + '</b></div>';

    const srcs = await api('/api/sources');
    const sel = $('src');
    const roots = srcs.filter((s) => s.kind === 'root');
    sel.innerHTML = roots.length
      ? roots.map((s) => '<option value="' + esc(s.name) + '">'
        + esc(s.name) + '　(' + fmtSize(s.size) + ')</option>').join('')
      : '<option value="">（项目根目录下没有可用文档）</option>';
    sel.onchange = () => { state.rootSrc = sel.value; };

    // 之前上传过的（含服务重启后仍在磁盘上的）恢复成胶囊
    srcs.filter((s) => s.kind === 'upload').slice(0, MAX_FILES).forEach((s) => {
      state.uploads.push({ key: 'disk:' + s.name, name: s.name, size: s.size,
        id: null, status: 'ready', error: '' });
    });
    if (state.uploads.length) state.activeKey = state.uploads[0].key;

    setSourceMode('upload');
    renderFiles();
    renderOutline(state.outline);
    if (state.uploads.length) {
      setStatus('已恢复 ' + state.uploads.length + ' 个上传文件');
    }
  } catch (e) {
    $('cfg').innerHTML = '<b style="color:#D31245">无法连接服务</b>';
    setError('无法连接服务：' + e.message);
  }
}

/* ── 选源模式 ─────────────────────────────────────────────── */
function setSourceMode(mode) {
  state.mode = mode;
  document.querySelectorAll('input[name=sourceMode]').forEach((r) => {
    r.checked = (r.value === mode);
  });
  $('dropZone').classList.toggle('disabled', mode !== 'upload');
  $('src').disabled = (mode !== 'existing');
}
document.querySelectorAll('input[name=sourceMode]').forEach((r) => {
  r.onchange = () => { if (r.checked) setSourceMode(r.value); };
});

/* 当前选中的源。上传模式下是那枚「使用中」的胶囊，否则是下拉里的文件。 */
function currentSource() {
  if (state.mode === 'existing') {
    const v = $('src').value;
    return v ? { src: v } : null;
  }
  const e = state.uploads.find((u) => u.key === state.activeKey && u.status === 'ready');
  if (!e) return null;
  return e.id ? { upload_id: e.id, name: e.name } : { src: e.name };
}

/* ── 上传 ─────────────────────────────────────────────────── */
$('browseBtn').onclick = () => $('files').click();
$('files').onchange = (e) => {
  addFiles(e.target.files);
  e.target.value = '';            // 允许再次选同一个文件
};

const dropZone = $('dropZone');
['dragenter', 'dragover'].forEach((ev) => {
  dropZone.addEventListener(ev, (e) => {
    if (state.mode !== 'upload') return;
    e.preventDefault();
    dropZone.classList.add('dragover');
  });
});
['dragleave', 'drop'].forEach((ev) => {
  dropZone.addEventListener(ev, (e) => {
    e.preventDefault();
    if (ev === 'dragleave' && dropZone.contains(e.relatedTarget)) return;
    dropZone.classList.remove('dragover');
  });
});
dropZone.addEventListener('drop', (e) => {
  if (state.mode !== 'upload') return;
  addFiles(e.dataTransfer.files);
});

async function addFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;

  const exts = (state.cfg && state.cfg.exts) || [];
  const max = (state.cfg && state.cfg.max_upload) || 40 * 1024 * 1024;

  for (const f of files) {
    if (state.uploads.length >= MAX_FILES) {
      setError('最多 ' + MAX_FILES + ' 个文件，超出的已忽略');
      break;
    }
    const dot = f.name.lastIndexOf('.');
    const ext = dot < 0 ? '' : f.name.slice(dot).toLowerCase();
    const entry = { key: 'f' + Date.now() + '_' + state.uploads.length,
      name: f.name, size: f.size, id: null, status: 'uploading', error: '' };

    // 前端先拦一道，省一个来回
    if (exts.length && exts.indexOf(ext) < 0) {
      entry.status = 'bad';
      entry.error = '不支持的格式 ' + (ext || '(无扩展名)');
    } else if (f.size > max) {
      entry.status = 'bad';
      entry.error = '文件太大（' + fmtSize(f.size) + '，上限 ' + fmtSize(max) + '）';
    }

    state.uploads.push(entry);
    if (entry.status === 'uploading') normalizeActive();
    renderFiles();

    if (entry.status === 'bad') continue;
    try {
      const r = await api('/api/upload?name=' + encodeURIComponent(f.name),
        undefined, { method: 'POST', body: f });
      entry.id = r.id;
      entry.name = r.name;
      entry.size = r.size;
      entry.status = 'ready';
      if (entry.removed) await dropUpload(entry);      // 上传途中被删了，补一刀
    } catch (e) {
      entry.status = 'bad';
      entry.error = e.message.length > 160 ? e.message.slice(0, 160) + '…' : e.message;
    }
    if (entry.removed) removeEntry(entry.key);
    normalizeActive();            // 这一枚刚变成 ready，可能该轮到它「使用中」
    renderFiles();
    if (entry.status === 'bad') setError('「' + entry.name + '」上传失败：' + entry.error);
  }
  if (state.uploads.some((u) => u.status === 'ready')) {
    setStatus('已就绪 ' + state.uploads.filter((u) => u.status === 'ready').length + ' 个文件');
  }
}

async function dropUpload(entry) {
  try { await api('/api/uploads/' + encodeURIComponent(entry.name), undefined,
    { method: 'DELETE' }); } catch (_) { /* 尽力而为 */ }
}

function removeEntry(key) {
  state.uploads = state.uploads.filter((u) => u.key !== key);
  normalizeActive();
}

/* 「使用中」必须落在一个 ready 的胶囊上。删掉它、或它上传失败时，
   得把标记让给下一个可用的，否则选中项会停在一个不可用的条目上，
   点生成只得到一句「请先上传」而看不出为什么。 */
function normalizeActive() {
  const cur = state.uploads.find((u) => u.key === state.activeKey && u.status === 'ready');
  if (cur) return;
  const next = state.uploads.find((u) => u.status === 'ready');
  state.activeKey = next ? next.key : null;
}

$('fileList').onclick = async (e) => {
  const pill = e.target.closest('.file-pill');
  if (!pill) return;
  const entry = state.uploads.find((u) => u.key === pill.dataset.key);
  if (!entry) return;

  if (e.target.closest('.remove-file')) {
    if (entry.status === 'uploading') {          // 还没传完：标记，传完自动删
      entry.removed = true;
      removeEntry(entry.key);
    } else {
      if (entry.id || entry.status === 'ready') await dropUpload(entry);
      removeEntry(entry.key);
    }
    renderFiles();
    return;
  }
  if (entry.status === 'ready') {
    state.activeKey = entry.key;
    setSourceMode('upload');
    renderFiles();
  }
};

function renderFiles() {
  const box = $('fileList');
  box.innerHTML = state.uploads.map((u) => {
    const active = u.key === state.activeKey && u.status === 'ready';
    const tag = u.status === 'uploading' ? '上传中…'
      : u.status === 'bad' ? esc(u.error || '失败')
        : active ? '使用中' : '点击选用';
    return '<span class="file-pill' + (active ? ' active' : '')
      + (u.status === 'bad' ? ' bad' : '') + '" data-key="' + esc(u.key) + '">'
      + (u.status === 'uploading' ? '<span class="spin"></span>' : '')
      + '<span class="file-name" title="' + esc(u.name) + '">' + esc(u.name) + '</span>'
      + '<span class="file-tag">' + tag + '</span>'
      + '<button type="button" class="remove-file" title="移除"'
      + (u.status === 'uploading' ? ' disabled' : '') + '>×</button></span>';
  }).join('');
}

/* ── 生成大纲 ─────────────────────────────────────────────── */
$('btnOutline').onclick = async () => {
  if (state.busy) return;
  const src = currentSource();
  if (!src) {
    return setError(state.mode === 'existing'
      ? '请先在下拉里选一份文档'
      : '请先上传一个文件，或点选已就绪的胶囊');
  }
  $('btnOutline').disabled = true;
  $('btnGenerate').disabled = true;
  clearJob();
  setStatus('正在解析并生成大纲…', true);
  try {
    const { job_id } = await api('/api/outline', src);
    const job = await poll(job_id);
    if (job.error) throw new Error(job.error);

    state.outline = job.result.outline;
    state.parsedPath = job.result.parsed_path;
    state.stem = job.result.stem;
    $('deckName').value = (state.stem || 'deck').slice(0, 24);
    renderOutline(state.outline);
    $('btnGenerate').disabled = false;
    const meta = state.outline._meta || {};
    setStatus((meta.generated_by === 'llm' ? '大纲已生成：' : '大纲已生成（⚠️ 兜底产物）：')
              + (state.outline.page_count || 0) + ' 页正文，可在下方直接修改',
              false, meta.generated_by !== 'llm' ? 'warning' : 'info');
    $('outlinePanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) {
    setError('生成大纲失败：' + e.message);
  } finally {
    $('btnOutline').disabled = false;
  }
};

/* ── 大纲编辑 ─────────────────────────────────────────────── */
/* 兜底产物「看起来」和模型产出一样，这是它最危险的地方：模型调用失败时
   界面照样显示一份大纲，用户完全不知道内容取舍与标题主张都没生效过。
   所以来源不是 llm 就在最显眼的位置挂一条告警。 */
function renderOutlineWarn(meta) {
  const box = $('outlineWarn');
  const warn = (meta && meta.warnings) || [];
  if (meta && meta.generated_by === 'llm' && !warn.length) {
    box.hidden = true;
    box.innerHTML = '';
    return;
  }
  const why = warn.length ? warn.map((w) => '<li>' + esc(w) + '</li>').join('') : '';
  box.innerHTML = '<strong>⚠️ 这份大纲是确定性兜底产物，不是模型产出。</strong>'
    + '<div>结构能跑完，但<strong>内容取舍、标题主张、版式与语义的匹配都未生效</strong>，'
    + '产出会明显单调。</div>'
    + (why ? '<ul>' + why + '</ul>' : '');
  box.hidden = false;
}

function renderOutline(o) {
  const box = $('outlineEditor');
  if (!o) {
    box.innerHTML = '';
    $('outlineMeta').textContent = '';
    $('outlineJson').value = '';
    renderOutlineWarn(null);
    $('outlineWarn').hidden = true;
    return;
  }
  // 这些键由服务端保证存在，但人工编辑过的 JSON 未必 —— 直接取值会
  // 让整块大纲渲染崩掉，而崩了之后页面上什么都不显示，很难看出原因。
  const range = o._page_range || [0, 0];
  const sections = o.sections || [];
  $('outlineMeta').textContent =
    '共 ' + (o.page_count || 0) + ' 页正文　目标 ' + range[0] + '–' + range[1];
  renderOutlineWarn(o._meta);
  let n = 0;
  box.innerHTML = sections.map((s, si) => {
    const pages = (s.pages || []).map((p, pi) => {
      n += 1;
      // source 一直没在界面上露出过，而它正是「这页内容有据可依」的证据
      const tip = [p.hint, p.source].filter(Boolean).join('\n');
      return '<div class="page">'
        + '<span class="pageno">' + String(n).padStart(2, '0') + '</span>'
        + '<input data-si="' + si + '" data-pi="' + pi + '" value="' + esc(p.title) + '">'
        + '<span class="hinttag" title="' + esc(tip) + '">'
        + esc(p.hint || '') + '</span></div>';
    }).join('');
    return '<div class="sec">'
      + '<input class="secname" data-si="' + si + '" value="' + esc(s.name) + '">'
      + '<div class="secsum">' + esc(s.summary || '') + '</div>'
      + pages + '</div>';
  }).join('');

  box.querySelectorAll('input').forEach((inp) => {
    inp.oninput = () => {
      const si = +inp.dataset.si;
      if (inp.dataset.pi === undefined) o.sections[si].name = inp.value;
      else o.sections[si].pages[+inp.dataset.pi].title = inp.value;
      o.toc = o.sections.map((x) => x.summary ? x.name + ' —— ' + x.summary : x.name);
      $('outlineJson').value = JSON.stringify(o, null, 2);
    };
  });
  $('outlineJson').value = JSON.stringify(o, null, 2);
}

$('outlineJson').oninput = () => {
  try {
    const o = JSON.parse($('outlineJson').value);
    if (o && o.sections) {
      state.outline = o;
      $('outlineMeta').textContent = '已从 JSON 更新（' + o.sections.length + ' 章）';
    }
  } catch (_) { /* 编辑中，忽略 */ }
};

/* ── 生成 PPT ─────────────────────────────────────────────── */
$('btnGenerate').onclick = async () => {
  if (state.busy) return;
  let outline = state.outline;
  try {
    const t = JSON.parse($('outlineJson').value);
    if (t && t.sections) outline = t;
  } catch (_) { /* 用编辑器里的版本 */ }
  if (!outline) return setError('大纲为空，请先生成大纲');

  $('btnGenerate').disabled = true;
  $('btnOutline').disabled = true;
  clearJob();
  setStatus('正在规划版式并渲染…', true);
  try {
    const { job_id } = await api('/api/generate', {
      outline: outline,
      parsed_path: state.parsedPath,
      name: $('deckName').value || 'deck',
      rounds: +$('rounds').value || 0,
    });
    const job = await poll(job_id);
    if (job.error) throw new Error(job.error);
    showResult(job.result);
    const s = job.result.summary || {};
    setStatus('完成：' + job.result.slides + ' 页，几何检查 '
      + (s.error || 0) + ' error / ' + (s.warn || 0) + ' warn');
    $('resultPanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) {
    setError('生成失败：' + e.message);
  } finally {
    $('btnGenerate').disabled = false;
    $('btnOutline').disabled = false;
  }
};

/* ── 轮询 ─────────────────────────────────────────────────── */
function clearJob() {
  state.pollToken += 1;                 // 旧轮询就此失效，不会再写 DOM
  $('log').textContent = '';
  $('log').classList.add('empty');
  $('jobState').textContent = '';
  renderProgress([]);
}

function poll(jobId) {
  const token = ++state.pollToken;
  return new Promise((resolve, reject) => {
    const tick = async () => {
      if (token !== state.pollToken) return;          // 已被新任务取代
      let j;
      try {
        j = await api('/api/job/' + jobId);
      } catch (e) {
        return reject(e);
      }
      const lines = j.log || [];
      const logEl = $('log');
      logEl.classList.toggle('empty', !lines.length);
      logEl.textContent = lines.length ? lines.join('\n') : '（等待输出…）';
      logEl.scrollTop = logEl.scrollHeight;
      renderProgress(j.progress || []);
      $('jobState').textContent =
        j.status === 'running' ? '运行中…'
          : j.status === 'done' ? '完成' : '失败';
      if (j.status === 'running') {
        setTimeout(tick, 1200);
        return;
      }
      resolve(j);
    };
    tick();
  });
}

function renderProgress(items) {
  const panel = $('progressPanel');
  const list = $('progressList');
  if (!items.length) {
    panel.classList.add('hidden');
    list.innerHTML = '';
    return;
  }
  panel.classList.remove('hidden');
  list.innerHTML = items.map((it) =>
    '<div class="progress-item ' + esc(it.level || 'info') + '">'
    + '<span class="progress-dot"></span><div>'
    + '<p>' + esc(it.message) + '</p>'
    + (it.detail ? '<small>' + esc(it.detail) + '</small>' : '')
    + '</div></div>').join('');
  list.scrollTop = list.scrollHeight;
}

/* ── 结果 ─────────────────────────────────────────────────── */
function showResult(r) {
  const s = r.summary || {};
  const err = s.error || 0;
  const warn = s.warn || 0;
  // 未配模型时 repair 会跳过回环、回一个空 report —— 那不是「检查通过」
  const cls = err ? 'err' : warn ? 'warn' : 'ok';
  $('qaBadge').innerHTML = '<span class="badge ' + cls + '">几何检查 '
    + err + ' error / ' + warn + ' warn</span>';

  const dl = $('downloadArea');
  dl.classList.remove('empty');
  dl.innerHTML = '<div class="download-row">'
    + '<a class="dl" href="' + esc(r.download) + '">下载 pptx（'
    + r.slides + ' 页）</a>'
    + '<span class="meta">公司封面 + 目录 + ' + (r.slides - 3)
    + ' 页正文 + 封底</span></div>';

  const g = $('previewGrid');
  const pages = r.pages || [];
  if (pages.length) {
    g.innerHTML = pages.map((p, i) =>
      '<div class="preview-card">'
      + '<img loading="lazy" src="/preview/' + encodeURIComponent(r.name) + '/'
      + encodeURIComponent(p) + '" alt="第 ' + (i + 1) + ' 页">'
      + '<span>第 ' + (i + 1) + ' 页</span></div>').join('');
  } else {
    g.innerHTML = '<p class="hint">没有渲染图 —— 检查 officecli 是否可用'
      + '（缺了它不影响 pptx 本身，只是看不到逐页预览）。</p>';
  }
}

/* ── 新建 ─────────────────────────────────────────────────── */
$('newBtn').onclick = async () => {
  if (state.busy) return;
  for (const u of state.uploads) {
    if (u.status === 'ready') await dropUpload(u);
  }
  state.uploads = [];
  state.activeKey = null;
  state.outline = null;
  state.parsedPath = null;
  state.stem = null;
  state.rootSrc = '';
  state.pollToken += 1;
  $('deckName').value = 'mydeck';
  $('rounds').value = 3;
  clearJob();
  renderFiles();
  renderOutline(null);
  $('btnGenerate').disabled = true;
  $('qaBadge').innerHTML = '';
  const dl = $('downloadArea');
  dl.classList.add('empty');
  dl.textContent = '生成完成后，这里会出现下载链接与逐页预览。';
  $('previewGrid').innerHTML = '';
  setSourceMode('upload');
  setStatus('等待输入');
};

boot();
