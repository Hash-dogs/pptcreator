/* 版式管理页：查看图鉴 / 上传截图识别新版式 / 启用禁用 / 删除自定义版式。
 *
 * 与主页（app.js）是两个页面，但共用同一份 style.css 与同一批组件 class。
 * 下面三个小工具（esc / api / setStatus）是**照抄 app.js 的**：它是无构建的
 * 单文件脚本，没有模块导出机制，为一个 4 行的转义函数去动现有页面的加载顺序
 * 不划算。改动时注意两边一起动 —— 或者等哪天抽成 common.js。
 */
const $ = (id) => document.getElementById(id);

const state = {
  items: [],
  filter: 'all',
  busy: false,
  draft: null,
  pollToken: 0,
  statusTimer: null,
};

const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');

async function api(path, body) {
  const opt = body === undefined
    ? {}
    : { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  let j = null;
  try { j = await r.json(); } catch (e) { throw new Error('HTTP ' + r.status + '（响应不是 JSON）'); }
  if (!r.ok || j.error) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}

function setStatus(msg, loading, tone) {
  const el = $('statusBar');
  el.className = 'status sidebar-status'
    + (loading ? ' loading' : '') + (tone === 'warn' ? ' warn' : (tone ? ' error' : ''));
  el.textContent = msg;
  if (state.statusTimer) { clearInterval(state.statusTimer); state.statusTimer = null; }
  if (loading) {
    const t0 = Date.now();
    state.statusTimer = setInterval(() => {
      el.textContent = msg + '（已用时 ' + Math.round((Date.now() - t0) / 1000) + ' 秒）';
    }, 1000);
  }
}

const setError = (m) => setStatus(m, false, true);

/* ── 清单与图鉴 ──────────────────────────────────────────── */

async function loadLayouts() {
  const j = await api('/api/layouts');
  state.items = j.items || [];
  const custom = state.items.filter((x) => x.source === 'custom').length;
  $('countMeta').textContent = '内置 ' + (state.items.length - custom) + ' 套 · 自定义 '
    + custom + ' 套 · 已禁用 ' + (j.disabled || []).length + ' 套';
  $('cfg').innerHTML = '版式目录<br><span class="muted">' + esc(j.dir) + '</span><br>'
    + (j.vision ? '视觉模型：已配置' : '<span class="warn">视觉模型：未配置，无法识别截图</span>');
  // 模型名可疑时把提醒显示出来（服务端给的话）—— 名单只提醒，不拦配置，
  // 所以这里不能让入口消失，只能把话说清楚。
  $('visionHint').innerHTML = j.vision
    ? (j.vision_note
        ? '<span class="warn">' + esc(j.vision_note) + '</span>'
        : '视觉模型已配置')
    : '未配置视觉模型（PPTGEN_VISION_*），这个入口用不了';
  $('btnPreviews').disabled = !j.officecli;
  renderGrid();
}

function renderGrid() {
  const f = state.filter;
  const list = state.items.filter((x) =>
    f === 'all' ? true : (f === 'off' ? !x.enabled : x.source === f));
  if (!list.length) {
    $('grid').innerHTML = '<p class="hint">这个筛选下没有版式。</p>';
    return;
  }
  $('grid').innerHTML = list.map((x) => {
    const img = x.preview
      ? '<img loading="lazy" src="' + esc(x.preview) + '" alt="' + esc(x.name) + '">'
      : '<div class="no-preview">还没有预览图</div>';
    const del = x.source === 'custom'
      ? '<button type="button" class="link danger" data-del="' + esc(x.name) + '">删除</button>'
      : '';
    return ''
      + '<div class="preview-card layout-card' + (x.enabled ? '' : ' off') + '" data-name="'
      + esc(x.name) + '">'
      + (x.preview ? '<div class="shot" data-zoom="' + esc(x.preview) + '">' + img + '</div>' : img)
      + '<div class="cap">'
      + '<div class="cap-line"><strong>' + esc(x.name) + '</strong>'
      + '<span class="badge ' + (x.source === 'custom' ? 'ok' : '') + '">'
      + (x.source === 'custom' ? '自定义' : '内置') + '</span>'
      + (x.enabled ? '' : '<span class="badge warn">已禁用</span>')
      + '</div>'
      + '<div class="meta">' + esc(x.intent_label) + (x.items ? ' · ' + esc(x.items) : '')
      + (x.blocks ? ' · ' + x.blocks + ' 个区块' : '') + '</div>'
      + '<div class="hint">' + esc(x.signature) + '</div>'
      + '<div class="row-actions">'
      + '<button type="button" class="link" data-toggle="' + esc(x.name) + '">'
      + (x.enabled ? '禁用' : '启用') + '</button>' + del
      + '</div></div></div>';
  }).join('');
}

/* ── 启停与删除 ─────────────────────────────────────────── */

async function toggle(name, enabled) {
  try {
    await api('/api/layouts/state', { name: name, enabled: enabled });
    setStatus((enabled ? '已启用 ' : '已禁用 ') + name + '（下次生成生效）', false);
    await loadLayouts();
  } catch (e) { setError(e.message); }
}

async function removeLayout(name) {
  if (!confirm('删除自定义版式「' + name + '」？'
      + '用到它的旧 PPT 会打不开 —— 如果只是想不再选中它，用「禁用」。')) return;
  try {
    // DELETE 不经过 api()：那个封装只做 GET/POST，删除是第三个方法
    const r = await fetch('/api/layouts/' + encodeURIComponent(name), { method: 'DELETE' });
    const j = await r.json();
    if (!r.ok || j.error) throw new Error(j.error || ('HTTP ' + r.status));
    setStatus('已删除 ' + name, false);
    await loadLayouts();
  } catch (e) { setError(e.message); }
}

/* ── 上传截图 → 识别 ─────────────────────────────────────── */

async function uploadShot(file) {
  $('shotName').innerHTML = '<span class="file-pill active">' + esc(file.name) + '</span>';
  setStatus('上传截图…', true);
  const r = await fetch('/api/layouts/recognize?name=' + encodeURIComponent(file.name),
    { method: 'POST', body: file });
  const j = await r.json();
  if (!r.ok || j.error) throw new Error(j.error || ('HTTP ' + r.status));
  $('draftShot').src = URL.createObjectURL(file);
  $('draftPanel').classList.remove('hidden');
  $('draftMeta').innerHTML = '';
  $('draftWarn').hidden = true;
  $('btnAdopt').disabled = true;
  state.draft = null;
  return pollJob(j.job_id);
}

/* 轮询后台任务（与 app.js 的 poll 同一套：job 只活在内存里，断了就没了） */
function poll(jobId) {
  const token = ++state.pollToken;
  return new Promise((resolve, reject) => {
    const tick = async () => {
      if (token !== state.pollToken) return;         // 被新任务作废
      let j;
      try { j = await api('/api/job/' + jobId); } catch (e) { return reject(e); }
      renderProgress(j.progress || []);
      if (j.status === 'running') { setTimeout(tick, 1200); return; }
      if (j.status === 'failed') return reject(new Error(j.error || '任务失败'));
      resolve(j.result || {});
    };
    tick();
  });
}

function renderProgress(items) {
  const panel = $('progressPanel');
  if (!items.length) { panel.classList.add('hidden'); return; }
  panel.classList.remove('hidden');
  $('progressList').innerHTML = items.map((p) =>
    '<div class="progress-item ' + esc(p.level) + '">'
    + '<span class="progress-dot"></span><span>' + esc(p.message) + '</span></div>').join('');
}

async function pollJob(jobId) {
  try {
    const res = await poll(jobId);
    setStatus(res.ok ? '识别通过，请查看试片' : (res.reason || '识别未通过'), false,
      res.ok ? false : 'warn');
    await showDraft(res.rid);
    return res;
  } catch (e) {
    setError('识别失败：' + e.message);
    return null;
  }
}

/* ── 草稿：原图 vs 试片，采用 / 放弃 ──────────────────────── */

async function showDraft(rid) {
  if (!rid) return;
  let d;
  try { d = await api('/api/layout-draft/' + encodeURIComponent(rid)); }
  catch (e) { return setError('取回识别结果失败：' + e.message); }
  state.draft = d;
  $('draftPanel').classList.remove('hidden');
  const meta = d.meta || {};
  $('draftName').textContent = d.ok ? (meta.name || '识别结果') : '这次没识别出来';
  $('draftBadge').className = 'badge ' + (d.ok ? 'ok' : 'err');
  $('draftBadge').textContent = d.ok ? '可以采用' : '不合格';
  if (d.preview) $('draftTrial').src = d.preview;
  $('draftTrial').style.display = d.preview ? '' : 'none';

  const rows = [];
  if (d.ok) {
    rows.push(['版式名', meta.name]);
    rows.push(['分类', (meta.intents || []).join('、') || '—']);
    rows.push(['容量', (meta.min_items || '—') + '–' + (meta.max_items || '—') + ' 条'
      + (meta.item_chars ? '，单条 ≤' + meta.item_chars + ' 字' : '')]);
    rows.push(['视觉特征', meta.signature]);
    rows.push(['适合', meta.best_for + '　不适合：' + meta.avoid_for]);
    rows.push(['降级到', (meta.fallback || []).join(' → ')]);
    if (d.geometry) {
      rows.push(['几何自检', d.geometry.error + ' error / ' + d.geometry.warn + ' warn']);
    }
    rows.push(['构图比对', d.verify && d.verify.reason ? d.verify.reason : '已通过']);
    // 重试过就说出来（静默重试与静默截断是同一类问题）
    (d.attempts || []).forEach((a) => {
      rows.push(['第 ' + a.round + ' 轮', '不合格（已自动修正）：'
        + (a.problems || []).join('；')]);
    });
  } else {
    rows.push(['原因', d.reason]);
    (d.attempts || []).forEach((a) => {
      rows.push(['第 ' + a.round + ' 轮', (a.problems || []).join('；')]);
    });
  }
  $('draftMeta').innerHTML = rows.map((r) =>
    '<div class="draft-row"><span class="muted">' + esc(r[0]) + '</span>'
    + '<span>' + esc(r[1]) + '</span></div>').join('');
  $('draftWarn').hidden = !!d.ok;
  if (!d.ok) $('draftWarn').textContent = d.reason || '';
  $('btnAdopt').disabled = !d.ok;
  $('draftPanel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function adopt() {
  if (!state.draft) return;
  $('btnAdopt').disabled = true;
  try {
    const j = await api('/api/layouts/adopt', { rid: state.draft.rid });
    setStatus('已采用 ' + j.name + '（现在它可以被选中了）', false);
    $('draftPanel').classList.add('hidden');
    state.draft = null;
    await loadLayouts();
  } catch (e) { setError('采用失败：' + e.message); $('btnAdopt').disabled = false; }
}

async function discard() {
  if (state.draft) {
    try { await api('/api/layouts/discard', { rid: state.draft.rid }); } catch (e) { /* 丢弃失败无所谓 */ }
  }
  state.draft = null;
  $('draftPanel').classList.add('hidden');
  setStatus('已放弃这次识别，版式库没有变化', false);
}

/* ── 预览图 ─────────────────────────────────────────────── */

async function genPreviews() {
  try {
    setStatus('正在渲染预览图…', true);
    const j = await api('/api/layouts/previews', {});
    await pollJob(j.job_id);
    setStatus('预览图已更新', false);
    await loadLayouts();
  } catch (e) { setError('预览图生成失败：' + e.message); }
}

/* ── 事件绑定 ───────────────────────────────────────────── */

$('filter').onchange = (e) => { state.filter = e.target.value; renderGrid(); };
$('browseBtn').onclick = () => $('shotFile').click();
$('shotFile').onchange = (e) => {
  const f = e.target.files[0];
  if (f) uploadShot(f).catch((err) => setError(err.message));
};
$('dropZone').ondragover = (e) => { e.preventDefault(); };
$('dropZone').ondrop = (e) => {
  e.preventDefault();
  const f = e.dataTransfer.files[0];
  if (f) uploadShot(f).catch((err) => setError(err.message));
};
$('btnAdopt').onclick = adopt;
$('btnDiscard').onclick = discard;
$('btnPreviews').onclick = genPreviews;

$('grid').onclick = (e) => {
  const t = e.target.closest('[data-toggle]');
  if (t) {
    const it = state.items.find((x) => x.name === t.dataset.toggle);
    return toggle(t.dataset.toggle, it ? !it.enabled : true);
  }
  const d = e.target.closest('[data-del]');
  if (d) return removeLayout(d.dataset.del);
  const z = e.target.closest('[data-zoom]');
  if (z) { $('lbImg').src = z.dataset.zoom; $('lightbox').classList.remove('hidden'); }
};

$('lightbox').onclick = () => $('lightbox').classList.add('hidden');
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') $('lightbox').classList.add('hidden');
});

loadLayouts().then(() => setStatus('就绪', false))
  .catch((e) => setError('读取版式清单失败：' + e.message));
