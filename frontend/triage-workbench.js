/* 离线复核分流规则工作台
 * 断网时使用规则包内嵌的固定树在浏览器本地计算建议与命中分支，
 * 进度写入 localStorage 并在恢复网络后幂等补传。
 */
const twEndpoint = '/api/triage-packages';
const twStoreKey = 'lift_triage_workbench_v1';

function twEl(tag, text) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  return node;
}
function twEsc(value) {
  return String(value ?? '').replace(/[&<>"']/g, m =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'}[m]));
}
function twRole() {
  try { return JSON.parse(localStorage.getItem('lift_user') || 'null')?.role || 'viewer'; }
  catch { return 'viewer'; }
}
function twToken() { return localStorage.getItem('lift_token') || ''; }

async function twRequest(path = '', options = {}) {
  const response = await fetch(twEndpoint + path, {
    method: options.method || (options.body ? 'POST' : 'GET'),
    headers: {'Authorization': `Bearer ${twToken()}`, 'Content-Type': 'application/json'},
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail || data));
    error.payload = data.detail;
    error.status = response.status;
    throw error;
  }
  return data;
}

/* ----------------------------- 本地存储 ----------------------------- */
function twLoadStore() {
  try { return JSON.parse(localStorage.getItem(twStoreKey)) || {packages: {}, claims: {}, outbox: []}; }
  catch { return {packages: {}, claims: {}, outbox: []}; }
}
function twSaveStore(store) { localStorage.setItem(twStoreKey, JSON.stringify(store)); }
function twCachePackage(pack) {
  const store = twLoadStore();
  store.packages[pack.package_no] = pack;
  twSaveStore(store);
}
function twCacheClaim(claim) {
  const store = twLoadStore();
  const previous = store.claims[claim.claim_token] || {};
  store.claims[claim.claim_token] = {
    ...previous, ...claim,
    leaf_results: claim.checkpoint?.leaf_results || previous.leaf_results || [],
    node_checks: claim.checkpoint?.node_checks || previous.node_checks || [],
    node_confirmed: previous.node_confirmed || [],
  };
  twSaveStore(store);
}

/* ----------------------- 离线固定树本地计算引擎 ----------------------- */
function twEvaluate(tree, features) {
  let nodeId = tree.root;
  const path = [];
  let guard = 0;
  while (tree.nodes[nodeId].type === 'split') {
    if (++guard > Object.keys(tree.nodes).length) throw new Error('规则树遍历异常');
    const node = tree.nodes[nodeId];
    const raw = features[node.feature];
    const value = Number(raw);
    if (!Number.isFinite(value)) throw new Error(`特征 ${node.feature} 必须是数值`);
    const branch = value <= node.threshold ? 'left' : 'right';
    const nextNode = node[branch];
    path.push({node_id: node.node_id, feature: node.feature, threshold: node.threshold,
               value, branch, next_node: nextNode});
    nodeId = nextNode;
  }
  const leaf = tree.nodes[nodeId];
  return {leaf_id: nodeId, prediction: leaf.label, path};
}

function twNodeChecks(pack) {
  return pack.splits.map(s => ({node_id: s.node_id, feature: s.feature, threshold: s.threshold,
                                left_child: s.left_child, right_child: s.right_child}));
}

/* ------------------------------- 外发箱 ------------------------------- */
async function twFlushOutbox(statusEl) {
  const store = twLoadStore();
  if (!store.outbox.length) return;
  const pending = [...store.outbox];
  const failed = [];
  let returned = false;
  for (const item of pending) {
    try {
      if (item.kind === 'checkpoint') {
        const claim = await twRequest(`/claims/${item.token}/checkpoint`, {method: 'PUT', body: item.body});
        twCacheClaim(claim);
      } else if (item.kind === 'submit') {
        const claim = await twRequest(`/claims/${item.token}/submit`, {body: item.body});
        twCacheClaim(claim);
      }
    } catch (error) {
      if (error.status === 422 && error.payload?.code === 'REVIEW_RETURNED') {
        twCacheClaim(error.payload.claim);
        returned = true;
        continue; // 送审被退回，不再重试这一条
      }
      failed.push(item);
      break; // 仍处于断网，后续请求等待下次重试
    }
  }
  // Re-read before writing so progress saved while flushing is not lost.
  const latest = twLoadStore();
  latest.outbox = failed;
  twSaveStore(latest);
  if (returned) twRenderWork();
  if (statusEl) statusEl.textContent = failed.length ? `网络未恢复，${failed.length} 条请求待补传` : '待补传请求已全部送达';
}

/* ------------------------------- 界面 ------------------------------- */
const launch = twEl('button', '离线分流复核');
launch.id = 'triage-workbench-open';
launch.hidden = true;
const dialog = twEl('dialog');
dialog.id = 'triage-workbench';
dialog.setAttribute('aria-labelledby', 'triage-workbench-title');
const header = twEl('header');
const title = twEl('h2', '离线分流规则包工作台');
title.id = 'triage-workbench-title';
const closeBtn = twEl('button', '关闭');
closeBtn.addEventListener('click', () => dialog.close());
header.append(title, closeBtn);
const status = twEl('p');
status.setAttribute('role', 'status');
const offlineBadge = twEl('span', '当前离线：本地计算与保存，联网后自动补传');
offlineBadge.className = 'tw-offline';
const content = twEl('section');
dialog.append(header, offlineBadge, status, content);
document.body.append(launch, dialog);

const state = {packages: [], claims: [], openPackage: null, openToken: null, sequence: 0};

function twShowLaunch() {
  try { launch.hidden = !twToken(); } catch { launch.hidden = true; }
}
twShowLaunch();
window.addEventListener('storage', twShowLaunch);
setInterval(twShowLaunch, 600);
window.addEventListener('online', async () => {
  offlineBadge.classList.remove('show');
  status.textContent = '网络已恢复，正在补传离线请求…';
  try { await twRefresh(); } catch { /* 保留本地视图 */ }
  await twFlushOutbox(status);
  twRenderHome();
});
window.addEventListener('offline', () => offlineBadge.classList.add('show'));
setInterval(() => { if (dialog.open && navigator.onLine) twFlushOutbox(status); }, 5000);

function twBadge(value) {
  const map = {
    issued: ['已发放', 'issued'], returned: ['已退回', 'returned'],
    checking: ['核对中', 'checking'], submitted: ['待复核', 'submitted'],
    accepted: ['已通过', 'accepted'],
  };
  const [label, cls] = map[value] || [value, 'checking'];
  return `<span class="tw-badge ${cls}">${label}</span>`;
}

async function twRefresh() {
  const data = await twRequest('');
  state.packages = data.packages;
  state.claims = data.claims;
  for (const pkg of state.packages) {
    const local = twLoadStore().packages[pkg.package_no];
    if (!local && pkg.status === 'issued') {
      try { twCachePackage(await twRequest(`/${pkg.id}/download`)); } catch { /* 离线时用本地缓存 */ }
    }
  }
}

async function twRenderHome() {
  state.openPackage = null;
  state.openToken = null;
  const role = twRole();
  content.replaceChildren();
  const controls = twEl('div');
  controls.className = 'tw-controls';
  if (role === 'supervisor') {
    const issueBtn = twEl('button', '生成并发放新规则包');
    issueBtn.className = 'primary';
    issueBtn.addEventListener('click', async () => {
      try {
        const pack = await twRequest('', {body: {}});
        status.textContent = `已发放规则包 ${pack.package_no}`;
        await twRefresh();
        twRenderHome();
      } catch (error) { status.textContent = error.message; }
    });
    controls.append(issueBtn);
  }
  content.append(controls);

  const grid = twEl('div');
  grid.className = 'tw-package-grid';
  for (const pkg of state.packages) {
    const card = twEl('article');
    card.className = `tw-card ${pkg.status}`;
    card.innerHTML = `
      <div class="tw-no">${twEsc(pkg.package_no)}</div>
      <div>${twBadge(pkg.status)} <small>固定树 ${twEsc(pkg.tree_version)} · ${pkg.leaf_count} 个可达叶</small></div>
      <div class="tw-hash">sha256:${twEsc(pkg.content_hash)}</div>
      <div><small>认领 ${pkg.claim_count ?? 0} · 核对中 ${pkg.open_claim_count ?? 0}</small></div>`;
    const actions = twEl('div');
    const openBtn = twEl('button', '打开规则包');
    openBtn.addEventListener('click', () => twRenderPackage(pkg.id));
    const dlBtn = twEl('button', '下载离线 JSON');
    dlBtn.addEventListener('click', () => twDownload(pkg));
    actions.append(openBtn, dlBtn);
    card.append(actions);
    grid.append(card);
  }
  if (!state.packages.length) content.append(twEl('p', '尚未发放规则包，请主管先生成。'));
  content.append(twEl('h3', '规则包清单'), grid);

  const claimWrap = twEl('div');
  claimWrap.className = 'tw-claim-list';
  const localStore = twLoadStore();
  const allClaims = [...state.claims];
  for (const [token, local] of Object.entries(localStore.claims)) {
    if (!allClaims.some(c => c.claim_token === token)) allClaims.push(local);
  }
  const claimTitle = twEl('h3', '各复核点认领与进度');
  content.append(claimTitle);
  for (const claim of allClaims) {
    const row = twEl('div');
    row.className = 'tw-claim-row';
    const pct = Math.round(100 * (claim.checked_leaves || 0) / claim.total_leaves);
    row.innerHTML = `
      <div class="tw-grow"><strong>${twEsc(claim.review_point)}</strong>
        <div class="tw-hash">${twEsc(claim.package_no || '')} · ${twEsc(claim.claim_token.slice(0, 8))}…</div></div>
      ${twBadge(claim.status)}
      <div style="min-width:11rem"><div class="tw-progress"><i class="${pct === 100 ? 'done' : ''}" style="width:${pct}%"></i></div>
        <small>${claim.checked_leaves || 0}/${claim.total_leaves} 叶 · ${pct}%</small></div>
      ${claim.returned_mismatches?.length ? '<span class="tw-bad">有退回项</span>' : ''}`;
    if (role !== 'viewer') {
      const workBtn = twEl('button', '打开核对工作台');
      workBtn.className = 'primary';
      workBtn.addEventListener('click', () => twOpenWork(claim.claim_token));
      row.append(workBtn);
    }
    claimWrap.append(row);
  }
  content.append(claimWrap);
}

async function twRenderPackage(packageId) {
  content.replaceChildren();
  status.textContent = '正在读取规则包详情…';
  const detail = await twRequest(`/${packageId}`);
  const pack = twLoadStore().packages[detail.package_no]
    || await twRequest(`/${packageId}/download`).then(p => { twCachePackage(p); return p; });
  state.openPackage = pack;
  const back = twEl('button', '← 返回清单');
  back.className = 'tw-back';
  back.addEventListener('click', twRenderHome);
  content.append(back);

  const meta = twEl('article');
  meta.className = 'tw-card issued';
  meta.innerHTML = `
    <div class="tw-no">${twEsc(detail.package_no)}</div>
    ${twBadge(detail.status)}
    <div class="tw-hash">sha256:${twEsc(detail.content_hash)}</div>
    <small>固定树 ${twEsc(detail.tree_version)} · 模型参照 ${twEsc(pack.model_reference)} · 生成于 ${twEsc(pack.generated_at)}</small>`;
  const dlBtn = twEl('button', '下载离线 JSON');
  dlBtn.addEventListener('click', () => twDownload(pack));
  meta.append(dlBtn);
  content.append(meta);

  // 分界节点（送审时逐项核对分界值与两侧分支）
  const nodeTitle = twEl('h3', '分界节点（送审需逐个核对）');
  const nodeGrid = twEl('div');
  nodeGrid.className = 'tw-node-grid';
  for (const split of pack.splits) {
    const card = twEl('div');
    card.className = 'tw-node';
    card.innerHTML = `<strong>${twEsc(split.node_id)}</strong><br>
      特征 <code>${twEsc(split.feature)}</code><br>
      分界值 <code>${twEsc(split.threshold)}</code><br>
      左侧(≤) → <code>${twEsc(split.left_child)}</code><br>
      右侧(>) → <code>${twEsc(split.right_child)}</code>`;
    nodeGrid.append(card);
  }
  content.append(nodeTitle, nodeGrid);

  // 认领
  if (twRole() !== 'viewer' && detail.status === 'issued') {
    const form = twEl('form');
    form.className = 'tw-controls';
    const label = twEl('label', '离线复核点名称');
    const input = twEl('input');
    input.placeholder = '如：江宁分站离线复核点 A';
    input.required = true;
    input.maxLength = 120;
    label.append(input);
    const submit = twEl('button', '认领规则包');
    submit.className = 'primary';
    form.append(label, submit);
    form.addEventListener('submit', async event => {
      event.preventDefault();
      try {
        const claim = await twRequest(`/${packageId}/claims`, {body: {review_point: input.value.trim()}});
        twCachePackage(pack);
        twCacheClaim(claim);
        status.textContent = `认领成功，凭据 ${claim.claim_token.slice(0, 8)}…，中断后可用同一复核点续跑`;
        twOpenWork(claim.claim_token);
      } catch (error) { status.textContent = error.message; }
    });
    content.append(twEl('h3', '认领（断网也能领，重连后自动补登记）'), form);
  }

  const events = twEl('div');
  events.className = 'tw-claim-list';
  for (const claim of detail.claims) {
    const row = twEl('div');
    row.className = 'tw-claim-row';
    row.innerHTML = `<div class="tw-grow"><strong>${twEsc(claim.review_point)}</strong>
      <div class="tw-hash">${twEsc(claim.claim_token.slice(0, 8))}…</div></div>${twBadge(claim.status)}
      <small>${claim.checked_leaves}/${claim.total_leaves} 叶</small>`;
    if (twRole() !== 'viewer') {
      const workBtn = twEl('button', '打开核对工作台');
      workBtn.className = 'primary';
      workBtn.addEventListener('click', () => twOpenWork(claim.claim_token));
      row.append(workBtn);
    }
    if (twRole() === 'supervisor' && claim.status === 'submitted') {
      const accept = twEl('button', '复核通过');
      accept.addEventListener('click', async () => {
        await twRequest(`/claims/${claim.id}/accept`, {body: {}});
        status.textContent = '该复核点送审已通过';
        twRenderPackage(packageId);
      });
      const reject = twEl('button', '退回');
      reject.addEventListener('click', () => twSupervisorReturn(claim.id, packageId));
      row.append(accept, reject);
    }
    events.append(row);
  }
  content.append(twEl('h3', '该包的复核点进度'), events);
  status.textContent = '';
}

async function twSupervisorReturn(claimId, packageId) {
  const reason = prompt('请写明退回原因（将随对不上的输入一并退回复核点）');
  if (!reason || reason.trim().length < 4) { status.textContent = '退回原因至少 4 个字符'; return; }
  try {
    await twRequest(`/claims/${claimId}/return`, {body: {reason: reason.trim()}});
    status.textContent = '已退回该复核点';
    twRenderPackage(packageId);
  } catch (error) { status.textContent = error.message; }
}

function twDownload(pkg) {
  twCachePackage(pkg);
  const blob = new Blob([JSON.stringify(pkg, null, 2)], {type: 'application/json;charset=utf-8'});
  const url = URL.createObjectURL(blob);
  const link = twEl('a');
  link.href = url;
  link.download = `${pkg.package_no}.json`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  status.textContent = '规则包已下载，可完全离线使用';
}

/* ----------------------------- 核对工作台 ----------------------------- */
async function twOpenWork(token) {
  content.replaceChildren();
  state.openToken = token;
  await twFlushOutbox(status);
  const local = twLoadStore().claims[token];
  let claim = state.claims.find(c => c.claim_token === token);
  if (!claim) {
    try { await twRefresh(); claim = state.claims.find(c => c.claim_token === token); } catch { /* 离线 */ }
  }
  claim = claim || local;
  if (!claim) { status.textContent = '找不到认领记录'; return; }
  twCacheClaim(claim);
  const pack = twLoadStore().packages[claim.package_no];
  if (!pack) { status.textContent = '本地缺少规则包，请先联网打开并下载该包'; return; }
  state.openPackage = pack;
  twRenderWork();
}

function twCurrentClaim() {
  return twLoadStore().claims[state.openToken];
}

function twRenderWork() {
  const claim = twCurrentClaim();
  const pack = state.openPackage;
  if (!claim || !pack) return;
  const offline = !navigator.onLine;
  offlineBadge.classList.toggle('show', offline);
  content.replaceChildren();

  const back = twEl('button', '← 返回清单');
  back.className = 'tw-back';
  back.addEventListener('click', twRenderHome);
  const head = twEl('div');
  head.innerHTML = `<h3>${twEsc(claim.review_point)} · ${twEsc(pack.package_no)}</h3>
    <div>${twBadge(claim.status)} <small>进度 ${claim.checked_leaves || 0}/${claim.total_leaves} ·
    凭据 ${twEsc(claim.claim_token.slice(0, 8))}…</small></div>
    <div class="tw-hash">固定包 sha256:${twEsc(pack.content_hash)}</div>`;
  content.append(back, head);

  // 退回项：对不上的输入、期望值与实际值
  if (claim.returned_mismatches?.length) {
    const box = twEl('section');
    box.append(twEl('h4', `退回项（${claim.returned_mismatches.length} 条）— 修正后重新核对并送审`));
    for (const mismatch of claim.returned_mismatches) {
      const item = twEl('div');
      item.className = 'tw-mismatch';
      item.innerHTML = `<strong>${twEsc(mismatch.type)}</strong> · ${twEsc(mismatch.target_ref)}<br>
        ${twEsc(mismatch.message)}
        ${mismatch.input ? `<br>对不上的输入：<code>${twEsc(JSON.stringify(mismatch.input))}</code>` : ''}
        ${mismatch.expected !== undefined && mismatch.expected !== null ? `<br>期望：<code>${twEsc(JSON.stringify(mismatch.expected))}</code>` : ''}
        ${mismatch.actual !== undefined && mismatch.actual !== null ? `<br>实际：<code>${twEsc(JSON.stringify(mismatch.actual))}</code>` : ''}`;
      box.append(item);
    }
    content.append(box);
  }

  // 分界值核对
  const nodeSection = twEl('section');
  nodeSection.append(twEl('h4', '分界值与两侧分支核对'));
  const confirmed = new Set(claim.node_confirmed || []);
  const nodeGrid = twEl('div');
  nodeGrid.className = 'tw-node-grid';
  for (const split of pack.splits) {
    const card = twEl('div');
    card.className = 'tw-node';
    const checkId = `tw-node-${split.node_id}`;
    card.innerHTML = `<strong>${twEsc(split.node_id)}</strong><br>
      <code>${twEsc(split.feature)} ≤ ${twEsc(split.threshold)}</code><br>
      左 <code>${twEsc(split.left_child)}</code> / 右 <code>${twEsc(split.right_child)}</code>`;
    const checkLabel = twEl('label', `已核对 ${split.node_id}`);
    checkLabel.style.flex = 'none';
    const checkbox = twEl('input');
    checkbox.type = 'checkbox';
    checkbox.id = checkId;
    checkbox.checked = confirmed.has(split.node_id);
    checkbox.addEventListener('change', () => {
      const store = twLoadStore();
      const current = new Set(store.claims[state.openToken].node_confirmed || []);
      checkbox.checked ? current.add(split.node_id) : current.delete(split.node_id);
      store.claims[state.openToken].node_confirmed = [...current];
      twSaveStore(store);
    });
    checkLabel.append(checkbox);
    card.append(checkLabel);
    nodeGrid.append(card);
  }
  nodeSection.append(nodeGrid);
  content.append(nodeSection);

  // 逐叶核对
  const leafSection = twEl('section');
  leafSection.append(twEl('h4', '逐个可达叶核对（断网可算，保存支持续跑）'));
  const checked = Object.fromEntries((claim.leaf_results || []).map(r => [r.leaf_id, r]));
  for (const leaf of pack.leaves) {
    const card = twEl('div');
    card.className = `tw-leaf ${checked[leaf.leaf_id] ? 'checked' : ''}`;
    const titleRow = twEl('div');
    titleRow.innerHTML = `<strong>${twEsc(leaf.leaf_id)}</strong> · 期望建议
      <span class="tw-ok">${twEsc(leaf.prediction)}</span> · ${twEsc(leaf.advice)}`;
    card.append(titleRow);

    const featureGrid = twEl('div');
    featureGrid.className = 'tw-feature-grid';
    const inputs = {};
    for (const name of Object.keys(pack.feature_schema)) {
      const label = twEl('label', name);
      const input = twEl('input');
      input.type = 'number';
      input.step = name === 'average_score' ? '0.01' : '1';
      input.value = checked[leaf.leaf_id]?.input?.[name] ?? leaf.witness_input[name];
      inputs[name] = input;
      label.append(input);
      featureGrid.append(label);
    }
    card.append(featureGrid);

    const resultBox = twEl('div');
    const prior = checked[leaf.leaf_id];
    if (prior) {
      resultBox.innerHTML = `<span class="tw-ok">已核对：命中 ${twEsc(prior.prediction)}</span>`;
      const pathPre = twEl('pre');
      pathPre.className = 'tw-path';
      pathPre.textContent = twFormatPath(prior.path);
      resultBox.append(pathPre);
    }
    const calcBtn = twEl('button', '离线计算建议与命中分支');
    calcBtn.addEventListener('click', () => {
      try {
        const features = Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k, Number(el.value)]));
        const result = twEvaluate(pack.tree, features);
        const advice = pack.advice_text[result.prediction];
        const pre = twEl('pre');
        pre.className = 'tw-path';
        pre.textContent = `命中叶：${result.leaf_id}（期望 ${leaf.leaf_id}）\n建议：${result.prediction} · ${advice}\n\n${twFormatPath(result.path)}`;
        resultBox.replaceChildren(pre);
        resultBox.dataset.result = JSON.stringify({...result, advice, input: features});
      } catch (error) { status.textContent = error.message; }
    });
    const saveBtn = twEl('button', '核对通过并保存进度');
    saveBtn.className = 'primary';
    saveBtn.addEventListener('click', () => twSaveLeaf(leaf, inputs, resultBox, card));
    card.append(calcBtn, saveBtn, resultBox);
    leafSection.append(card);
  }
  content.append(leafSection);

  // 送审
  const allConfirmed = pack.splits.every(s => (claim.node_confirmed || []).includes(s.node_id));
  const allChecked = pack.leaves.every(l => (claim.leaf_results || []).some(r => r.leaf_id === l.leaf_id));
  const submitBar = twEl('div');
  submitBar.className = 'tw-controls';
  const submitBtn = twEl('button', '送审（服务端按固定包与固定树逐项复核）');
  submitBtn.className = 'primary';
  submitBtn.disabled = !allConfirmed || !allChecked || claim.status === 'submitted' || claim.status === 'accepted';
  submitBtn.title = !allChecked ? '还有可达叶未核对' : !allConfirmed ? '还有分界节点未核对' : '';
  submitBtn.addEventListener('click', () => twSubmit());
  submitBar.append(submitBtn);
  if (!allChecked || !allConfirmed) {
    submitBar.append(twEl('small', `可达叶 ${Object.keys(checked).length}/${pack.leaves.length}，分界节点 ${(claim.node_confirmed || []).length}/${pack.splits.length}`));
  }
  content.append(submitBar);
  status.textContent = offline ? '离线模式：计算与保存均在本地，联网自动补传' : '';
}

