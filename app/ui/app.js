/* 视频猎手 前端逻辑：无框架，原生 DOM + 轮询 */
'use strict';

const $ = (id) => document.getElementById(id);
const MAX_RENDER = 400;

const state = {
  entries: [],
  selected: new Set(),
  jobId: null,
  scanning: false,
  paused: false,
  tasks: [],
  settings: {},
  healthTimer: null,
  filter: 'all',
  timer: null,
};

/* ---------------- 工具 ---------------- */

function fmtBytes(n) {
  if (!n || n <= 0) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v : v.toFixed(1)) + ' ' + units[i];
}

function fmtDur(sec) {
  if (!sec || sec <= 0) return '';
  const s = Math.round(sec);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  if (h) return `${h}:${String(m).padStart(2, '0')}:${String(r).padStart(2, '0')}`;
  return `${m}:${String(r).padStart(2, '0')}`;
}

function fmtSpeed(bps) {
  if (!bps || bps <= 0) return '';
  return fmtBytes(bps) + '/s';
}

function fmtEta(sec) {
  if (!sec || sec <= 0) return '';
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return m ? `剩余 ${m}分${s}秒` : `剩余 ${s}秒`;
}

function toast(msg, ms = 2200) {
  const el = $('toast');
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, ms);
}

async function api(path, options) {
  const res = await fetch(path, Object.assign({
    headers: { 'Content-Type': 'application/json' },
  }, options || {}));
  let data = {};
  try { data = await res.json(); } catch (_) { /* 非 JSON 响应 */ }
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || `请求失败(${res.status})`);
  }
  return data;
}

/* ---------------- 初始化 ---------------- */

async function loadHealth() {
  try {
    const d = await api('api/health');
    $('diskChip').textContent = '可用空间 ' + d.disk_free;
    $('engineChip').style.color = d.browser ? 'var(--success)' : 'var(--text-3)';
    const probing = !d.browser && d.browser_state === 'probing';
    if (d.browser) {
      $('engineChip').textContent = '浏览器渲染已启用' + (d.browser_version ? ' · ' + d.browser_version : '');
      $('engineChip').title = d.browser_render_error ? ('上次渲染异常：' + d.browser_render_error) : '';
    } else if (probing) {
      $('engineChip').textContent = '正在检测浏览器内核…';
      $('engineChip').title = '首次安装需要几秒探测内核，探测期间仍可用静态解析扫描';
    } else {
      $('engineChip').textContent = '浏览器渲染不可用（悬停看原因）';
      $('engineChip').title = d.browser_reason || '';
    }
    $('dirHint').textContent = '保存到：' + d.download_dir;

    const eng = $('setEngine');
    if (eng) {
      if (d.browser) eng.textContent = '内核状态：可用' + (d.browser_version ? ' · ' + d.browser_version : '');
      else if (probing) eng.textContent = '内核状态：检测中…';
      else eng.textContent = '内核状态：不可用（' + (d.browser_reason || '未知原因') + '）';
    }
    const tools = $('setTools');
    if (tools) {
      const ff = d.ffmpeg ? ('可用' + (d.ffmpeg_version ? ' · ' + d.ffmpeg_version : ''))
        : ('不可用（' + (d.ffmpeg_reason || '未内置') + '）');
      const imp = d.impersonate ? '已就绪' : ('未启用' + (d.impersonate_reason ? '（' + d.impersonate_reason + '）' : ''));
      tools.textContent = 'ffmpeg：' + ff + '　指纹伪装：' + imp;
    }
    if (probing) scheduleHealthRetry();
  } catch (err) {
    $('engineChip').textContent = '服务未就绪';
    $('engineChip').style.color = 'var(--danger)';
  }
}

function scheduleHealthRetry() {
  if (state.healthTimer) return;
  state.healthTimer = setTimeout(() => {
    state.healthTimer = null;
    loadHealth();
  }, 2000);
}

