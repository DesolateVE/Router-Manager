const API = '';
let proxies = [], groups = [], rules = [], settings = {};
let bindings = [];
let tunnels = [];
let portBindings = [];
let subRuleSets = {};  // dict: set_name -> [SubRuleEntry]
let ruleProviders = {};  // dict: name -> RuleProvider
let proxyLatency = {};  // id -> ms (null=testing, -1=error)
let _connWs = null, _connData = [], _connPrev = null, _connPrevTime = 0, _connPrevMap = {};
let _connWsStopped = false;
const MIHOMO_CONFIG_DIRTY_KEY = 'routerManagerConfigDirty';
const SING_BOX_CONFIG_DIRTY_KEY = 'routerManagerSingBoxConfigDirty';

// Rule modal context:
// null = main rule,  string = adding new entry to that set,  {setName, idx} = editing entry
let ruleModalContext = null;

// Sub-rule side panel state
let srCurrentSet = null;  // null = root, string = current set name
let _srSetNames = [];     // ordered list of set names for safe index-based handling

// ── Toast ──
let _toastTimer = null;
function toast(msg, type = 'success') {
  const t = document.getElementById('toast');
  if (_toastTimer) { clearTimeout(_toastTimer); _toastTimer = null; }
  t.textContent = msg;
  t.className = 'toast show ' + type;
  _toastTimer = setTimeout(() => { t.className = 'toast'; _toastTimer = null; }, 2500);
}

function renderConfigDirtyBanner() {
  const banner = document.getElementById('configDirtyBanner');
  const reloadBtn = document.getElementById('btnReload');
  if (!banner || !reloadBtn) return;
  const isDirty = isMihomoConfigDirty() || isSingBoxConfigDirty();
  banner.style.display = isDirty ? 'flex' : 'none';
  reloadBtn.classList.toggle('btn-attention', isDirty);
}

function isMihomoConfigDirty() { return localStorage.getItem(MIHOMO_CONFIG_DIRTY_KEY) === '1'; }
function isSingBoxConfigDirty() { return localStorage.getItem(SING_BOX_CONFIG_DIRTY_KEY) === '1'; }

function markMihomoConfigDirty() {
  localStorage.setItem(MIHOMO_CONFIG_DIRTY_KEY, '1');
  renderConfigDirtyBanner();
}

function markSingBoxConfigDirty() {
  localStorage.setItem(SING_BOX_CONFIG_DIRTY_KEY, '1');
  renderConfigDirtyBanner();
}

function clearMihomoConfigDirty() {
  localStorage.removeItem(MIHOMO_CONFIG_DIRTY_KEY);
  renderConfigDirtyBanner();
}

function clearSingBoxConfigDirty() {
  localStorage.removeItem(SING_BOX_CONFIG_DIRTY_KEY);
  renderConfigDirtyBanner();
}

function shouldMarkMihomoConfigDirty(path, method) {
  if (!['POST', 'PUT', 'DELETE'].includes(method)) return false;
  return [
    '/api/proxies',
    '/api/groups',
    '/api/rules',
    '/api/sub-rules',
    '/api/rule-providers',
    '/api/import/',
    '/api/device-bindings',
    '/api/tunnels'
  ].some(prefix => path.startsWith(prefix));
}

function shouldMarkSingBoxConfigDirty(path, method) {
  if (!['POST', 'PUT', 'DELETE'].includes(method)) return false;
  return [
    '/api/proxies',
    '/api/import/',
    '/api/port-bindings'
  ].some(prefix => path.startsWith(prefix));
}

function shouldClearMihomoConfigDirty(path, method) {
  if (method !== 'POST') return false;
  return path === '/api/apply' || path === '/api/reload' || path === '/api/start' || path === '/api/restart';
}

function shouldClearSingBoxConfigDirty(path, method) {
  if (method !== 'POST') return false;
  return path === '/api/apply' || path === '/api/sing-box/start' || path === '/api/sing-box/restart';
}

// ── API helpers ──
async function api(path, opts = {}) {
  const method = (opts.method || 'GET').toUpperCase();
  const res = await fetch(API + path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { const j = await res.json(); detail = j.detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  if (shouldClearMihomoConfigDirty(path, method)) clearMihomoConfigDirty();
  else if (shouldMarkMihomoConfigDirty(path, method)) markMihomoConfigDirty();
  if (shouldClearSingBoxConfigDirty(path, method)) clearSingBoxConfigDirty();
  else if (shouldMarkSingBoxConfigDirty(path, method)) markSingBoxConfigDirty();
  return res.json();
}

// ── Tab switching ──
let _logPollTimer = null;
function activateTab(tabName) {
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  const tab = document.querySelector('.nav-item[data-tab="' + tabName + '"]');
  const panel = document.getElementById('panel-' + tabName);
  if (tab) tab.classList.add('active');
  if (panel) panel.classList.add('active');
  if (tabName === 'logs') {
    refreshLogs();
    if (_logPollTimer) clearInterval(_logPollTimer);
    _logPollTimer = setInterval(refreshLogs, 2000);
  } else if (_logPollTimer) {
    clearInterval(_logPollTimer);
    _logPollTimer = null;
  }
  if (tabName === 'dashboard') {
    renderDashboardSummary();
  }
}

document.querySelectorAll('.nav-item').forEach(item => {
  item.addEventListener('click', () => {
    activateTab(item.dataset.tab);
  });
});

// ── Modal ──
function closeModal(id) { document.getElementById(id).classList.remove('show'); }
function showModal(id) { document.getElementById(id).classList.add('show'); }

function toggleProcessPanel(event) {
  if (event) event.stopPropagation();
  document.getElementById('processPopover').classList.toggle('show');
}

document.addEventListener('click', () => {
  const popover = document.getElementById('processPopover');
  if (popover) popover.classList.remove('show');
});

// ── Utils ──
function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }
function escHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function formatBytes(b) {
  if (b == null) return '--';
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
  if (b < 1073741824) return (b / 1048576).toFixed(1) + ' MB';
  return (b / 1073741824).toFixed(2) + ' GB';
}

// ── Connections WebSocket ──
function startConnWs() {
  _connWsStopped = false;
  _doConnWs();
}

function stopConnWs() {
  _connWsStopped = true;
  if (_connWs) { _connWs.onclose = null; _connWs.close(); _connWs = null; }
  _connData = []; _connPrev = null; _connPrevMap = {};
}

function _doConnWs() {
  if (_connWs || _connWsStopped) return;
  if (!settings || !settings.mihomo_api_port) return;
  const host = window.location.hostname || '127.0.0.1';
  const qs = settings.mihomo_api_secret ? '?token=' + encodeURIComponent(settings.mihomo_api_secret) : '';
  const wsUrl = 'ws://' + host + ':' + settings.mihomo_api_port + '/connections' + qs;
  try { _connWs = new WebSocket(wsUrl); } catch (_) { return; }
  _connWs.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      const now = Date.now();
      const dt = _connPrevTime > 0 ? (now - _connPrevTime) / 1000 : 0;
      const conns = data.connections || [];
      const newMap = {};
      conns.forEach(c => {
        const prev = _connPrevMap[c.id];
        c._dlSpeed = (dt > 0 && prev) ? Math.max(0, (c.download - prev.dl) / dt) : 0;
        c._ulSpeed = (dt > 0 && prev) ? Math.max(0, (c.upload - prev.ul) / dt) : 0;
        newMap[c.id] = { dl: c.download, ul: c.upload };
      });
      _connPrevMap = newMap;
      let dlSpeed = 0, ulSpeed = 0;
      if (_connPrev && dt > 0) {
        dlSpeed = Math.max(0, (data.downloadTotal - _connPrev.dl) / dt);
        ulSpeed = Math.max(0, (data.uploadTotal - _connPrev.ul) / dt);
      }
      _connPrev = { dl: data.downloadTotal, ul: data.uploadTotal };
      _connPrevTime = now;
      _connData = conns;
      updateDashboardStats(data, dlSpeed, ulSpeed);
      renderConnTable();
    } catch (_) {}
  };
  _connWs.onclose = () => { _connWs = null; if (!_connWsStopped) setTimeout(_doConnWs, 4000); };
  _connWs.onerror = () => {};
}

function updateDashboardStats(data, dlSpeed, ulSpeed) {
  const el = (id) => document.getElementById(id);
  if (el('statDownload')) el('statDownload').textContent = formatBytes(data.downloadTotal);
  if (el('statUpload'))   el('statUpload').textContent   = formatBytes(data.uploadTotal);
  if (el('statDlSpeed'))  el('statDlSpeed').textContent  = formatSpeed(dlSpeed);
  if (el('statUlSpeed'))  el('statUlSpeed').textContent  = formatSpeed(ulSpeed);
  if (el('statMemory'))   el('statMemory').textContent   = formatBytes(data.memory);
}

function renderDashboardSummary() {
  const modeMap = { rule: '规则', global: '全局', direct: '直连' };
  const modeEl = document.getElementById('statMode');
  const controllerEl = document.getElementById('statController');
  if (modeEl) {
    modeEl.textContent = modeMap[settings.mode] || settings.mode || '--';
  }
  if (controllerEl) {
    controllerEl.textContent = settings.mihomo_api_port ? ':' + settings.mihomo_api_port : '--';
  }
}

function formatSpeed(bps) {
  if (!bps) return '0 B/s';
  if (bps < 1024)       return Math.round(bps) + ' B/s';
  if (bps < 1048576)    return (bps / 1024).toFixed(1) + ' KB/s';
  if (bps < 1073741824) return (bps / 1048576).toFixed(1) + ' MB/s';
  return (bps / 1073741824).toFixed(2) + ' GB/s';
}

function elapsedTime(startIso) {
  const sec = Math.floor((Date.now() - new Date(startIso)) / 1000);
  if (sec < 60)   return sec + 's';
  if (sec < 3600) return Math.floor(sec / 60) + 'm' + (sec % 60) + 's';
  return Math.floor(sec / 3600) + 'h' + Math.floor((sec % 3600) / 60) + 'm';
}

