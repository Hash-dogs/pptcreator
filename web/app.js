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
  previewUrls: [],       // 逐页预览图的 URL，弹层翻页要用（deck 名出了
                         // showResult 就没了，所以必须存下来）
  result: null,          // 整个生成结果（按页修订要用 deck_live / page_index，
                         // 这些同样出了 showResult 就没了）
  pageMeta: [],          // 与 previewUrls 平行的页面清单，来自服务端的 page_index。
                         // 页码映射（预览 N ↔ slides[N-3]）只在服务端算一次，
                         // 这里只显示不算 —— 前端再算一遍就是第二处真相。
  lbIndex: -1,           // 弹层当前页码（0 基）；-1 = 没开
  lbBefore: false,       // 弹层是否正在看「改动前」那一帧
  picked: {},            // 预览页号 → 是否勾选（用于「引用选中的页」）
  revise: null,          // 当前的修改方案（服务端返回的 items + sha）
  beforeUrls: {},        // 预览页号 → 「改动前」快照的 URL
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

/* 目录行的拼法，与后端 `pipeline._toc_line` 对齐。

   目录页那 10.12" 的占位符在 24pt 下一行只放得下约 28 字，而目录页
   **不在几何检查范围内**（后端默认跳过第 1/2/最后一页）—— 超了没有任何东西会报。
   后端 `_normalise_plan` 还会按真实宽度再夹一次（前端随时可能贴一份手改 JSON
   进来），这里只是让编辑时的预览不出现长得离谱的行。 */
const TOC_MAX = 30;
const TOC_SEP = ' —— ';
function tocLine(name, summary) {
  name = (name || '').slice(0, TOC_MAX);
  summary = (summary || '').trim();
  if (!summary) return name;
  const room = TOC_MAX - name.length - TOC_SEP.length;
  if (room < 4) return name;
  return name + TOC_SEP + summary.slice(0, room);
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
  // page_count 只数**正文页**：章节分隔页是结构页，不占正文页数预算，
  // 但确实会出现在成品里 —— 不写出来的话，用户会以为页数算错了。
  const div = o.divider_count || 0;
  $('outlineMeta').textContent =
    '共 ' + (o.page_count || 0) + ' 页正文　目标 ' + range[0] + '–' + range[1]
    + (div ? '　+ ' + div + ' 页章节分隔页（合计 ' + (o.total_pages || 0) + ' 页）' : '');
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
      o.toc = o.sections.map((x) => tocLine(x.name, x.summary));
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
  // 按页修订要用的东西（deck_live / page_index）出了这里就没了，存下来
  state.result = r;
  state.pageMeta = r.page_index || [];
  state.picked = {};
  // 「改动前」快照的文件名（apply 的结果里才有），弹层用来做对比
  state.beforeUrls = {};
  const before = r.before || {};
  const name = encodeURIComponent(r.name);
  Object.keys(before).forEach((pg) => {
    state.beforeUrls[+pg] = '/preview/' + name + '/' + encodeURIComponent(before[pg]);
  });
  // 缩略图用的就是全尺寸原图（没有单独的缩略图接口），所以点开放大
  // 是纯本地行为，不会再发一次请求 —— 弹层直接复用这些 URL。
  // **重渲过的那几页要带版本号**：URL 不变而图变了，浏览器会给旧图。
  const fresh = new Set((r.rendered || []).map(Number));
  const bust = Date.now();
  state.previewUrls = pages.map((p, i) =>
    '/preview/' + name + '/' + encodeURIComponent(p)
    + (fresh.has(i + 1) ? '?v=' + bust : ''));
  if (pages.length) {
    g.innerHTML = pages.map((p, i) => {
      const meta = state.pageMeta[i] || {};
      // 卡片上写的就是服务端算好的 label（「预览 05 · 正文 03」）——
      // 用户说「第 5 页」时指的那个数，必须和这里显示的是同一个。
      // 没有 page_index（老结果、拿不到 deck）时退回序号，不至于没得看。
      const label = meta.label || ('第 ' + (i + 1) + ' 页');
      const tip = [label, meta.layout, meta.headline].filter(Boolean).join(' · ');
      const changed = (r.changed || []).indexOf(i + 1) >= 0;
      // 模板页（封面/目录/封底）也能微调，只是不能重做 —— 不用特别标出来，
      // 提交后服务端会按类型给出准确的说法。
      return '<div class="preview-card' + (meta.kind ? ' k-' + esc(meta.kind) : '')
        + (changed ? ' changed' : '') + '" data-idx="' + i + '"'
        + ' data-kind="' + esc(meta.kind || '') + '" title="' + esc(tip) + '">'
        + '<label class="pick" title="勾选后可用「引用选中的页」">'
        + '<input type="checkbox" data-pick="' + (i + 1) + '"></label>'
        + (meta.layout ? '<span class="layout-tag">' + esc(meta.layout) + '</span>' : '')
        + '<img loading="lazy" src="' + state.previewUrls[i] + '"'
        + ' alt="' + esc(label) + '">'
        + '<span>' + esc(label) + '</span></div>';
    }).join('');
  } else {
    g.innerHTML = '<p class="hint">没有渲染图 —— 检查 officecli 是否可用'
      + '（缺了它不影响 pptx 本身，只是看不到逐页预览）。</p>';
  }
  // 拿得到 deck 才能按页改（CLI 产物、历史结果可能没有）
  $('revisePanel').classList.toggle('hidden', !r.deck_live);
  $('diffPanel').classList.add('hidden');
  state.revise = null;
  renderPickedHint();
}