function twFormatPath(path) {
  return path.map(step =>
    `${step.node_id}：${step.feature}=${step.value} ${step.branch === 'left' ? '≤' : '>'} ${step.threshold} → ${step.next_node}`
  ).join('\n');
}

async function twSaveLeaf(leaf, inputs, resultBox, card) {
  const pack = state.openPackage;
  const features = Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k, Number(el.value)]));
  let result;
  try {
    result = twEvaluate(pack.tree, features);
  } catch (error) { status.textContent = error.message; return; }
  if (result.leaf_id !== leaf.leaf_id) {
    status.textContent = `输入实际命中 ${result.leaf_id}，与所核叶 ${leaf.leaf_id} 不一致，请调整输入`;
    return;
  }
  const record = {leaf_id: leaf.leaf_id, input: features,
                  prediction: result.prediction, advice: pack.advice_text[result.prediction], path: result.path};
  // Persist locally first so an interrupted save always keeps its progress.
  const local = twLoadStore();
  const claim = local.claims[state.openToken];
  claim.leaf_results = [...(claim.leaf_results || []).filter(r => r.leaf_id !== leaf.leaf_id), record];
  claim.checked_leaves = claim.leaf_results.length;
  const body = {checkpoint: {
    leaf_results: claim.leaf_results,
    node_checks: twNodeChecks(pack),
    saved_at: new Date().toISOString(),
  }};
  twSaveStore(local);

  if (!navigator.onLine) {
    const queued = twLoadStore();
    // Retries replace the queued checkpoint rather than duplicating packets.
    queued.outbox = queued.outbox.filter(item => !(item.token === state.openToken && item.kind === 'checkpoint'));
    queued.outbox.push({id: crypto.randomUUID(), token: state.openToken, kind: 'checkpoint', body});
    twSaveStore(queued);
    status.textContent = `离线保存叶 ${leaf.leaf_id}，已加入补传队列`;
    twRenderWork();
    return;
  }
  try {
    const updated = await twRequest(`/claims/${state.openToken}/checkpoint`, {method: 'PUT', body});
    twCacheClaim(updated);
    status.textContent = `叶 ${leaf.leaf_id} 已保存，进度 ${updated.checked_leaves}/${updated.total_leaves}`;
  } catch (error) {
    const queued = twLoadStore();
    queued.outbox = queued.outbox.filter(item => !(item.token === state.openToken && item.kind === 'checkpoint'));
    queued.outbox.push({id: crypto.randomUUID(), token: state.openToken, kind: 'checkpoint', body});
    twSaveStore(queued);
    status.textContent = `网络异常：叶 ${leaf.leaf_id} 已本地保存，将在联网后补传`;
  }
  twRenderWork();
}