async function closeConn(id) {
  try { await api('/api/connections/' + encodeURIComponent(id), { method: 'DELETE' }); }
  catch (e) { toast('关闭失败: ' + e.message, 'error'); }
}

async function closeAllConns() {
  try { await api('/api/connections', { method: 'DELETE' }); toast('已关闭全部连接'); }
  catch (e) { toast('关闭失败: ' + e.message, 'error'); }
}

// ── Status/Control ──
async function refreshStatus() {
  try {
    const s = await api('/api/status');
    const running = s.mihomo_running;
    const singBoxRunning = s.sing_box_running;
    const runtimeEl = document.getElementById('statRuntime');
    const mihomoProcessStatusEl = document.getElementById('mihomoProcessStatus');
    const singBoxProcessStatusEl = document.getElementById('singBoxProcessStatus');
    if (runtimeEl) runtimeEl.textContent = running ? '在线' : '离线';
    if (mihomoProcessStatusEl) {
      mihomoProcessStatusEl.innerHTML = running
        ? '<span class="status-dot on"></span>运行中 (PID: ' + s.mihomo_pid + ')'
        : '<span class="status-dot off"></span>已停止';
    }
    if (singBoxProcessStatusEl) {
      singBoxProcessStatusEl.innerHTML = singBoxRunning
        ? '<span class="status-dot on"></span>运行中 (PID: ' + s.sing_box_pid + ')'
        : '<span class="status-dot off"></span>已停止';
    }
  } catch (e) { console.error(e); }
}

async function controlMihomo(action) {
  try {
    await api('/api/' + action, { method: 'POST' });
    if (action === 'start' || action === 'restart') clearMihomoConfigDirty();
    toast(action === 'start' ? '已启动' : action === 'stop' ? '已停止' : '已重启');
    if (action === 'stop') { stopConnWs(); }
    if (action === 'start' || action === 'restart') { setTimeout(startConnWs, 1200); }
    setTimeout(refreshStatus, 500);
  } catch (e) { toast('操作失败: ' + e.message, 'error'); }
}

async function controlSingBox(action) {
  try {
    await api('/api/sing-box/' + action, { method: 'POST' });
    toast(action === 'start' ? 'sing-box 已启动' : action === 'stop' ? 'sing-box 已停止' : 'sing-box 已重启');
    setTimeout(refreshStatus, 500);
  } catch (e) { toast('sing-box 操作失败: ' + e.message, 'error'); }
}

// ── Load data ──
async function loadAll() {
  try {
    [proxies, groups, rules, settings, subRuleSets, ruleProviders, bindings, tunnels, portBindings] = await Promise.all([
      api('/api/proxies'), api('/api/groups'), api('/api/rules'),
      api('/api/settings'), api('/api/sub-rules'), api('/api/rule-providers'),
      api('/api/device-bindings'), api('/api/tunnels'), api('/api/port-bindings')
    ]);
    renderProxies();
    renderGroups();
    renderRules();
    renderBindings();
    renderTunnels();
    renderPortBindings();
    loadSettingsUI();
    renderDashboardSummary();
    document.getElementById('statProxies').textContent = proxies.length;
    document.getElementById('statGroups').textContent = groups.length;
    document.getElementById('statRules').textContent = rules.length;
    startConnWs();
    if (document.getElementById('subRulePanel').classList.contains('show')) renderSubRulePanel();
    if (document.getElementById('ruleProviderPanel').classList.contains('show')) renderRuleProviderPanel();
  } catch (e) { console.error(e); }
}

let currentPreviewCore = 'mihomo';

function setPreviewCore(core) {
  currentPreviewCore = core === 'sing-box' ? 'sing-box' : 'mihomo';
  document.getElementById('previewCoreMihomo').classList.toggle('active', currentPreviewCore === 'mihomo');
  document.getElementById('previewCoreSingBox').classList.toggle('active', currentPreviewCore === 'sing-box');
  loadPreview();
}

async function loadPreview() {
  const previewCore = currentPreviewCore;
  const viewer = document.getElementById('yamlPreview');
  try {
    const path = previewCore === 'sing-box' ? '/api/sing-box/config/preview' : '/api/config/preview';
    const res = await fetch(API + path);
    if (!res.ok) throw new Error(res.statusText);
    const configText = await res.text();
    if (previewCore !== currentPreviewCore) return;
    viewer.textContent = configText;
  } catch (e) {
    if (previewCore === currentPreviewCore) {
      viewer.textContent = '加载 ' + previewCore + ' 配置失败：' + (e.message || '未知错误');
      toast('加载配置预览失败', 'error');
    }
  }
}