/* ── 预览弹层 ─────────────────────────────────────────────── */
/* 点缩略图放大看整页。左右方向键/按钮翻页，到头循环。
 *
 * 键盘是「不常驻」的：全站只有这一处监听 keydown，所以弹层关着的时候
 * 必须直接 return —— 否则方向键会被这里吃掉，#outlineJson 那个
 * textarea 里就没法用方向键移光标了。
 */
function openLightbox(idx) {
  if (idx < 0 || idx >= state.previewUrls.length) return;
  state.lbIndex = idx;
  $('lightbox').classList.remove('hidden');
  paintLightbox();
}

function paintLightbox() {
  const n = state.previewUrls.length;
  const i = state.lbIndex;
  const pg = i + 1;
  const b = state.beforeUrls[pg];
  // 改动过的页才有「改前」那一帧；有就显示切换按钮，没有就藏起来 ——
  // 而不是显示一个点了没反应的按钮
  const btn = $('lbToggle');
  btn.classList.toggle('hidden', !b);
  if (!b) state.lbBefore = false;
  $('lbImg').src = (state.lbBefore && b) ? b : state.previewUrls[i];
  $('lbImg').alt = '第 ' + pg + ' 页' + (state.lbBefore ? '（改动前）' : '');
  btn.textContent = state.lbBefore ? '看改动后' : '看改动前';
  $('lbPage').textContent = '第 ' + pg + ' / ' + n + ' 页'
    + (b ? (state.lbBefore ? ' · 改动前' : ' · 改动后') : '');
}

function stepLightbox(d) {
  const n = state.previewUrls.length;
  if (!n) return;
  // 循环：一屏看完整个 deck 比撞到头停住更顺
  state.lbIndex = (state.lbIndex + d + n) % n;
  paintLightbox();
}

function closeLightbox() {
  state.lbIndex = -1;
  state.lbBefore = false;
  $('lightbox').classList.add('hidden');
  // 清掉 src，免得下次打开时先闪一下上一张
  $('lbImg').removeAttribute('src');
}

$('previewGrid').onclick = (e) => {
  // 勾选框是卡片上的另一个控件：点它不该开弹层。（卡片上唯一能点的
  // 「选中」入口就是这个 —— 不改那套键盘翻页逻辑，它比选中功能更值钱。）
  if (e.target.closest('.pick')) return;
  const card = e.target.closest('.preview-card');
  if (!card) return;
  openLightbox(Number(card.dataset.idx));
};

$('previewGrid').onchange = (e) => {
  const box = e.target.closest('input[data-pick]');
  if (!box) return;
  state.picked[+box.dataset.pick] = box.checked;
  renderPickedHint();
};

$('lbClose').onclick = closeLightbox;
$('lbPrev').onclick = () => stepLightbox(-1);
$('lbNext').onclick = () => stepLightbox(1);
$('lbToggle').onclick = () => { state.lbBefore = !state.lbBefore; paintLightbox(); };

// 点图片本身不关，点四周的背景才关
$('lightbox').onclick = (e) => {
  if (e.target === $('lightbox')) closeLightbox();
};

document.onkeydown = (e) => {
  if (state.lbIndex < 0) return;
  if (e.key === 'Escape') { closeLightbox(); }
  else if (e.key === 'ArrowLeft') { stepLightbox(-1); }
  else if (e.key === 'ArrowRight') { stepLightbox(1); }
  else { return; }
  e.preventDefault();
};

/* ── 按页修改 ─────────────────────────────────────────────── */
/* 一个输入框说多个页面要改什么；顶部一个开关决定**整框**按哪种模式走
 * （服务端据此分派：patch = 只改字段、版式不变；rewrite = 整页重做、可换版式）。
 *
 * 「生成修改方案」只提案、不落盘 —— 用户逐条看过（标量新值还能直接手改）
 * 再点「应用」。这是这一整套设计里最关键的一步：自然语言改稿最容易崩的地方
 * 就是「它到底动了哪几个字」，所以 diff 必须挡在落盘之前。
 */