async function twSubmit() {
  const pack = state.openPackage;
  const claim = twCurrentClaim();
  const body = {
    package_hash: pack.content_hash,
    leaf_results: claim.leaf_results,
    node_checks: twNodeChecks(pack),
    saved_at: new Date().toISOString(),
  };
  if (!navigator.onLine) {
    const store = twLoadStore();
    store.outbox = store.outbox.filter(item => !(item.token === state.openToken && item.kind === 'submit'));
    store.outbox.push({id: crypto.randomUUID(), token: state.openToken, kind: 'submit', body});
    twSaveStore(store);
    status.textContent = '离线：送审请求已排队，联网后自动发送，重试不会重复发包';
    return;
  }
  try {
    const updated = await twRequest(`/claims/${state.openToken}/submit`, {body});
    twCacheClaim(updated);
    status.textContent = '送审成功，等待主管复核';
    twRenderWork();
  } catch (error) {
    if (error.status === 422 && error.payload?.code === 'REVIEW_RETURNED') {
      twCacheClaim(error.payload.claim);
      status.textContent = `送审被退回：${error.payload.mismatches.length} 项不一致，请按退回项修正`;
      twRenderWork();
    } else {
      status.textContent = error.message;
    }
  }
}

launch.addEventListener('click', async () => {
  dialog.showModal();
  offlineBadge.classList.toggle('show', !navigator.onLine);
  status.textContent = '正在加载规则包…';
  try {
    await twRefresh();
    await twFlushOutbox(status);
    twRenderHome();
  } catch (error) {
    // 离线打开：使用本地缓存继续核对
    if (twLoadStore().packages && Object.keys(twLoadStore().packages).length) {
      state.packages = [];
      state.claims = Object.values(twLoadStore().claims);
      twRenderHome();
      status.textContent = '离线模式：仅显示本机已缓存的规则包与进度';
    } else {
      status.textContent = error.message;
    }
  }
});