async function loadSettings() {
  try {
    const d = await api('api/settings');
    const s = d.settings || {};
    state.settings = s;
    $('setDir').value = s.download_dir || '';
    $('setDirHint').textContent = '当前实际目录：' + (s._resolved_dir || '—');
    const dl = $('dirOptions');
    if (dl && Array.isArray(s._share_dirs)) {
      dl.innerHTML = s._share_dirs.map(function (p) {
        return '<option value="' + p.replace(/"/g, '&quot;') + '"></option>';
      }).join('');
    }
    $('setWorkers').value = s.workers || 2;
    $('setFormat').value = s.format || 'bv*+ba/b';
    $('setRate').value = s.rate_limit_ms != null ? s.rate_limit_ms : 300;
    $('setMaxVerify').value = s.max_verify || 60;
    $('setCookies').value = s.cookies_file || '';
    $('setProxy').value = s.proxy || '';
    $('optBrowser').checked = !!s.use_browser;
    $('optImpersonate').checked = s.impersonate !== false;
    $('optDepth').value = String(s.crawl_depth != null ? s.crawl_depth : 2);
  } catch (err) {
    /* 首屏拿不到设置不阻塞扫描 */
  }
}

/* ---------------- 扫描 ---------------- */

async function startScan() {
  const url = $('urlInput').value.trim();
  if (!url) { toast('请先填写网址'); $('urlInput').focus(); return; }
  if (state.scanning) return;

  // 扫描参数即时生效，不必进设置页
  try {
    await api('api/settings', {
      method: 'POST',
      body: JSON.stringify({
        use_browser: $('optBrowser').checked,
        crawl_depth: Number($('optDepth').value),
      }),
    });
  } catch (_) { /* 设置同步失败仍继续扫描 */ }

  state.scanning = true;
  state.paused = false;
  state.entries = [];
  state.selected.clear();
  $('btnScan').disabled = true;
  $('btnScan').textContent = '扫描中…';
  $('btnPauseScan').disabled = false;
  syncPauseBtn();
  $('resultCard').hidden = true;
  $('logCard').hidden = true;
  $('scanStatus').hidden = false;
  $('scanSpinner').classList.remove('idle');
  $('scanStage').textContent = '正在连接';
  $('scanMessage').textContent = url;

  try {
    const d = await api('api/scan', { method: 'POST', body: JSON.stringify({ url }) });
    state.jobId = d.job_id;
    pollScan();
  } catch (err) {
    finishScan();
    toast(err.message, 3200);
  }
}

function syncPauseBtn() {
  const btn = $('btnPauseScan');
  if (!btn) return;
  btn.textContent = state.paused ? '继续' : '暂停';
  btn.disabled = !state.scanning;
}

async function pollScan() {
  if (!state.jobId) return;
  try {
    const d = await api('api/scan/' + state.jobId);
    const job = d.job;
    state.paused = !!job.paused;
    $('scanMessage').textContent = job.message || '';
    $('scanStage').textContent = state.paused ? '已暂停' : stageName(job.stage);
    $('scanSpinner').classList.toggle('idle', state.paused);
    syncPauseBtn();

    if (job.status === 'done') {
      const res = job.result || {};
      state.entries = res.entries || [];
      renderResults(res);
      finishScan();
      return;
    }
    if (job.status === 'cancelled') {
      // 停止不是「白干」：把停止前扫到的可下载视频汇总出来
      const res = job.result || {};
      const items = res.entries || [];
      finishScan();
      if (items.length) {
        state.entries = items;
        state.selected = new Set(items.map((e) => e.url));
        renderResults(res, { stopped: true });
        toast(`已停止：汇总到 ${items.length} 个可下载视频，已全选，可直接下载`, 5200);
      } else {
        toast('已停止：这一轮还没扫到视频', 3600);
      }
      return;
    }
    if (job.status === 'error') {
      finishScan();
      const msg = job.error || '未知错误';
      // 网络类失败（被墙/代理）信息量大，多留几秒让人看清
      toast('扫描失败：' + msg, /代理|DNS|握手|阻断/.test(msg) ? 9000 : 4200);
      return;
    }
    setTimeout(pollScan, 900);
  } catch (err) {
    finishScan();
    toast(err.message, 3200);
  }
}

function stageName(stage) {
  return {
    queued: '排队中',
    probe: '识别页面',
    fetch: '抓取页面',
    parse: '解析链接',
    browser: '浏览器渲染',
    crawl: '深度爬取',
    verify: '校验视频',
    paused: '已暂停',
  }[stage] || '处理中';
}

function finishScan() {
  state.scanning = false;
  state.paused = false;
  state.jobId = null;
  $('btnScan').disabled = false;
  $('btnScan').textContent = '扫描';
  $('btnPauseScan').disabled = true;
  $('btnPauseScan').textContent = '暂停';
  $('scanStatus').hidden = true;
}

/* ---------------- 结果渲染 ---------------- */

function filteredEntries() {
  if (state.filter === 'verified') return state.entries.filter((e) => e.verified);
  if (state.filter === 'direct') return state.entries.filter((e) => !e.verified);
  return state.entries;
}

function renderResults(res, opts) {
  const stats = res.stats || {};
  const stopped = !!(opts && opts.stopped);
  const total = (state.entries || []).length;
  const verified = stats.verified || 0;
  const sniff = stats.sniff || {};

  if (stopped) {
    $('resultSummary').textContent =
      `已停止 · 汇总到 ${total} 个可下载视频（已确认 ${verified} 个 / 直链 ${total - verified} 个），`
      + `扫描 ${stats.pages || 1} 页`;
  } else {
    $('resultSummary').textContent =
      `共 ${total} 个视频，已确认 ${verified} 个，扫描 ${stats.pages || 1} 页，耗时 ${stats.elapsed || 0}s`
      + (sniff.dropped ? `（另忽略 ${sniff.dropped} 条特征不足的地址）` : '');
  }

  const warnBox = $('warnBox');
  const warnings = (res.warnings || []).slice();
  if (stopped) warnings.unshift('扫描被手动停止：以上是停止前已扫到的视频，结果不完整。');
  if (warnings.length) {
    warnBox.hidden = false;
    warnBox.textContent = '';
    const ul = document.createElement('ul');
    warnings.slice(0, 6).forEach((w) => {
      const li = document.createElement('li');
      li.textContent = w;
      ul.appendChild(li);
    });
    warnBox.appendChild(ul);
  } else {
    warnBox.hidden = true;
  }

  $('resultCard').hidden = false;
  renderList();
  if (!state.entries.length) {
    const tips = (res.warnings && res.warnings.length)
      ? '没有找到视频，上方是该站的失败原因'
      : '没有找到视频：试试调大爬取深度、开启浏览器渲染，或换列表页地址';
    toast(tips, 4800);
  }
}

function renderList() {
  const list = $('resultList');
  list.textContent = '';
  const items = filteredEntries();
  const slice = items.slice(0, MAX_RENDER);

  slice.forEach((entry) => {
    const li = document.createElement('li');
    li.className = 'result-item';

    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = state.selected.has(entry.url);
    box.addEventListener('change', () => {
      if (box.checked) state.selected.add(entry.url);
      else state.selected.delete(entry.url);
      syncSelectUI();
    });
    li.appendChild(box);

    if (entry.thumbnail) {
      const img = document.createElement('img');
      img.className = 'thumb';
      img.loading = 'lazy';
      img.referrerPolicy = 'no-referrer';
      img.src = entry.thumbnail;
      img.alt = '';
      img.addEventListener('error', () => {
        const ph = document.createElement('div');
        ph.className = 'thumb-fallback';
        ph.textContent = '无封面';
        img.replaceWith(ph);
      });
      li.appendChild(img);
    } else {
      const ph = document.createElement('div');
      ph.className = 'thumb-fallback';
      ph.textContent = '无封面';
      li.appendChild(ph);
    }

    const main = document.createElement('div');
    main.className = 'result-main';
    const title = document.createElement('div');
    title.className = 'result-title';
    title.textContent = entry.title || entry.url;
    title.title = entry.url;
    main.appendChild(title);

    const meta = document.createElement('div');
    meta.className = 'result-meta';
    const parts = [];
    if (entry.duration) parts.push(fmtDur(entry.duration));
    if (entry.size) parts.push(fmtBytes(entry.size));
    if (entry.height) parts.push(entry.height + 'p');
    if (entry.uploader) parts.push(entry.uploader);
    parts.forEach((p) => {
      const span = document.createElement('span');
      span.textContent = p;
      meta.appendChild(span);
    });

    const badge = document.createElement('span');
    badge.className = 'badge' + (entry.verified ? '' : ' direct');
    badge.textContent = entry.verified ? '已确认' : '直链';
    meta.appendChild(badge);
    main.appendChild(meta);
    li.appendChild(main);

    list.appendChild(li);
  });

  const more = $('moreHint');
  if (items.length > MAX_RENDER) {
    more.hidden = false;
    more.textContent = `仅显示前 ${MAX_RENDER} 条，共 ${items.length} 条；全选仍会覆盖全部结果`;
  } else {
    more.hidden = true;
  }
  syncSelectUI();
}

function syncSelectUI() {
  const items = filteredEntries();
  const all = items.length > 0 && items.every((e) => state.selected.has(e.url));
  const some = items.some((e) => state.selected.has(e.url));
  const box = $('checkAll');
  box.checked = all;
  box.indeterminate = !all && some;
  $('btnDownload').disabled = state.selected.size === 0;
  $('btnDownload').textContent = state.selected.size
    ? `下载选中 (${state.selected.size})` : '下载选中';
}

/* ---------------- 下载 ---------------- */

async function startDownload() {
  const urls = Array.from(state.selected).map((url) => {
    const entry = state.entries.find((e) => e.url === url) || {};
    // 带上来源页：直链大多校验 Referer，不带就 403
    return { url, title: entry.title || '', referer: entry.referer || '' };
  });
  if (!urls.length) return;
  try {
    const d = await api('api/download', {
      method: 'POST',
      body: JSON.stringify({ urls }),
    });
    toast(`已加入队列：${d.count} 个`);
    pollTasks();
  } catch (err) {
    toast(err.message, 3200);
  }
}

function taskStateText(t) {
  if (t.status === 'running') {
    const parts = [];
    if (t.percent > 0) parts.push(t.percent.toFixed(0) + '%');
    if (t.speed) parts.push(fmtSpeed(t.speed));
    if (t.eta) parts.push(fmtEta(t.eta));
    return parts.join(' · ') || '开始中';
  }
  return {
    queued: '排队中',
    done: '已完成',
    error: t.error || '失败',
    cancelled: '已停止',
  }[t.status] || t.status;
}

function renderTasks() {
  const list = $('taskList');
  const tasks = state.tasks.slice().reverse();
  list.textContent = '';

  if (!tasks.length) {
    const li = document.createElement('li');
    li.className = 'empty';
    li.textContent = '还没有下载任务';
    list.appendChild(li);
    $('queueSummary').textContent = '暂无任务';
    return;
  }

  const running = tasks.filter((t) => t.status === 'running').length;
  const queued = tasks.filter((t) => t.status === 'queued').length;
  const done = tasks.filter((t) => t.status === 'done').length;
  $('queueSummary').textContent =
    `进行中 ${running}，排队 ${queued}，已完成 ${done}，共 ${tasks.length}`;

  tasks.slice(0, 100).forEach((t) => {
    const li = document.createElement('li');
    li.className = 'task-item';

    const main = document.createElement('div');
    main.className = 'task-main';

    const title = document.createElement('div');
    title.className = 'task-title';
    title.textContent = t.title || t.url;
    title.title = t.filepath || t.url;
    main.appendChild(title);

    const bar = document.createElement('div');
    bar.className = 'bar' + (t.status === 'done' ? ' done' : t.status === 'error' ? ' error' : '');
    const fill = document.createElement('i');
    fill.style.width = Math.max(0, Math.min(100, t.percent || (t.status === 'done' ? 100 : 0))) + '%';
    bar.appendChild(fill);
    main.appendChild(bar);

    const meta = document.createElement('div');
    meta.className = 'task-meta';
    if (t.status === 'done') {
      meta.textContent = fmtBytes(t.total) + ' · ' + (t.filepath ? t.filepath.split('/').pop() : '');
      meta.title = t.filepath || '';
    } else if (t.status === 'error') {
      meta.textContent = t.error || '';
      meta.style.color = 'var(--danger)';
    } else if (t.total) {
      meta.textContent = `${fmtBytes(t.downloaded)} / ${fmtBytes(t.total)}`;
    } else {
      meta.textContent = t.status === 'queued' ? '等待开始' : '获取文件信息…';
    }
    main.appendChild(meta);
    li.appendChild(main);

    const st = document.createElement('span');
    st.className = 'task-state' + (t.status === 'done' ? ' done' : t.status === 'error' ? ' error' : '');
    st.textContent = taskStateText(t);
    li.appendChild(st);

    if (t.status === 'running' || t.status === 'queued') {
      li.appendChild(iconBtn('取消', () => act('tasks/' + t.id + '/cancel')));
    } else if (t.status === 'error' || t.status === 'cancelled') {
      li.appendChild(iconBtn('重试', () => act('tasks/' + t.id + '/retry')));
    }
    list.appendChild(li);
  });
}

function iconBtn(text, fn) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'btn ghost small';
  b.textContent = text;
  b.addEventListener('click', fn);
  return b;
}