function pickedPages() {
  return Object.keys(state.picked).filter((k) => state.picked[k])
    .map(Number).sort((a, b) => a - b);
}

function renderPickedHint() {
  const got = pickedPages();
  $('pickedHint').textContent = got.length ? ('已勾选 ' + got.join('、') + ' 页') : '';
}

function reviseMode() {
  const el = document.querySelector('input[name="reviseMode"]:checked');
  return el ? el.value : 'patch';
}

function headlineOf(sl) {
  if (!sl) return '';
  if (sl.title) return sl.title;
  const flat = (v) => (Array.isArray(v) ? v.map(flat).join('')
    : (v && typeof v === 'object' ? Object.keys(v).filter((k) => !['hl', 'size', 'bold', 'color'].includes(k)).map((k) => flat(v[k])).join('') : String(v == null ? '' : v)));
  for (const k of ['lines', 'quote', 'body', 'claim', 'lead']) {
    if (sl[k]) { const t = flat(sl[k]).trim(); if (t) return t; }
  }
  return '';
}

$('btnQuote').onclick = () => {
  const got = pickedPages();
  if (!got.length) return setStatus('先在预览图上勾选要改的页', false, 'warn');
  const ta = $('reviseText');
  ta.value = (ta.value.trim() ? ta.value.replace(/\s+$/, '') + '\n' : '')
    + '第 ' + got.join('、') + ' 页：';
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
};

$('btnRevise').onclick = async () => {
  if (state.busy) return;
  const text = $('reviseText').value.trim();
  if (!text) return setStatus('先写下要改什么', false, 'warn');
  const deck = state.result && state.result.deck_live;
  if (!deck) return setStatus('这份结果没有可修改的 deck', false, 'warn');

  $('btnRevise').disabled = true;
  clearJob();
  setStatus('正在理解你的要求并生成方案…', true);
  try {
    const { job_id } = await api('/api/revise',
      { deck: deck, mode: reviseMode(), text: text });
    const job = await poll(job_id);
    if (job.error) throw new Error(job.error);
    showDiff(job.result);
    setStatus('方案已生成 —— 逐条确认后点「应用选中的修改」');
  } catch (e) {
    setError('生成方案失败：' + e.message);
  } finally {
    $('btnRevise').disabled = false;
  }
};

function diffRow(it, i) {
  const rej = it.status === 'reject';
  const page = state.pageMeta[it.preview - 1] || {};
  const label = page.label || ('第 ' + it.preview + ' 页');
  const badge = rej ? '<span class="badge err">不能应用</span>'
    : it.status === 'warn' ? '<span class="badge warn">有提醒</span>'
      : '<span class="badge ok">可直接应用</span>';
  let body = '';
  if (it.new) {
    body += '<div class="diff-line">版式 <code>' + esc(it.was_layout || page.layout || '?')
      + '</code> → <code>' + esc(it.new.layout || '?') + '</code></div>'
      + '<div class="diff-line">新大字：<ins>' + esc(headlineOf(it.new) || '（无）')
      + '</ins></div>';
  }
  (it.changes || []).forEach((c, j) => {
    // 标量新值可以手改（改不了的那种是列表/富文本，一个 input 装不下）
    const editable = typeof c.after === 'string' && c.after.length <= 80;
    body += '<div class="diff-line"><code>' + esc(c.path) + '</code>'
      + '<del>' + esc(c.before) + '</del>'
      + (editable
        ? '<input class="diff-val" data-item="' + i + '" data-op="' + j
          + '" value="' + esc(c.after) + '">'
        : '<ins>' + esc(c.after) + '</ins>')
      + (c.why ? '<small>' + esc(c.why) + '</small>' : '') + '</div>';
  });
  if (it.reason) body += '<div class="diff-why">' + esc(it.reason) + '</div>';
  return '<div class="diff-row' + (rej ? ' reject' : '') + '">'
    + '<div class="diff-head">'
    + '<label class="pick"><input type="checkbox" data-accept="' + i + '"'
    + (rej ? ' disabled' : ' checked') + '></label>'
    + '<strong>' + esc(label) + '</strong>' + badge + '</div>'
    + '<div class="diff-req">「' + esc(it.quote || it.request || '') + '」</div>'
    + body + '</div>';
}

