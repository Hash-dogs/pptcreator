/* 文档转 PPT —— 前端逻辑（原生 JS，无构建步骤） */
const $ = (id) => document.getElementById(id);
const state = { outline: null, parsedPath: null, stem: null, pollTimer: null };

async function api(path, body) {
  const opt = body
    ? { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body) }
    : {};
  const r = await fetch(path, opt);
  const j = await r.json();
  if (!r.ok || j.error) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}

/* ── 初始化 ─────────────────────────────────────────────── */
async function boot() {
  try {
    const c = await api('/api/config');
    $('cfg').innerHTML =
      `文本模型 <b>${c.llm || '未配置'}</b>　` +
      `视觉模型 <b class="${c.vision ? '' : 'off'}">${c.vision || '未配置'}</b>　` +
      `页数 ${c.pages[0]}–${c.pages[1]}　` +
      `渲染 <b class="${c.officecli ? '' : 'off'}">${c.officecli ? 'officecli' : '缺失'}</b>`;
    const srcs = await api('/api/sources');
    const sel = $('src');
    sel.innerHTML = srcs.length
      ? srcs.map(s => `<option value="${s.name}">${s.name}</option>`).join('')
      : '<option value="">（项目根目录下没有可用的文档）</option>';
    sel.onchange = showSrcInfo;
    showSrcInfo();
  } catch (e) {
    $('cfg').innerHTML = '<b style="color:#D31245">无法连接服务：' + e.message + '</b>';
  }
  activate(1);
}

function showSrcInfo() {
  const f = $('src').value;
  $('srcInfo').textContent = f ? '将解析：' + f : '';
}

/* ── 步骤切换 ───────────────────────────────────────────── */
function activate(n) {
  for (let i = 1; i <= 4; i++) {
    const el = $('step' + i);
    el.classList.toggle('muted', i !== n);
    el.classList.toggle('active', i === n);
  }
}

/* ── 步骤 1：生成大纲 ───────────────────────────────────── */
$('btnOutline').onclick = async () => {
  const src = $('src').value;
  if (!src) return alert('请先选择源文档');
  $('btnOutline').disabled = true;
  try {
    const { job_id } = await api('/api/outline', { src });
    activate(3);
    $('log').textContent = '';
    const job = await poll(job_id);
    if (job.error) throw new Error(job.error);
    state.outline = job.result.outline;
    state.parsedPath = job.result.parsed_path;
    state.stem = job.result.stem;
    $('deckName').value = (job.result.stem || 'deck').slice(0, 24);
    renderOutline(job.result.outline);
    activate(2);
  } catch (e) {
    alert('生成大纲失败：' + e.message);
    activate(1);
  } finally {
    $('btnOutline').disabled = false;
  }
};

/* ── 步骤 2：大纲编辑器 ─────────────────────────────────── */
function renderOutline(o) {
  $('outlineMeta').textContent =
    `共 ${o.page_count} 页正文　目标 ${o._page_range[0]}–${o._page_range[1]}`;
  const box = $('outlineEditor');
  box.innerHTML = '';
  let n = 0;
  o.sections.forEach((s, si) => {
    const d = document.createElement('div');
    d.className = 'sec';
    d.innerHTML =
      `<input class="secname" data-si="${si}" value="${esc(s.name)}">
       <div class="secsum">${esc(s.summary || '')}</div>`;
    s.pages.forEach((p, pi) => {
      n++;
      const row = document.createElement('div');
      row.className = 'page';
      row.innerHTML =
        `<span class="pageno">${String(n).padStart(2, '0')}</span>
         <input data-si="${si}" data-pi="${pi}" value="${esc(p.title)}">
         <span class="hinttag" title="${esc(p.hint || '')}">${esc(p.hint || '')}</span>`;
      d.appendChild(row);
    });
    box.appendChild(d);
  });
  // 编辑即写回 state.outline
  box.querySelectorAll('input').forEach(inp => {
    inp.oninput = () => {
      const si = +inp.dataset.si;
      if (inp.dataset.pi === undefined) o.sections[si].name = inp.value;
      else o.sections[si].pages[+inp.dataset.pi].title = inp.value;
      o.toc = o.sections.map(x =>
        x.summary ? `${x.name} —— ${x.summary}` : x.name);
      $('outlineJson').value = JSON.stringify(o, null, 2);
    };
  });
  $('outlineJson').value = JSON.stringify(o, null, 2);
}

$('outlineJson').oninput = () => {
  try {
    const o = JSON.parse($('outlineJson').value);
    if (o && o.sections) { state.outline = o; $('outlineMeta').textContent = '已从 JSON 更新'; }
  } catch (_) { /* 编辑中，忽略 */ }
};

/* ── 步骤 3：生成 ───────────────────────────────────────── */
$('btnGenerate').onclick = async () => {
  let outline = state.outline;
  try {
    const t = JSON.parse($('outlineJson').value);
    if (t && t.sections) outline = t;
  } catch (_) { /* 用编辑器里的版本 */ }
  if (!outline) return alert('大纲为空');

  $('btnGenerate').disabled = true;
  activate(3);
  $('log').textContent = '';
  $('jobState').textContent = '';
  try {
    const { job_id } = await api('/api/generate', {
      outline,
      parsed_path: state.parsedPath,
      name: $('deckName').value || 'deck',
      rounds: +$('rounds').value || 0,
    });
    const job = await poll(job_id);
    if (job.error) throw new Error(job.error);
    showResult(job.result);
    activate(4);
  } catch (e) {
    alert('生成失败：' + e.message);
  } finally {
    $('btnGenerate').disabled = false;
  }
};

/* ── 轮询任务 ───────────────────────────────────────────── */
function poll(jobId) {
  return new Promise((resolve, reject) => {
    const tick = async () => {
      try {
        const j = await api('/api/job/' + jobId);
        $('log').textContent = (j.log || []).join('\n');
        $('log').scrollTop = $('log').scrollHeight;
        $('jobState').textContent =
          j.status === 'running' ? '运行中…' : (j.status === 'done' ? '完成' : '失败');
        if (j.status === 'running') { state.pollTimer = setTimeout(tick, 1200); return; }
        resolve(j);
      } catch (e) { reject(e); }
    };
    tick();
  });
}

/* ── 步骤 4：结果 ───────────────────────────────────────── */
function showResult(r) {
  const s = r.summary || {};
  const ok = (s.error || 0) === 0 && (s.warn || 0) === 0;
  $('qaBadge').innerHTML =
    `<span class="badge ${ok ? 'ok' : 'warn'}">几何检查 ${s.error || 0} error / ` +
    `${s.warn || 0} warn</span>`;
  $('resultBar').innerHTML =
    `<a class="dl" href="${r.download}">下载 pptx（${r.slides} 页）</a>
     <span class="srcinfo">共 ${r.slides} 页：公司封面 + 目录 + 正文 + 封底</span>`;
  const g = $('gallery');
  g.innerHTML = '';
  (r.pages || []).forEach((p, i) => {
    const f = document.createElement('figure');
    f.innerHTML =
      `<img loading="lazy" src="/preview/${encodeURIComponent(r.name)}/${p}" alt="第${i + 1}页">
       <figcaption>第 ${i + 1} 页</figcaption>`;
    g.appendChild(f);
  });
  if (!(r.pages || []).length) {
    g.innerHTML = '<p class="hint">渲染图未生成（检查 officecli 是否可用）。</p>';
  }
}

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

boot();