async function act(path) {
  try {
    // 注意：接口都在 api/ 下，少这个前缀会被当成静态文件 404（清理已完成点不动就是这个）
    await api('api/' + path, { method: 'POST', body: '{}' });
    pollTasks();
  } catch (err) {
    toast(err.message, 3000);
  }
}

let lastTaskSig = '';
async function pollTasks() {
  try {
    const d = await api('api/tasks');
    const tasks = d.tasks || [];
    const sig = tasks.map((t) => `${t.id}:${t.status}:${Math.round(t.percent)}`).join('|');
    state.tasks = tasks;
    if (sig !== lastTaskSig) {
      lastTaskSig = sig;
      renderTasks();
    }
  } catch (_) { /* 轮询失败静默重试 */ }
}

/* ---------------- 设置 ---------------- */

function openSettings() {
  $('settingsModal').hidden = false;
  loadSettings();
}

async function checkEngine() {
  const btn = $('btnCheckEngine');
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = '检测中…';
  try {
    await api('api/browser/check', { method: 'POST', body: '{}' });
    toast('正在后台重新检测内核…');
    for (let i = 0; i < 20; i++) {
      await new Promise((r) => setTimeout(r, 1500));
      const d = await api('api/health');
      if (d.browser_state !== 'probing') {
        toast(d.browser ? ('内核可用 · ' + (d.browser_version || '版本未知')) : '内核仍不可用，详见上方原因');
        break;
      }
    }
  } catch (err) {
    toast(err.message, 3200);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
    loadHealth();
  }
}