function showDiff(res) {
  state.revise = res;
  const items = res.items || [];
  const ok = items.filter((x) => x.status !== 'reject').length;
  $('diffMeta').textContent = ok + ' 条可应用 / 共 ' + items.length + ' 条'
    + ((res.unclear || []).length ? '，另有 ' + res.unclear.length + ' 条待澄清' : '')
    + '（' + (res.mode === 'rewrite' ? '整页重做' : '小范围修改') + '）';
  $('diffList').innerHTML = items.map(diffRow).join('')
    + (res.unclear || []).map((u) => '<div class="diff-row reject">'
      + '<div class="diff-head"><span class="badge warn">待澄清</span></div>'
      + '<div class="diff-why">' + esc(u) + '</div></div>').join('');
  $('diffPanel').classList.remove('hidden');
  $('diffPanel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function collectAccepted() {
  const out = [];
  const items = (state.revise && state.revise.items) || [];
  document.querySelectorAll('#diffList input[data-accept]').forEach((box) => {
    if (!box.checked) return;
    const it = items[+box.dataset.accept];
    if (!it) return;
    const item = { who: +box.dataset.accept, preview: it.preview,
                   request: it.request, mode: it.mode };
    if (it.new) item.new = it.new;              // 整页重做：把确认过的那一版带回去
    if (it.ops) item.ops = JSON.parse(JSON.stringify(it.ops));
    out.push(item);
  });
  // 面板上被手改过的新值以用户为准。**按条目下标找，不能按页码找** ——
  // 同一页可以有多条意见（「标题压短；那条说明也改短」），按页码会张冠李戴。
  document.querySelectorAll('#diffList input.diff-val').forEach((el) => {
    const item = out.find((x) => x.who === +el.dataset.item);
    if (item && item.ops && item.ops[+el.dataset.op]) {
      item.ops[+el.dataset.op].value = el.value;
    }
  });
  out.forEach((x) => { delete x.who; });
  return out;
}

/* 把服务端报出来的大纲补丁合并回页面上这份大纲。
 *
 * 为什么必须做：`/api/generate` 的输入是**页面上的 `state.outline`**
 * （见 btnGenerate），不是磁盘上那份。服务端虽然已经把封面标题/目录条目
 * 写回了 outline.json，但用户不刷新页面就不会重新读盘 —— 于是「改完封面
 * 标题 → 直接点生成 PPT」会把刚改的覆盖回去。两处都写才闭合。
 */
function mergeOutlinePatch(patch) {
  if (!patch || !patch.length || !state.outline) return 0;
  let n = 0;
  patch.forEach((p) => {
    if (!p || p.after === undefined) return;
    if (p.field === 'cover.title') { state.outline.title = p.after; n++; }
    else if (p.field === 'cover.subtitle') { state.outline.subtitle = p.after; n++; }
    else if (p.field === 'toc') { state.outline.toc = p.after; n++; }
  });
  if (n) renderOutline(state.outline);      // 编辑区与 JSON 文本域一起刷
  return n;
}

$('btnDiscard').onclick = () => {
  state.revise = null;
  $('diffPanel').classList.add('hidden');
  setStatus('已丢弃修改方案');
};

$('btnApply').onclick = async () => {
  if (state.busy) return;
  const items = collectAccepted();
  if (!items.length) return setStatus('没有勾选任何一条', false, 'warn');
  const deck = state.result && state.result.deck_live;
  if (!deck) return;

  $('btnApply').disabled = true;
  clearJob();
  setStatus('正在应用 ' + items.length + ' 条修改并重渲改动的页…', true);
  try {
    const { job_id } = await api('/api/revise/apply', {
      deck: deck, mode: reviseMode(), items: items,
      sha: state.revise.sha, rid: state.revise.rid,
    });
    const job = await poll(job_id);
    if (job.error) throw new Error(job.error);
    const r = job.result;
    state.revise = null;
    showResult(r);
    // 封面标题/目录条目是**从大纲派生的**（重新规划时会被重算）——
    // 所以服务端写盘之外，页面上这份大纲也要同步，否则下次点「生成 PPT」会丢
    const merged = mergeOutlinePatch(r.outline_patch);
    const s = r.summary || {};
    setStatus('已修改 ' + (r.changed || []).length + ' 页（重渲 '
      + (r.rendered || []).length + ' 张），几何检查 '
      + (s.error || 0) + ' error / ' + (s.warn || 0) + ' warn'
      + (merged ? '；同时更新了大纲里的 ' + merged + ' 处' : ''));
    $('resultPanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) {
    setError('应用失败：' + e.message);
  } finally {
    $('btnApply').disabled = false;
  }
};

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
  state.previewUrls = [];
  state.lbIndex = -1;
  $('lightbox').classList.add('hidden');
  setSourceMode('upload');
  setStatus('等待输入');
};

boot();