async function openConfigPreview() {
  activateTab('settings');
  await loadPreview();
  const preview = document.getElementById('yamlPreview');
  if (preview) preview.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// ── Proxies ──
function renderProxies() {
  const el = document.getElementById('proxyList');
  if (!proxies.length) { el.innerHTML = '<div style="text-align:center;color:var(--text2);padding:40px">暂无代理节点，点击"导入"或"添加"开始</div>'; return; }
  el.innerHTML = proxies.map(p => {
    const lat = proxyLatency[p.id];
    const latHtml = lat === undefined
      ? '<span class="lat-badge">--</span>'
      : lat === null
      ? '<span class="lat-badge testing">测速中</span>'
      : lat < 0
      ? '<span class="lat-badge err">超时</span>'
      : `<span class="lat-badge ok">${lat}ms</span>`;
    return `
    <div class="proxy-item ${p.enabled ? '' : 'disabled'}">
      <span class="type-badge proxy-type-badge">${esc(p.type)}</span>
      <div class="name">
        ${esc(p.alias || p.name)}
        ${p.alias ? '<small>原名: ' + esc(p.name) + '</small>' : ''}
      </div>
      <span class="meta">${esc(p.server)}:${p.port}</span>
      ${latHtml}
      <label class="toggle" style="flex-shrink:0"><input type="checkbox" ${p.enabled ? 'checked' : ''} onchange="toggleProxy('${p.id}', this.checked)"><span class="slider"></span></label>
      <button class="btn btn-sm" onclick="testProxyDelay('${p.id}')">测速</button>
      <button class="btn btn-sm" onclick="editProxy('${p.id}')">编辑</button>
      <button class="btn btn-danger btn-sm" onclick="deleteProxy('${p.id}')">删除</button>
    </div>
  `;
  }).join('');
}

function _fillDialerProxySelect(excludeId, selectedName) {
  const sel = document.getElementById('pDialerProxy');
  sel.innerHTML = '<option value="">直连</option>';
  proxies.filter(p => p.id !== excludeId).forEach(p => {
    const name = p.alias || p.name;
    const opt = document.createElement('option');
    opt.value = name;
    opt.textContent = name + ' (' + p.type + ')';
    if (name === selectedName) opt.selected = true;
    sel.appendChild(opt);
  });
}

function _extraWithoutDialerProxy(extra) {
  const cleaned = { ...(extra || {}) };
  delete cleaned['dialer-proxy'];
  return cleaned;
}

function clearDialerProxy() {
  document.getElementById('pDialerProxy').value = '';
}

function showAddProxyModal() {
  document.getElementById('proxyModalTitle').textContent = '添加代理';
  document.getElementById('proxyEditId').value = '';
  ['pName','pAlias','pServer','pPort','pExtra'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('pType').value = 'ss';
  document.getElementById('pEnabled').checked = true;
  _fillDialerProxySelect(null, '');
  showModal('proxyModal');
}

function editProxy(id) {
  const p = proxies.find(x => x.id === id);
  if (!p) return;
  document.getElementById('proxyModalTitle').textContent = '编辑代理';
  document.getElementById('proxyEditId').value = id;
  document.getElementById('pName').value = p.name;
  document.getElementById('pAlias').value = p.alias || '';
  document.getElementById('pType').value = p.type;
  document.getElementById('pServer').value = p.server;
  document.getElementById('pPort').value = p.port;
  document.getElementById('pEnabled').checked = p.enabled;
  _fillDialerProxySelect(p.id, (p.extra && p.extra['dialer-proxy']) || '');
  document.getElementById('pExtra').value = JSON.stringify(_extraWithoutDialerProxy(p.extra), null, 2);
  showModal('proxyModal');
}

async function saveProxy() {
  const id = document.getElementById('proxyEditId').value;
  let extra = {};
  try { extra = JSON.parse(document.getElementById('pExtra').value || '{}'); } catch (e) { toast('JSON 格式错误', 'error'); return; }
  const dialerProxy = document.getElementById('pDialerProxy').value.trim();
  if (dialerProxy) extra['dialer-proxy'] = dialerProxy;
  else delete extra['dialer-proxy'];
  const data = {
    name: document.getElementById('pName').value,
    alias: document.getElementById('pAlias').value,
    type: document.getElementById('pType').value,
    server: document.getElementById('pServer').value,
    port: parseInt(document.getElementById('pPort').value) || 0,
    enabled: document.getElementById('pEnabled').checked,
    extra: extra
  };
  try {
    if (id) await api('/api/proxies/' + id, { method: 'PUT', body: data });
    else await api('/api/proxies', { method: 'POST', body: { ...data, id: '' } });
    closeModal('proxyModal');
    toast(id ? '已更新' : '已添加');
    await loadAll();
  } catch (e) { toast('保存失败', 'error'); }
}

async function toggleProxy(id, enabled) {
  await api('/api/proxies/' + id, { method: 'PUT', body: { enabled } });
  await loadAll();
}

async function deleteProxy(id) {
  if (!confirm('确定删除此代理？')) return;
  await api('/api/proxies/' + id, { method: 'DELETE' });
  toast('已删除');
  await loadAll();
}

// ── Import ──
function showImportModal() {
  document.getElementById('importYamlContent').value = '';
  document.getElementById('importUri').value = '';
  document.getElementById('importText').value = '';
  document.getElementById('importType').value = 'yaml';
  showModal('importModal');
  toggleImportFields();
}

function toggleImportFields() {
  const type = document.getElementById('importType').value;
  ['importUriField','importTextField','importYamlField'].forEach(f => {
    document.getElementById(f).style.display = 'none';
  });
  const fieldMap = { uri: 'importUriField', text: 'importTextField', yaml: 'importYamlField', singbox: 'importYamlField' };
  if (fieldMap[type]) document.getElementById(fieldMap[type]).style.display = '';
}

async function doImport() {
  const type = document.getElementById('importType').value;
  let result;
  try {
    if (type === 'uri')
      result = await api('/api/import/uri', { method: 'POST', body: { uri: document.getElementById('importUri').value } });
    else if (type === 'text')
      result = await api('/api/import/text', { method: 'POST', body: { text: document.getElementById('importText').value } });
    else if (type === 'yaml')
      result = await api('/api/import/yaml', { method: 'POST', body: { yaml: document.getElementById('importYamlContent').value } });
    else if (type === 'singbox')
      result = await api('/api/import/sing-box', { method: 'POST', body: { json: document.getElementById('importYamlContent').value } });
    if (result.error) { toast(result.error, 'error'); return; }
    closeModal('importModal');
    toast('成功导入 ' + (result.imported || 0) + ' 个节点');
    await loadAll();
  } catch (e) { toast('导入失败: ' + e.message, 'error'); }
}

// ── Groups ──
function renderGroups() {
  const el = document.getElementById('groupList');
  if (!groups.length) { el.innerHTML = '<div style="text-align:center;color:var(--text2);padding:40px">暂无策略组</div>'; return; }
  const typeMap = { select: '手动选择', 'url-test': '自动选择', fallback: '自动回退', 'load-balance': '负载均衡', relay: '链式代理', relay: '链式代理' };
  el.innerHTML = groups.map(g => {
    const count = g.include_all ? proxies.length : g.proxies.length;
    return `
    <div class="proxy-item">
      <span class="type-badge">${esc(typeMap[g.type] || g.type)}</span>
      <div class="name">${esc(g.name)}<small>${count} 个节点${g.include_all ? ' (全部)' : ''}</small></div>
      <button class="btn btn-sm" onclick="editGroup('${g.id}')">编辑</button>
      <button class="btn btn-danger btn-sm" onclick="deleteGroup('${g.id}')">删除</button>
    </div>`;
  }).join('');
}

// Picker state
let pickerSelected = [];

function buildProxyPicker(selectedIds = []) {
  pickerSelected = [...selectedIds];
  renderPickerSelected();
  renderPickerOptions();
}

function renderPickerSelected() {
  const el = document.getElementById('gProxySelected');
  if (!pickerSelected.length) {
    el.innerHTML = '<span class="picker-empty">未选择任何节点</span>';
    return;
  }
  el.innerHTML = pickerSelected.map(id => {
    const isGroup = groups.some(g => g.name === id);
    const isSpecial = id === 'DIRECT' || id === 'REJECT';
    const label = isGroup ? '[组] ' + esc(id) : (isSpecial ? id : (() => { const p = proxies.find(x => x.id === id); return p ? esc(p.alias || p.name) : esc(id); })());
    const cls = isGroup ? 'picker-tag selected is-group' : 'picker-tag selected';
    return `<span class="${cls}" data-id="${esc(id)}" onclick="pickerRemove(this.dataset.id)">${label}</span>`;
  }).join('');
}

function renderPickerOptions() {
  const el = document.getElementById('gProxyOptions');
  let items = [
    { id: 'DIRECT', label: 'DIRECT', isGroup: false },
    { id: 'REJECT', label: 'REJECT', isGroup: false },
    ...proxies.map(p => ({ id: p.id, label: esc(p.alias || p.name), isGroup: false })),
    ...groups.map(g => ({ id: g.name, label: '[组] ' + esc(g.name), isGroup: true }))
  ].filter(item => !pickerSelected.includes(item.id));
  if (!items.length) { el.innerHTML = '<span class="picker-empty">全部已选择</span>'; return; }
  el.innerHTML = items.map(item =>
    `<span class="picker-tag option${item.isGroup ? ' is-group' : ''}" data-id="${item.id}" onclick="pickerAdd(this.dataset.id)">${item.label}</span>`
  ).join('');
}

function pickerAdd(id) {
  if (!pickerSelected.includes(id)) pickerSelected.push(id);
  renderPickerSelected();
  renderPickerOptions();
}

function pickerRemove(id) {
  pickerSelected = pickerSelected.filter(x => x !== id);
  renderPickerSelected();
  renderPickerOptions();
}

function showAddGroupModal() {
  document.getElementById('groupModalTitle').textContent = '添加策略组';
  document.getElementById('groupEditId').value = '';
  document.getElementById('gName').value = '';
  document.getElementById('gType').value = 'select';
  document.getElementById('gIncludeAll').checked = false;
  document.getElementById('gUrl').value = 'https://www.gstatic.com/generate_204';
  document.getElementById('gInterval').value = '300';
  buildProxyPicker([]);
  showModal('groupModal');
}

function editGroup(id) {
  const g = groups.find(x => x.id === id);
  if (!g) return;
  document.getElementById('groupModalTitle').textContent = '编辑策略组';
  document.getElementById('groupEditId').value = id;
  document.getElementById('gName').value = g.name;
  document.getElementById('gType').value = g.type;
  document.getElementById('gIncludeAll').checked = g.include_all;
  document.getElementById('gUrl').value = g.url;
  document.getElementById('gInterval').value = g.interval;
  buildProxyPicker(g.proxies);
  showModal('groupModal');
}

async function saveGroup() {
  const id = document.getElementById('groupEditId').value;
  const data = {
    name: document.getElementById('gName').value,
    type: document.getElementById('gType').value,
    proxies: pickerSelected,
    include_all: document.getElementById('gIncludeAll').checked,
    url: document.getElementById('gUrl').value,
    interval: parseInt(document.getElementById('gInterval').value) || 300,
    timeout: 5000
  };
  try {
    if (id) await api('/api/groups/' + id, { method: 'PUT', body: data });
    else await api('/api/groups', { method: 'POST', body: { ...data, id: '' } });
    closeModal('groupModal');
    toast(id ? '已更新' : '已添加');
    await loadAll();
  } catch (e) { toast('保存失败', 'error'); }
}

async function deleteGroup(id) {
  if (!confirm('确定删除此策略组？')) return;
  await api('/api/groups/' + id, { method: 'DELETE' });
  toast('已删除');
  await loadAll();
}

// ── Rules ──────────────────────────────────────────────────────────────────
function getRuleById(id) { return rules.find(r => r.id === id) || null; }

function resolveRuleTarget(target) {
  if (!target || target === 'DIRECT' || target === 'REJECT') return target || '';
  const g = groups.find(x => x.name === target); if (g) return target;
  const p = proxies.find(x => x.id === target); if (p) return p.alias || p.name;
  return target;
}

function renderRules() {
  const el = document.getElementById('ruleList');
  if (!rules.length) {
    el.innerHTML = '<div style="text-align:center;color:var(--text2);padding:40px">暂无路由规则</div>';
    return;
  }
  el.innerHTML = rules.map((r, i) => renderMainRuleItem(r, i, rules.length)).join('');
}

function renderMainRuleItem(r, idx, total) {
  const isSubRef = r.type === 'SUB-RULE';
  let nameHtml;
  if (isSubRef) {
    nameHtml = `<b>条件: (${esc(r.payload)})</b><small>→ 集合: ${esc(r.target)}</small>`;
  } else if (r.type === 'MATCH') {
    nameHtml = '<i>匹配所有</i>';
  } else {
    nameHtml = esc(r.payload);
  }
  const targetHtml = isSubRef
    ? `<span class="meta" style="color:#5b21b6">集合: ${esc(r.target)}</span>`
    : `<span class="meta" style="color:var(--accent)">→ ${esc(resolveRuleTarget(r.target))}</span>`;
  return `<div class="proxy-item${r.enabled ? '' : ' disabled'}${isSubRef ? ' is-container' : ''}">
    <span class="type-badge${isSubRef ? ' badge-sub' : ''}">${esc(r.type)}</span>
    <div class="name">${nameHtml}</div>
    ${targetHtml}
    <label class="toggle" style="flex-shrink:0"><input type="checkbox" ${r.enabled ? 'checked' : ''} onchange="toggleRule('${r.id}',this.checked)"><span class="slider"></span></label>
    <button class="btn btn-sm" onclick="moveMainRule('${r.id}',-1)" ${idx===0?'disabled':''}>▲</button>
    <button class="btn btn-sm" onclick="moveMainRule('${r.id}',1)" ${idx===total-1?'disabled':''}>▼</button>
    <button class="btn btn-sm" onclick="editRule('${r.id}')">编辑</button>
    <button class="btn btn-danger btn-sm" onclick="deleteRule('${r.id}')">删除</button>
  </div>`;
}

function updateRuleTargets() {
  const sel = document.getElementById('rTarget');
  const current = sel.value;
  sel.innerHTML = '<option value="DIRECT">DIRECT</option><option value="REJECT">REJECT</option>';
  groups.forEach(g => sel.innerHTML += '<option value="' + esc(g.name) + '">[策略组] ' + esc(g.name) + '</option>');
  proxies.filter(p => p.enabled).forEach(p => {
    const label = p.alias || p.name;
    sel.innerHTML += '<option value="' + esc(p.id) + '">[节点] ' + esc(label) + '</option>';
  });
  sel.value = current || 'DIRECT';
}

function updateSubRuleSetOptions() {
  const sel = document.getElementById('rSubRuleSetName');
  const current = sel.value;
  const names = Object.keys(subRuleSets);
  sel.innerHTML = names.length
    ? names.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join('')
    : '<option value="">（暂无子规则集）</option>';
  if (names.includes(current)) sel.value = current;
}

function updateRuleSetOptions() {
  const sel = document.getElementById('rRuleSetName');
  const current = sel.value;
  const names = Object.keys(ruleProviders);
  sel.innerHTML = names.length
    ? names.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join('')
    : '<option value="">（暂无规则集合）</option>';
  if (names.includes(current)) sel.value = current;
}

function isCidrRuleType(type) {
  return type === 'IP-CIDR' || type === 'IP-CIDR6' || type === 'SRC-IP-CIDR';
}

function maskMaxByType(type) {
  return type === 'IP-CIDR6' ? 128 : 32;
}

function defaultMaskByType(type) {
  if (type === 'IP-CIDR6') return 128;
  if (type === 'SRC-IP-CIDR') return 32;
  return 24;
}

function setMaskInputValue(inputId, maxMask, selectedMask) {
  const input = document.getElementById(inputId);
  const val = Number.isInteger(selectedMask) ? selectedMask : maxMask;
  input.value = String(Math.min(maxMask, Math.max(0, val)));
}

function parseCidrPayload(raw, type) {
  const input = (raw || '').trim();
  if (!input) {
    return { ip: '', mask: defaultMaskByType(type) };
  }
  const slash = input.lastIndexOf('/');
  if (slash <= 0 || slash === input.length - 1) {
    return { ip: input, mask: defaultMaskByType(type) };
  }
  const ip = input.slice(0, slash).trim();
  const parsedMask = parseInt(input.slice(slash + 1).trim(), 10);
  if (!Number.isFinite(parsedMask)) {
    return { ip: input, mask: defaultMaskByType(type) };
  }
  const max = maskMaxByType(type);
  return { ip, mask: Math.min(max, Math.max(0, parsedMask)) };
}

function composeCidrPayload(ip, mask, type) {
  const rawIp = (ip || '').trim();
  if (!rawIp) return '';
  const max = maskMaxByType(type);
  const parsed = parseInt(mask, 10);
  const normalized = Number.isFinite(parsed) ? Math.min(max, Math.max(0, parsed)) : defaultMaskByType(type);
  return rawIp + '/' + normalized;
}

function syncMainCidrFields() {
  const type = document.getElementById('rType').value;
  const parsed = parseCidrPayload(document.getElementById('rPayload').value, type);
  document.getElementById('rPayloadIp').value = parsed.ip;
  document.getElementById('rPayloadMask').max = String(maskMaxByType(type));
  setMaskInputValue('rPayloadMask', maskMaxByType(type), parsed.mask);
  document.getElementById('rPayloadIpLabel').textContent = type === 'IP-CIDR6' ? 'IPv6 地址' : 'IP 地址';
}

function syncCondCidrFields() {
  const condType = document.getElementById('rCondType').value;
  const parsed = parseCidrPayload(document.getElementById('rCondPayload').value, condType);
  document.getElementById('rCondPayloadIp').value = parsed.ip;
  document.getElementById('rCondPayloadMask').max = String(maskMaxByType(condType));
  setMaskInputValue('rCondPayloadMask', maskMaxByType(condType), parsed.mask);
  document.getElementById('rCondPayloadIpLabel').textContent = condType === 'IP-CIDR6' ? 'IPv6 地址' : 'IP 地址';
}

function onRuleTypeChange() {
  const type = document.getElementById('rType').value;
  const isSubRule = type === 'SUB-RULE';
  const isMatch = type === 'MATCH';
  const isRuleSet = type === 'RULE-SET';
  const useCidrMask = isCidrRuleType(type) && !isSubRule && !isMatch && !isRuleSet;
  document.getElementById('rPayloadField').style.display = (isMatch || isSubRule || isRuleSet || useCidrMask) ? 'none' : '';
  document.getElementById('rPayloadMaskRow').style.display = useCidrMask ? 'grid' : 'none';
  document.getElementById('rTargetField').style.display = isSubRule ? 'none' : '';
  document.getElementById('rSubRuleCondRow').style.display = isSubRule ? '' : 'none';
  document.getElementById('rSubRuleSetRow').style.display = isSubRule ? '' : 'none';
  document.getElementById('rRuleSetRow').style.display = isRuleSet ? '' : 'none';

  const hints = {
    'DOMAIN':                 { label: '域名',              ph: 'google.com' },
    'DOMAIN-SUFFIX':          { label: '域名后缀',          ph: 'google.com（匹配所有子域名）' },
    'DOMAIN-KEYWORD':         { label: '域名关键词',        ph: 'google' },
    'DOMAIN-WILDCARD':        { label: '域名通配符',        ph: '*.google.com' },
    'DOMAIN-REGEX':           { label: '域名正则',          ph: '^.*\\.google\\.com$' },
    'GEOSITE':                { label: 'GeoSite 类别',      ph: 'CN 、 google 、 youtube' },
    'GEOIP':                  { label: 'GeoIP 国家代码',    ph: 'CN 、 US 、 JP' },
    'IP-CIDR':                { label: 'IP 段 (CIDR)',      ph: '192.168.0.0/16' },
    'IP-CIDR6':               { label: 'IPv6 段 (CIDR)',    ph: '2001:db8::/32' },
    'IP-SUFFIX':              { label: 'IP 后缀范围',       ph: '8.8.8.8/24' },
    'IP-ASN':                 { label: 'IP 所属 ASN',       ph: '13335' },
    'SRC-IP-CIDR':            { label: '来源 IP 段',        ph: '192.168.1.0/24' },
    'SRC-IP-SUFFIX':          { label: '来源 IP 后缀',      ph: '192.168.1.201/8' },
    'SRC-IP-ASN':             { label: '来源 IP ASN',       ph: '9808' },
    'SRC-GEOIP':              { label: '来源 GeoIP 国家',   ph: 'CN 、 US' },
    'DST-PORT':               { label: '目标端口',          ph: '80 或 8080-9090' },
    'SRC-PORT':               { label: '来源端口',          ph: '1024-65535' },
    'IN-PORT':                { label: '入站端口',          ph: '7890' },
    'IN-TYPE':                { label: '入站类型',          ph: 'SOCKS/HTTP' },
    'IN-USER':                { label: '入站用户名',        ph: 'mihomo' },
    'IN-NAME':                { label: '入站名称',          ph: 'ss' },
    'PROCESS-NAME':           { label: '进程名称',          ph: 'chrome.exe 或 curl' },
    'PROCESS-NAME-WILDCARD':  { label: '进程名通配符',      ph: '*telegram*' },
    'PROCESS-NAME-REGEX':     { label: '进程名正则',        ph: 'curl$' },
    'PROCESS-PATH':           { label: '进程完整路径',      ph: '/usr/bin/wget' },
    'PROCESS-PATH-WILDCARD':  { label: '进程路径通配符',    ph: '/usr/*/wget' },
    'PROCESS-PATH-REGEX':     { label: '进程路径正则',      ph: '.*bin/wget' },
    'UID':                    { label: 'Linux UID',          ph: '1001' },
    'NETWORK':                { label: '网络协议',          ph: 'tcp 或 udp' },
    'DSCP':                   { label: 'DSCP 标记',         ph: '4' },
    'AND':                    { label: '逻辑条件 (AND)',     ph: '((DOMAIN,baidu.com),(NETWORK,UDP))' },
    'OR':                     { label: '逻辑条件 (OR)',      ph: '((NETWORK,UDP),(DOMAIN,baidu.com))' },
    'NOT':                    { label: '逻辑条件 (NOT)',     ph: '((DOMAIN,baidu.com))' },
  };
  const h = hints[type] || { label: '匹配内容', ph: '' };
  document.getElementById('rPayloadLabel').textContent = h.label;
  document.getElementById('rPayload').placeholder = h.ph;

  if (useCidrMask) {
    syncMainCidrFields();
  }

  onCondTypeChange();
}
document.getElementById('rType').addEventListener('change', onRuleTypeChange);

function onCondTypeChange() {
  const condType = document.getElementById('rCondType').value;
  const isNetwork = condType === 'NETWORK';
  const useCidrMask = isCidrRuleType(condType);
  document.getElementById('rCondPayload').style.display = (isNetwork || useCidrMask) ? 'none' : '';
  document.getElementById('rCondPayloadMaskRow').style.display = (!isNetwork && useCidrMask) ? 'grid' : 'none';
  document.getElementById('rCondPayloadSel').style.display = isNetwork ? '' : 'none';
  const condHints = {
    'DOMAIN': 'google.com', 'DOMAIN-SUFFIX': 'google.com', 'DOMAIN-KEYWORD': 'google',
    'DOMAIN-REGEX': '^.*\\.google\\..*$', 'GEOIP': 'CN', 'GEOSITE': 'CN',
    'IP-CIDR': '192.168.0.0/16', 'IP-CIDR6': '2001:db8::/32',
    'PROCESS-NAME': 'chrome.exe', 'DST-PORT': '80 或 8080-9090', 'SRC-PORT': '1024-65535',
  };
  document.getElementById('rCondPayload').placeholder = condHints[condType] || '';
  if (!isNetwork && useCidrMask) {
    syncCondCidrFields();
  }
}
document.getElementById('rCondType').addEventListener('change', onCondTypeChange);

function showAddRuleModal() {
  ruleModalContext = null;
  document.getElementById('ruleModalTitle').textContent = '添加规则';
  document.getElementById('ruleEditId').value = '';
  document.getElementById('rType').value = 'DOMAIN-SUFFIX';
  document.getElementById('rPayload').value = '';
  document.getElementById('rPayloadIp').value = '';
  document.getElementById('rPayloadMask').value = '';
  document.getElementById('rCondPayload').value = '';
  document.getElementById('rCondPayloadIp').value = '';
  document.getElementById('rCondPayloadMask').value = '';
  document.getElementById('rCondPayloadSel').value = 'tcp';
  document.getElementById('rCondType').value = 'NETWORK';
  document.getElementById('rNoResolve').checked = false;
  document.querySelector('input[name="rPosition"][value="append"]').checked = true;
  document.getElementById('rPositionField').style.display = '';
  document.getElementById('rParentInfo').style.display = 'none';
  updateRuleTargets();
  updateSubRuleSetOptions();
  updateRuleSetOptions();
  document.getElementById('rTarget').value = 'DIRECT';
  onRuleTypeChange();
  showModal('ruleModal');
}

function editRule(id) {
  const r = rules.find(x => x.id === id);
  if (!r) return;
  ruleModalContext = null;
  document.getElementById('ruleModalTitle').textContent = '编辑规则';
  document.getElementById('ruleEditId').value = id;
  document.getElementById('rType').value = r.type;
  document.getElementById('rNoResolve').checked = r.no_resolve;
  document.getElementById('rPositionField').style.display = 'none';
  document.getElementById('rParentInfo').style.display = 'none';
  updateRuleTargets();
  updateSubRuleSetOptions();
  updateRuleSetOptions();
  if (r.type === 'SUB-RULE') {
    const parts = r.payload.split(',');
    const condType = parts[0] || 'NETWORK';
    document.getElementById('rCondType').value = condType;
    const condVal = parts.slice(1).join(',');
    if (condType === 'NETWORK') {
      document.getElementById('rCondPayloadSel').value = condVal || 'tcp';
      document.getElementById('rCondPayload').value = '';
    } else {
      document.getElementById('rCondPayload').value = condVal;
      if (isCidrRuleType(condType)) {
        syncCondCidrFields();
      }
    }
    document.getElementById('rSubRuleSetName').value = r.target;
  } else if (r.type === 'RULE-SET') {
    document.getElementById('rPayload').value = '';
    document.getElementById('rTarget').value = r.target;
    document.getElementById('rRuleSetName').value = r.payload;
  } else {
    document.getElementById('rPayload').value = r.payload;
    if (isCidrRuleType(r.type)) {
      syncMainCidrFields();
    }
    document.getElementById('rTarget').value = r.target;
  }
  onRuleTypeChange();
  showModal('ruleModal');
}

async function saveRule() {
  const editId = document.getElementById('ruleEditId').value;
  const type = document.getElementById('rType').value;
  let payload, target;
  if (type === 'RULE-SET') {
    payload = document.getElementById('rRuleSetName').value;
    target = document.getElementById('rTarget').value;
    if (!payload) { toast('请选择规则集合', 'error'); return; }
  } else if (type === 'SUB-RULE') {
    const condType = document.getElementById('rCondType').value;
    let condPayload;
    if (condType === 'NETWORK') {
      condPayload = document.getElementById('rCondPayloadSel').value;
    } else if (isCidrRuleType(condType)) {
      condPayload = composeCidrPayload(
        document.getElementById('rCondPayloadIp').value,
        document.getElementById('rCondPayloadMask').value,
        condType
      );
      if (!condPayload) { toast('请填写条件 IP 地址', 'error'); return; }
    } else {
      condPayload = document.getElementById('rCondPayload').value.trim();
    }
    payload = condType + (condPayload ? ',' + condPayload : '');
    target = document.getElementById('rSubRuleSetName').value;
    if (!target) { toast('请选择目标子规则集', 'error'); return; }
  } else {
    if (isCidrRuleType(type)) {
      payload = composeCidrPayload(
        document.getElementById('rPayloadIp').value,
        document.getElementById('rPayloadMask').value,
        type
      );
      if (!payload) { toast('请填写 IP 地址', 'error'); return; }
    } else {
      payload = document.getElementById('rPayload').value;
    }
    target = document.getElementById('rTarget').value;
  }
  const no_resolve = document.getElementById('rNoResolve').checked;

  // Case 1: adding new entry to a sub-rule set
  if (ruleModalContext && typeof ruleModalContext === 'string') {
    const setName = ruleModalContext;
    const entries = Array.isArray(subRuleSets[setName]) ? [...subRuleSets[setName]] : [];
    entries.push({ type, payload, target, no_resolve, enabled: true });
    try {
      await api('/api/sub-rules/' + encodeURIComponent(setName), { method: 'PUT', body: entries });
      closeModal('ruleModal');
      toast('已添加');
      await loadAll();
    } catch (e) { toast('保存失败', 'error'); }
    return;
  }

  // Case 2: editing existing entry in a sub-rule set
  if (ruleModalContext && typeof ruleModalContext === 'object') {
    const { setName, idx } = ruleModalContext;
    const entries = Array.isArray(subRuleSets[setName]) ? [...subRuleSets[setName]] : [];
    if (idx >= 0 && idx < entries.length) {
      entries[idx] = { ...entries[idx], type, payload, target, no_resolve };
    }
    try {
      await api('/api/sub-rules/' + encodeURIComponent(setName), { method: 'PUT', body: entries });
      closeModal('ruleModal');
      toast('已更新');
      await loadAll();
    } catch (e) { toast('保存失败', 'error'); }
    return;
  }

  // Case 3: main rule (add or edit)
  const prepend = !editId && document.querySelector('input[name="rPosition"]:checked')?.value === 'prepend';
  const data = { type, payload, target, no_resolve, enabled: true, prepend };
  try {
    if (editId) await api('/api/rules/' + editId, { method: 'PUT', body: data });
    else await api('/api/rules', { method: 'POST', body: { ...data, id: '' } });
    closeModal('ruleModal');
    toast(editId ? '已更新' : '已添加');
    await loadAll();
  } catch (e) { toast('保存失败', 'error'); }
}

async function toggleRule(id, enabled) {
  await api('/api/rules/' + id, { method: 'PUT', body: { enabled } });
  await loadAll();
}

async function moveMainRule(id, dir) {
  const idx = rules.findIndex(r => r.id === id);
  if (idx < 0) return;
  const newIdx = idx + dir;
  if (newIdx < 0 || newIdx >= rules.length) return;
  const ordered = rules.map(r => r.id);
  [ordered[idx], ordered[newIdx]] = [ordered[newIdx], ordered[idx]];
  await api('/api/rules/reorder', { method: 'POST', body: ordered });
  await loadAll();
}

async function deleteRule(id) {
  if (!confirm('确定删除此规则？')) return;
  await api('/api/rules/' + id, { method: 'DELETE' });
  toast('已删除');
  await loadAll();
}
// ── Settings ──
function loadSettingsUI() {
  document.getElementById('sMixedPort').value = settings.mixed_port;
  document.getElementById('sMode').value = settings.mode;
  document.getElementById('sLogLevel').value = settings.log_level;
  document.getElementById('sAllowLan').checked = settings.allow_lan;
  document.getElementById('sApiPort').value = settings.mihomo_api_port;
  document.getElementById('sSecret').value = settings.mihomo_api_secret || '';
  document.getElementById('sSingBoxApiPort').value = settings.sing_box_api_port || 9091;
  document.getElementById('sMihomoBin').value = settings.mihomo_bin;
  document.getElementById('sSingBoxBin').value = settings.sing_box_bin || '/usr/bin/sing-box';
  document.getElementById('sDelayTestUrl').value = settings.delay_test_url || '';
}

async function saveSettings() {
  const prev = { ...settings };
  const s = {
    ...settings,
    mixed_port: parseInt(document.getElementById('sMixedPort').value),
    mode: document.getElementById('sMode').value,
    log_level: document.getElementById('sLogLevel').value,
    allow_lan: document.getElementById('sAllowLan').checked,
    mihomo_api_port: parseInt(document.getElementById('sApiPort').value),
    mihomo_api_secret: document.getElementById('sSecret').value,
    sing_box_api_port: parseInt(document.getElementById('sSingBoxApiPort').value) || 9091,
    mihomo_bin: document.getElementById('sMihomoBin').value,
    sing_box_bin: document.getElementById('sSingBoxBin').value || '/usr/bin/sing-box',
    delay_test_url: document.getElementById('sDelayTestUrl').value || 'http://www.gstatic.com/generate_204',
  };
  try {
    await api('/api/settings', { method: 'PUT', body: s });
    const mihomoFields = ['mixed_port', 'mode', 'log_level', 'allow_lan', 'mihomo_api_port', 'mihomo_api_secret', 'mihomo_bin'];
    if (mihomoFields.some(field => prev[field] !== s[field])) markMihomoConfigDirty();
    if ((prev.sing_box_bin || '/usr/bin/sing-box') !== s.sing_box_bin || (prev.sing_box_api_port || 9091) !== s.sing_box_api_port) markSingBoxConfigDirty();
    toast('设置已保存');
    await loadAll();
  } catch (e) { toast('保存失败', 'error'); }
}

// (esc/escHtml/formatBytes defined above)

async function reloadConfig() {
  const applyMihomo = isMihomoConfigDirty();
  const applySingBox = isSingBoxConfigDirty();
  if (!applyMihomo && !applySingBox) {
    toast('没有待应用的配置');
    return;
  }
  try {
    await api('/api/apply', { method: 'POST', body: { mihomo: applyMihomo, sing_box: applySingBox } });
    const parts = [];
    if (applyMihomo) parts.push('主配置已热重载');
    if (applySingBox) parts.push('端口绑定已应用');
    toast(parts.join('，'));
    setTimeout(refreshStatus, 500);
  } catch (e) { toast('应用配置失败: ' + e.message, 'error'); }
}

// ── Sub-Rule Manager Side Panel ──
let _renamingSetName = null;

function showSubRulePanelRoot() {
  srCurrentSet = null;
  renderSubRulePanel();
  document.getElementById('subRulePanel').classList.add('show');
}

function closeSidePanel() {
  document.getElementById('subRulePanel').classList.remove('show');
  srCurrentSet = null;
}

function renderSubRulePanel() {
  const isRoot = srCurrentSet === null;
  document.getElementById('srPanelTitle').textContent = isRoot ? '子规则集管理' : ('集合：' + srCurrentSet);

  const bc = document.getElementById('srBreadcrumb');
  bc.innerHTML = isRoot
    ? '<span class="breadcrumb-item active">全部集合</span>'
    : `<span class="breadcrumb-item" onclick="srGoRoot()">全部集合</span><span class="breadcrumb-sep"> › </span><span class="breadcrumb-item active">${esc(srCurrentSet)}</span>`;

  const actEl = document.getElementById('srPanelActions');
  actEl.innerHTML = isRoot
    ? '<button class="btn btn-primary btn-sm" onclick="showCreateSetModal()">+ 新建子规则集</button>'
    : '<button class="btn btn-primary btn-sm" onclick="showAddEntryToSet()">+ 添加规则</button>';

  const content = document.getElementById('srTreeContent');

  if (isRoot) {
    _srSetNames = Object.keys(subRuleSets);
    if (!_srSetNames.length) {
      content.innerHTML = `<div style="text-align:center;color:var(--text2);padding:40px">
        <div style="font-size:32px;margin-bottom:12px">⊞</div>
        <div style="font-size:15px;font-weight:600;margin-bottom:8px">暂无子规则集</div>
        <div style="font-size:13px">点击上方"新建子规则集"创建第一个命名集合</div>
      </div>`;
      return;
    }
    content.innerHTML = _srSetNames.map((name, i) => {
      const entries = subRuleSets[name] || [];
      return `<div class="sr-container-card">
        <div class="name">${esc(name)}<small>${entries.length} 条规则</small></div>
        <button class="btn btn-primary btn-sm" onclick="srDrillInto(${i})">进入 ▶</button>
        <button class="btn btn-sm" onclick="showRenameSetModal(${i})">重命名</button>
        <button class="btn btn-danger btn-sm" onclick="deleteSet(${i})">删除</button>
      </div>`;
    }).join('');
    return;
  }

  const entries = subRuleSets[srCurrentSet] || [];
  if (!entries.length) {
    content.innerHTML = '<div style="text-align:center;color:var(--text2);padding:40px;font-style:italic">此集合暂无规则，点击上方"添加规则"开始</div>';
    return;
  }
  content.innerHTML = entries.map((e, idx) => renderSrEntry(e, idx, entries.length)).join('');
}

function renderSrEntry(e, idx, total) {
  const isSubRef = e.type === 'SUB-RULE';
  let nameHtml;
  if (isSubRef) {
    nameHtml = `<b>条件: (${esc(e.payload)})</b><small>→ 集合: ${esc(e.target)}</small>`;
  } else if (e.type === 'MATCH') {
    nameHtml = '<i>匹配所有</i>';
  } else {
    nameHtml = esc(e.payload);
  }
  const targetHtml = isSubRef
    ? `<span class="meta" style="color:#5b21b6">集合: ${esc(e.target)}</span>`
    : `<span class="meta" style="color:var(--accent)">→ ${esc(resolveRuleTarget(e.target))}</span>`;
  return `<div class="proxy-item${e.enabled ? '' : ' disabled'}${isSubRef ? ' is-container' : ''}">
    <span class="type-badge${isSubRef ? ' badge-sub' : ''}">${esc(e.type)}</span>
    <div class="name">${nameHtml}</div>
    ${targetHtml}
    <label class="toggle" style="flex-shrink:0">
      <input type="checkbox" ${e.enabled ? 'checked' : ''} onchange="toggleSrEntry(${idx},this.checked)">
      <span class="slider"></span>
    </label>
    <button class="btn btn-sm" onclick="moveSrEntry(${idx},-1)" ${idx===0?'disabled':''}>▲</button>
    <button class="btn btn-sm" onclick="moveSrEntry(${idx},1)" ${idx===total-1?'disabled':''}>▼</button>
    <button class="btn btn-sm" onclick="editSrEntry(${idx})">编辑</button>
    <button class="btn btn-danger btn-sm" onclick="deleteSrEntry(${idx})">删除</button>
  </div>`;
}

function srGoRoot() { srCurrentSet = null; renderSubRulePanel(); }

function srDrillInto(i) { srCurrentSet = _srSetNames[i]; renderSubRulePanel(); }

function showCreateSetModal() {
  _renamingSetName = null;
  document.getElementById('srSetModalTitle').textContent = '新建子规则集';
  document.getElementById('srSetName').value = '';
  showModal('srSetModal');
}

function showRenameSetModal(i) {
  _renamingSetName = _srSetNames[i];
  document.getElementById('srSetModalTitle').textContent = '重命名子规则集';
  document.getElementById('srSetName').value = _renamingSetName;
  showModal('srSetModal');
}

async function saveSrSetName() {
  const name = document.getElementById('srSetName').value.trim();
  if (!name) { toast('请填写集合名称', 'error'); return; }
  if (name.includes('/')) { toast('集合名称不能包含 "/"', 'error'); return; }
  try {
    if (_renamingSetName) {
      await api('/api/sub-rules/' + encodeURIComponent(_renamingSetName) + '/rename', {
        method: 'POST', body: { new_name: name }
      });
      if (srCurrentSet === _renamingSetName) srCurrentSet = name;
    } else {
      await api('/api/sub-rules/' + encodeURIComponent(name), { method: 'PUT', body: [] });
      srCurrentSet = name;
    }
    closeModal('srSetModal');
    toast(_renamingSetName ? '已重命名' : '已创建');
    await loadAll();
    if (document.getElementById('subRulePanel').classList.contains('show')) renderSubRulePanel();
  } catch (e) { toast('操作失败', 'error'); }
}

async function deleteSet(i) {
  const name = _srSetNames[i];
  if (!confirm('确定删除子规则集 "' + name + '"？此操作不可撤销。')) return;
  await api('/api/sub-rules/' + encodeURIComponent(name), { method: 'DELETE' });
  if (srCurrentSet === name) srCurrentSet = null;
  toast('已删除');
  await loadAll();
}

function showAddEntryToSet() {
  ruleModalContext = srCurrentSet;
  document.getElementById('ruleModalTitle').textContent = '添加规则到集合';
  document.getElementById('ruleEditId').value = '';
  document.getElementById('rType').value = 'DOMAIN-SUFFIX';
  document.getElementById('rPayload').value = '';
  document.getElementById('rPayloadIp').value = '';
  document.getElementById('rPayloadMask').value = '';
  document.getElementById('rCondPayload').value = '';
  document.getElementById('rCondPayloadIp').value = '';
  document.getElementById('rCondPayloadMask').value = '';
  document.getElementById('rCondPayloadSel').value = 'tcp';
  document.getElementById('rCondType').value = 'NETWORK';
  document.getElementById('rNoResolve').checked = false;
  document.getElementById('rPositionField').style.display = 'none';
  document.getElementById('rParentInfo').style.display = '';
  document.getElementById('rParentName').textContent = srCurrentSet;
  updateRuleTargets();
  updateSubRuleSetOptions();
  updateRuleSetOptions();
  document.getElementById('rTarget').value = 'DIRECT';
  onRuleTypeChange();
  showModal('ruleModal');
}

function editSrEntry(idx) {
  const entries = subRuleSets[srCurrentSet] || [];
  const e = entries[idx];
  if (!e) return;
  ruleModalContext = { setName: srCurrentSet, idx };
  document.getElementById('ruleModalTitle').textContent = '编辑规则';
  document.getElementById('ruleEditId').value = '';
  document.getElementById('rType').value = e.type;
  document.getElementById('rNoResolve').checked = e.no_resolve;
  document.getElementById('rPositionField').style.display = 'none';
  document.getElementById('rParentInfo').style.display = '';
  document.getElementById('rParentName').textContent = srCurrentSet;
  updateRuleTargets();
  updateSubRuleSetOptions();
  updateRuleSetOptions();
  if (e.type === 'SUB-RULE') {
    const parts = e.payload.split(',');
    const condType = parts[0] || 'NETWORK';
    document.getElementById('rCondType').value = condType;
    const condVal = parts.slice(1).join(',');
    if (condType === 'NETWORK') {
      document.getElementById('rCondPayloadSel').value = condVal || 'tcp';
      document.getElementById('rCondPayload').value = '';
    } else {
      document.getElementById('rCondPayload').value = condVal;
      if (isCidrRuleType(condType)) {
        syncCondCidrFields();
      }
    }
    document.getElementById('rSubRuleSetName').value = e.target;
    document.getElementById('rPayload').value = '';
  } else if (e.type === 'RULE-SET') {
    document.getElementById('rPayload').value = '';
    document.getElementById('rTarget').value = e.target;
    document.getElementById('rRuleSetName').value = e.payload;
  } else {
    document.getElementById('rPayload').value = e.payload;
    if (isCidrRuleType(e.type)) {
      syncMainCidrFields();
    }
    document.getElementById('rTarget').value = e.target;
  }
  onRuleTypeChange();
  showModal('ruleModal');
}

async function toggleSrEntry(idx, enabled) {
  const entries = [...(subRuleSets[srCurrentSet] || [])];
  if (idx < 0 || idx >= entries.length) return;
  entries[idx] = { ...entries[idx], enabled };
  await api('/api/sub-rules/' + encodeURIComponent(srCurrentSet), { method: 'PUT', body: entries });
  await loadAll();
}

async function moveSrEntry(idx, dir) {
  const entries = [...(subRuleSets[srCurrentSet] || [])];
  const newIdx = idx + dir;
  if (newIdx < 0 || newIdx >= entries.length) return;
  [entries[idx], entries[newIdx]] = [entries[newIdx], entries[idx]];
  await api('/api/sub-rules/' + encodeURIComponent(srCurrentSet), { method: 'PUT', body: entries });
  await loadAll();
}

async function deleteSrEntry(idx) {
  if (!confirm('确定删除此规则？')) return;
  const entries = [...(subRuleSets[srCurrentSet] || [])];
  entries.splice(idx, 1);
  await api('/api/sub-rules/' + encodeURIComponent(srCurrentSet), { method: 'PUT', body: entries });
  toast('已删除');
  await loadAll();
}

// ── Rule Provider Panel ──
let _editingRpName = null;

function showRuleProviderPanel() {
  renderRuleProviderPanel();
  document.getElementById('ruleProviderPanel').classList.add('show');
}

function closeRuleProviderPanel() {
  document.getElementById('ruleProviderPanel').classList.remove('show');
}

function renderRuleProviderPanel() {
  const content = document.getElementById('rpContent');
  const names = Object.keys(ruleProviders);
  if (!names.length) {
    content.innerHTML = `<div style="text-align:center;color:var(--text2);padding:40px">
      <div style="font-size:32px;margin-bottom:12px">⊟</div>
      <div style="font-size:15px;font-weight:600;margin-bottom:8px">暂无规则集合</div>
      <div style="font-size:13px">点击上方"新建规则集合"添加 rule-provider</div>
    </div>`;
    return;
  }
  content.innerHTML = names.map(name => {
    const rp = ruleProviders[name];
    const detail = rp.type === 'http' ? (rp.url || '（无 URL）') : (rp.path || '（无路径）');
    return `<div class="sr-container-card">
      <div class="name">${esc(name)}<small>${esc(rp.behavior)} / ${esc(rp.type)}</small></div>
      <div style="font-size:12px;color:var(--text2);max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1">${esc(detail)}</div>
      <button class="btn btn-sm" onclick="editRuleProvider('${esc(name)}')">编辑</button>
      <button class="btn btn-danger btn-sm" onclick="deleteRuleProvider('${esc(name)}')">删除</button>
    </div>`;
  }).join('');
}

function showAddRuleProviderModal() {
  _editingRpName = null;
  document.getElementById('rpModalTitle').textContent = '新建规则集合';
  document.getElementById('rpName').value = '';
  document.getElementById('rpName').disabled = false;
  document.getElementById('rpBehavior').value = 'domain';
  document.getElementById('rpType').value = 'http';
  document.getElementById('rpUrl').value = '';
  document.getElementById('rpPath').value = '';
  document.getElementById('rpInterval').value = '86400';
  document.getElementById('rpFormat').value = 'yaml';
  onRpTypeChange();
  showModal('rpModal');
}

function editRuleProvider(name) {
  const rp = ruleProviders[name];
  if (!rp) return;
  _editingRpName = name;
  document.getElementById('rpModalTitle').textContent = '编辑规则集合';
  document.getElementById('rpName').value = name;
  document.getElementById('rpName').disabled = true;
  document.getElementById('rpBehavior').value = rp.behavior || 'domain';
  document.getElementById('rpType').value = rp.type || 'http';
  document.getElementById('rpUrl').value = rp.url || '';
  document.getElementById('rpPath').value = rp.path || '';
  document.getElementById('rpInterval').value = rp.interval || 86400;
  document.getElementById('rpFormat').value = rp.format || 'yaml';
  onRpTypeChange();
  showModal('rpModal');
}

function onRpTypeChange() {
  const isHttp = document.getElementById('rpType').value === 'http';
  document.getElementById('rpUrlField').style.display = isHttp ? '' : 'none';
  document.getElementById('rpIntervalField').style.display = isHttp ? '' : 'none';
}

async function saveRuleProvider() {
  const name = _editingRpName || document.getElementById('rpName').value.trim();
  if (!name) { toast('请填写规则集合名称', 'error'); return; }
  if (!_editingRpName && name.includes('/')) { toast('名称不能包含 "/"', 'error'); return; }
  const body = {
    behavior: document.getElementById('rpBehavior').value,
    type: document.getElementById('rpType').value,
    url: document.getElementById('rpUrl').value.trim(),
    path: document.getElementById('rpPath').value.trim(),
    interval: parseInt(document.getElementById('rpInterval').value) || 86400,
    format: document.getElementById('rpFormat').value,
  };
  try {
    await api('/api/rule-providers/' + encodeURIComponent(name), { method: 'PUT', body });
    closeModal('rpModal');
    toast(_editingRpName ? '已更新' : '已创建');
    await loadAll();
  } catch (e) { toast('保存失败', 'error'); }
}

async function deleteRuleProvider(name) {
  if (!confirm('确定删除规则集合 "' + name + '"？')) return;
  await api('/api/rule-providers/' + encodeURIComponent(name), { method: 'DELETE' });
  toast('已删除');
  await loadAll();
}

// ── Quick Bindings ──
function renderBindings() {
  const list = document.getElementById('bindingList');
  if (!list) return;
  if (!bindings.length) {
    list.innerHTML = '<div style="padding:24px;color:var(--text2);text-align:center">暂无绑定，点击右上角添加</div>';
    return;
  }
  list.innerHTML = bindings.map(b => `
    <div class="proxy-item${b.enabled ? '' : ' disabled'}">
      <div class="name">${esc(b.label || '未命名')}<small>IP: ${esc(b.ip)} → ${esc(b.proxy)}</small></div>
      <label class="toggle" onclick="event.stopPropagation()">
        <input type="checkbox" ${b.enabled ? 'checked' : ''} onchange="toggleBinding('${esc(b.id)}', this.checked)">
        <span class="slider"></span>
      </label>
      <button class="btn btn-sm" onclick="editBinding('${esc(b.id)}')">编辑</button>
      <button class="btn btn-sm btn-danger" onclick="deleteBinding('${esc(b.id)}')">删除</button>
    </div>
  `).join('');
}

function showAddBindingModal() {
  document.getElementById('bindingEditId').value = '';
  document.getElementById('bindingModalTitle').textContent = '添加快捷绑定';
  document.getElementById('bLabel').value = '';
  document.getElementById('bIp').value = '';
  _fillBindingProxySelect();
  document.getElementById('bProxy').value = '';
  showModal('bindingModal');
}

function _fillBindingProxySelect() {
  const sel = document.getElementById('bProxy');
  sel.innerHTML = '<option value="">请选择代理 / 策略组</option>' +
    proxies.filter(p => p.enabled).map(p => `<option value="${esc(p.name)}">${esc(p.alias||p.name)}</option>`).join('') +
    groups.map(g => `<option value="${esc(g.name)}">${esc(g.name)} (策略组)</option>`).join('');
}

function editBinding(id) {
  const b = bindings.find(x => x.id === id);
  if (!b) return;
  document.getElementById('bindingEditId').value = id;
  document.getElementById('bindingModalTitle').textContent = '编辑快捷绑定';
  document.getElementById('bLabel').value = b.label;
  document.getElementById('bIp').value = b.ip;
  _fillBindingProxySelect();
  document.getElementById('bProxy').value = b.proxy;
  showModal('bindingModal');
}

async function saveBinding() {
  const id = document.getElementById('bindingEditId').value;
  const label = document.getElementById('bLabel').value.trim();
  const ip = document.getElementById('bIp').value.trim();
  const proxy = document.getElementById('bProxy').value;
  if (!ip)    { toast('请输入设备 IP', 'error'); return; }
  if (!proxy) { toast('请选择代理', 'error'); return; }
  try {
    if (id) {
      await api('/api/device-bindings/' + encodeURIComponent(id), { method: 'PUT', body: { label, ip, proxy } });
      toast('已更新');
    } else {
      await api('/api/device-bindings', { method: 'POST', body: { label, ip, proxy, enabled: true } });
      toast('已添加');
    }
    closeModal('bindingModal');
    bindings = await api('/api/device-bindings');
    renderBindings();
  } catch (e) { toast('保存失败: ' + e.message, 'error'); }
}

async function toggleBinding(id, enabled) {
  try {
    await api('/api/device-bindings/' + encodeURIComponent(id), { method: 'PUT', body: { enabled } });
    bindings = await api('/api/device-bindings');
    renderBindings();
  } catch (e) { toast('操作失败', 'error'); }
}

async function deleteBinding(id) {
  const b = bindings.find(x => x.id === id);
  if (!b || !confirm('确定删除绑定 "' + (b.label || b.ip) + '"？')) return;
  try {
    await api('/api/device-bindings/' + encodeURIComponent(id), { method: 'DELETE' });
    toast('已删除');
    bindings = await api('/api/device-bindings');
    renderBindings();
  } catch (e) { toast('删除失败', 'error'); }
}

// ── Tunnels ──
function renderTunnels() {
  const list = document.getElementById('tunnelList');
  if (!list) return;
  if (!tunnels.length) {
    list.innerHTML = '<div style="padding:24px;color:var(--text2);text-align:center">暂无流量隧道，点击右上角添加</div>';
    return;
  }
  list.innerHTML = tunnels.map(t => `
    <div class="proxy-item${t.enabled ? '' : ' disabled'}">
      <span class="type-badge">${esc((t.network || []).join('/').toUpperCase())}</span>
      <div class="name">${esc(t.label || '未命名隧道')}<small>${esc(t.address)} → ${esc(t.target)}${t.proxy ? ' · via ' + esc(t.proxy) : ' · 直连'}</small></div>
      <label class="toggle" onclick="event.stopPropagation()">
        <input type="checkbox" ${t.enabled ? 'checked' : ''} onchange="toggleTunnel('${esc(t.id)}', this.checked)">
        <span class="slider"></span>
      </label>
      <button class="btn btn-sm" onclick="editTunnel('${esc(t.id)}')">编辑</button>
      <button class="btn btn-sm btn-danger" onclick="deleteTunnel('${esc(t.id)}')">删除</button>
    </div>
  `).join('');
}

function _fillTunnelProxySelect() {
  const sel = document.getElementById('tProxy');
  sel.innerHTML = '<option value="">直连（不经过代理）</option>' +
    proxies.filter(p => p.enabled).map(p => `<option value="${esc(p.name)}">${esc(p.alias||p.name)}</option>`).join('') +
    groups.map(g => `<option value="${esc(g.name)}">${esc(g.name)} (策略组)</option>`).join('');
}

function showAddTunnelModal() {
  document.getElementById('tunnelEditId').value = '';
  document.getElementById('tunnelModalTitle').textContent = '添加流量隧道';
  document.getElementById('tLabel').value = '';
  document.getElementById('tAddress').value = '';
  document.getElementById('tTarget').value = '';
  document.getElementById('tNetTcp').checked = true;
  document.getElementById('tNetUdp').checked = false;
  _fillTunnelProxySelect();
  document.getElementById('tProxy').value = '';
  showModal('tunnelModal');
}

function editTunnel(id) {
  const tunnel = tunnels.find(x => x.id === id);
  if (!tunnel) return;
  document.getElementById('tunnelEditId').value = id;
  document.getElementById('tunnelModalTitle').textContent = '编辑流量隧道';
  document.getElementById('tLabel').value = tunnel.label || '';
  document.getElementById('tAddress').value = tunnel.address || '';
  document.getElementById('tTarget').value = tunnel.target || '';
  document.getElementById('tNetTcp').checked = (tunnel.network || []).includes('tcp');
  document.getElementById('tNetUdp').checked = (tunnel.network || []).includes('udp');
  _fillTunnelProxySelect();
  document.getElementById('tProxy').value = tunnel.proxy || '';
  showModal('tunnelModal');
}

async function saveTunnel() {
  const id = document.getElementById('tunnelEditId').value;
  const label = document.getElementById('tLabel').value.trim();
  const address = document.getElementById('tAddress').value.trim();
  const target = document.getElementById('tTarget').value.trim();
  const proxy = document.getElementById('tProxy').value;
  const network = [];
  if (document.getElementById('tNetTcp').checked) network.push('tcp');
  if (document.getElementById('tNetUdp').checked) network.push('udp');
  if (!network.length) { toast('请至少选择一种网络类型', 'error'); return; }
  if (!address) { toast('请输入本地监听地址', 'error'); return; }
  if (!target) { toast('请输入目标地址', 'error'); return; }
  const body = { label, address, target, proxy, network };
  try {
    if (id) {
      await api('/api/tunnels/' + encodeURIComponent(id), { method: 'PUT', body });
      toast('已更新');
    } else {
      await api('/api/tunnels', { method: 'POST', body: { ...body, enabled: true } });
      toast('已添加');
    }
    closeModal('tunnelModal');
    tunnels = await api('/api/tunnels');
    renderTunnels();
  } catch (e) { toast('保存失败: ' + e.message, 'error'); }
}

async function toggleTunnel(id, enabled) {
  try {
    await api('/api/tunnels/' + encodeURIComponent(id), { method: 'PUT', body: { enabled } });
    tunnels = await api('/api/tunnels');
    renderTunnels();
  } catch (e) { toast('操作失败', 'error'); }
}

async function deleteTunnel(id) {
  const tunnel = tunnels.find(x => x.id === id);
  if (!tunnel || !confirm('确定删除隧道 "' + (tunnel.label || tunnel.address) + '"？')) return;
  try {
    await api('/api/tunnels/' + encodeURIComponent(id), { method: 'DELETE' });
    toast('已删除');
    tunnels = await api('/api/tunnels');
    renderTunnels();
  } catch (e) { toast('删除失败', 'error'); }
}

// ── sing-box Port Bindings ──
function renderPortBindings() {
  const list = document.getElementById('portBindingList');
  if (!list) return;
  if (!portBindings.length) {
    list.innerHTML = '<div style="padding:24px;color:var(--text2);text-align:center">暂无端口绑定，点击右上角添加</div>';
    return;
  }
  list.innerHTML = portBindings.map(b => {
    const proxy = proxies.find(p => p.id === b.proxy);
    const target = proxy ? (proxy.alias || proxy.name) : '未选择节点';
    return `
    <div class="proxy-item${b.enabled ? '' : ' disabled'}">
      <span class="type-badge">${esc(b.inbound_type || 'mixed')}</span>
      <div class="name">${esc(b.label || '未命名端口')}<small>${esc(b.listen || '0.0.0.0')}:${b.port} → ${esc(target)}</small></div>
      <label class="toggle" onclick="event.stopPropagation()">
        <input type="checkbox" ${b.enabled ? 'checked' : ''} onchange="togglePortBinding('${esc(b.id)}', this.checked)">
        <span class="slider"></span>
      </label>
      <button class="btn btn-sm" onclick="editPortBinding('${esc(b.id)}')">编辑</button>
      <button class="btn btn-sm btn-danger" onclick="deletePortBinding('${esc(b.id)}')">删除</button>
    </div>`;
  }).join('');
}

function _fillPortBindingProxySelect() {
  const sel = document.getElementById('pbProxy');
  sel.innerHTML = '<option value="">请选择节点</option>' +
    proxies.filter(p => p.enabled).map(p => `<option value="${esc(p.id)}">${esc(p.alias || p.name)} (${esc(p.type)})</option>`).join('');
}

function showAddPortBindingModal() {
  document.getElementById('portBindingEditId').value = '';
  document.getElementById('portBindingModalTitle').textContent = '添加端口绑定';
  document.getElementById('pbLabel').value = '';
  document.getElementById('pbListen').value = '0.0.0.0';
  document.getElementById('pbPort').value = '7901';
  document.getElementById('pbInboundType').value = 'mixed';
  _fillPortBindingProxySelect();
  document.getElementById('pbProxy').value = '';
  showModal('portBindingModal');
}

function editPortBinding(id) {
  const binding = portBindings.find(x => x.id === id);
  if (!binding) return;
  document.getElementById('portBindingEditId').value = id;
  document.getElementById('portBindingModalTitle').textContent = '编辑端口绑定';
  document.getElementById('pbLabel').value = binding.label || '';
  document.getElementById('pbListen').value = binding.listen || '0.0.0.0';
  document.getElementById('pbPort').value = binding.port || 7901;
  document.getElementById('pbInboundType').value = binding.inbound_type || 'mixed';
  _fillPortBindingProxySelect();
  document.getElementById('pbProxy').value = binding.proxy || '';
  showModal('portBindingModal');
}

async function savePortBinding() {
  const id = document.getElementById('portBindingEditId').value;
  const label = document.getElementById('pbLabel').value.trim();
  const listen = document.getElementById('pbListen').value.trim() || '0.0.0.0';
  const port = parseInt(document.getElementById('pbPort').value) || 0;
  const inbound_type = document.getElementById('pbInboundType').value;
  const proxy = document.getElementById('pbProxy').value;
  if (port <= 0 || port > 65535) { toast('请输入有效端口', 'error'); return; }
  if (!proxy) { toast('请选择节点', 'error'); return; }
  const body = { label, listen, port, inbound_type, proxy };
  try {
    if (id) {
      await api('/api/port-bindings/' + encodeURIComponent(id), { method: 'PUT', body });
      toast('已更新');
    } else {
      await api('/api/port-bindings', { method: 'POST', body: { ...body, enabled: true } });
      toast('已添加');
    }
    closeModal('portBindingModal');
    portBindings = await api('/api/port-bindings');
    renderPortBindings();
  } catch (e) { toast('保存失败: ' + e.message, 'error'); }
}

async function togglePortBinding(id, enabled) {
  try {
    await api('/api/port-bindings/' + encodeURIComponent(id), { method: 'PUT', body: { enabled } });
    portBindings = await api('/api/port-bindings');
    renderPortBindings();
  } catch (e) { toast('操作失败', 'error'); }
}

async function deletePortBinding(id) {
  const binding = portBindings.find(x => x.id === id);
  if (!binding || !confirm('确定删除端口绑定 "' + (binding.label || binding.port) + '"？')) return;
  try {
    await api('/api/port-bindings/' + encodeURIComponent(id), { method: 'DELETE' });
    toast('已删除');
    portBindings = await api('/api/port-bindings');
    renderPortBindings();
  } catch (e) { toast('删除失败', 'error'); }
}

// ── Init ──
renderConfigDirtyBanner();
loadAll();
refreshStatus();
setInterval(refreshStatus, 5000);

// ── Logs ──
let _lastLogSignature = null;
let currentLogCore = 'mihomo';
let _logRequestId = 0;

function setLogCore(core) {
  currentLogCore = core === 'sing-box' ? 'sing-box' : 'mihomo';
  _lastLogSignature = null;
  document.getElementById('logPanelTitle').textContent = currentLogCore + ' 日志';
  document.getElementById('logCoreMihomo').classList.toggle('active', currentLogCore === 'mihomo');
  document.getElementById('logCoreSingBox').classList.toggle('active', currentLogCore === 'sing-box');
  refreshLogs();
}

async function refreshLogs() {
  const requestedCore = currentLogCore;
  const requestId = ++_logRequestId;
  try {
    const data = await api((requestedCore === 'sing-box' ? '/api/sing-box/logs' : '/api/logs') + '?n=500');
    if (requestId !== _logRequestId || requestedCore !== currentLogCore) return;
    const lines = data.lines || [];
    // The process buffers are capped at 500 lines.  Their content can change
    // while the count remains 500, so count-only comparison leaves stale logs.
    const signature = lines.join('\n');
    if (signature === _lastLogSignature) return;
    _lastLogSignature = signature;
    const viewer = document.getElementById('logViewer');
    const autoScroll = document.getElementById('logAutoScroll').checked;
    viewer.innerHTML = lines.map(l => {
      const cls = l.includes('ERR') || l.includes('error') ? 'log-error'
                : l.includes('WARN') || l.includes('warn')  ? 'log-warn'
                : l.includes('INFO') || l.includes('info')  ? 'log-info'
                : 'log-debug';
      return '<div class="log-line ' + cls + '">' + escHtml(l) + '</div>';
    }).join('');
    if (autoScroll) viewer.scrollTop = viewer.scrollHeight;
  } catch (e) {
    if (requestId !== _logRequestId || requestedCore !== currentLogCore) return;
    const viewer = document.getElementById('logViewer');
    if (viewer) viewer.innerHTML = '<div class="log-line log-error">日志加载失败：' + escHtml(e.message || '未知错误') + '</div>';
  }
}

async function clearLogs() {
  try {
    await api(currentLogCore === 'sing-box' ? '/api/sing-box/logs' : '/api/logs', { method: 'DELETE' });
    _lastLogSignature = '';
    document.getElementById('logViewer').innerHTML = '';
    toast(currentLogCore + ' 日志已清空');
  } catch (e) { toast('清空日志失败：' + e.message, 'error'); }
}

// ── Proxy Delay Test ──
async function testProxyDelay(id) {
  const p = proxies.find(x => x.id === id);
  if (!p) return;
  const name = p.name;  // always use actual name; alias is display-only
  proxyLatency[id] = null;  // mark as testing
  renderProxies();
  try {
    const data = await api('/api/proxy-delay/' + encodeURIComponent(name));
    proxyLatency[id] = (typeof data.delay === 'number') ? data.delay : -1;
  } catch (_) {
    proxyLatency[id] = -1;
  }
  renderProxies();
}

async function testAllProxies() {
  if (!proxies.length) return;
  await Promise.all(proxies.map(p => testProxyDelay(p.id)));
}