async function saveSettings() {
  const payload = {
    download_dir: $('setDir').value.trim(),
    workers: Number($('setWorkers').value) || 2,
    format: $('setFormat').value,
    rate_limit_ms: Number($('setRate').value) || 0,
    max_verify: Number($('setMaxVerify').value) || 60,
    cookies_file: $('setCookies').value.trim(),
    proxy: $('setProxy').value.trim(),
    impersonate: $('optImpersonate').checked,
  };
  try {
    await api('api/settings', { method: 'POST', body: JSON.stringify(payload) });
    $('settingsModal').hidden = true;
    toast('设置已保存');
    loadHealth();
  } catch (err) {
    toast(err.message, 3200);
  }
}

async function showLog() {
  try {
    const d = await api('api/logs?lines=200');
    $('logBox').textContent = (d.lines || []).join('\n') || '暂无日志';
    $('logCard').hidden = false;
    $('settingsModal').hidden = true;
  } catch (err) {
    toast(err.message, 3000);
  }
}

/* ---------------- 事件绑定 ---------------- */

document.addEventListener('DOMContentLoaded', () => {
  loadHealth();
  loadSettings();
  pollTasks();
  setInterval(pollTasks, 1500);
  // 启动时 ffmpeg / 指纹伪装的自检要起子进程，晚一点再刷新一次状态
  setTimeout(loadHealth, 3500);

  $('btnScan').addEventListener('click', startScan);
  $('urlInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') startScan();
  });
  $('btnPauseScan').addEventListener('click', async () => {
    if (!state.jobId) return;
    const want = !state.paused;
    try {
      await api('api/scan/' + state.jobId + (want ? '/pause' : '/resume'),
        { method: 'POST', body: '{}' });
      state.paused = want;
      $('scanStage').textContent = want ? '已暂停' : stageName('crawl');
      $('scanSpinner').classList.toggle('idle', want);
      syncPauseBtn();
    } catch (err) {
      toast(err.message, 3000);
    }
  });
  $('btnCancelScan').addEventListener('click', async () => {
    if (!state.jobId) return;
    $('scanMessage').textContent = '正在停止，把已扫到的视频整理出来…';
    try {
      await api('api/scan/' + state.jobId + '/cancel', { method: 'POST', body: '{}' });
    } catch (err) {
      toast(err.message, 3000);
    }
  });

  $('checkAll').addEventListener('change', (e) => {
    const items = filteredEntries();
    if (e.target.checked) items.forEach((it) => state.selected.add(it.url));
    else items.forEach((it) => state.selected.delete(it.url));
    renderList();
  });

  $('filterSel').addEventListener('change', (e) => {
    state.filter = e.target.value;
    renderList();
  });

  $('btnDownload').addEventListener('click', startDownload);
  $('btnClear').addEventListener('click', () => act('tasks/clear'));
  $('btnSettings').addEventListener('click', openSettings);
  $('btnCloseSettings').addEventListener('click', () => { $('settingsModal').hidden = true; });
  $('settingsModal').addEventListener('click', (e) => {
    if (e.target === $('settingsModal')) $('settingsModal').hidden = true;
  });
  $('btnSaveSettings').addEventListener('click', saveSettings);
  $('btnCheckEngine').addEventListener('click', checkEngine);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('settingsModal').hidden) $('settingsModal').hidden = true;
  });
  $('btnShowLog').addEventListener('click', showLog);
});
