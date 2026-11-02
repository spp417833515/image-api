/* 顶栏：ComfyUI 在线状态 + 版本浮层（GET /api/status，只显示，不能操作） */
import { $, esc, fmtT, routeName } from './dom.js';
import { icon } from './icons.js';
import { S, currentModel, currentModelId, routesOff } from './store.js';

const verOf = v => v.tag || v.commit;

export function renderTop(){
  renderModel();
  const s = S.status, el = $('#status'), chev = icon(S.popOpen ? 'up' : 'down', 12);
  let cls = 'status', txt;
  if (S.statusError){ cls += ' down'; txt = S.statusError.trouble.title; }
  else if (!s){ cls += ' wait'; txt = '连接中…'; }
  else {
    const v = `<span class="ver">${esc(verOf(s.version))}</span>`, off = routesOff(s);
    const passed = s.last_check && s.last_check.items.every(x => x.ok);
    txt = `ComfyUI ${v}${s.last_check ? (passed ? ' · 检查通过' : ' · 检查没过') : ''}`;
    if (s.upgrading){ cls += ' warn busy'; txt = `正在升级到 <span class="ver">${esc(s.upgrading.target)}</span>`; }
    else if (!s.online){ cls += ' down'; txt = `ComfyUI ${v} · 没开`; }
    else if (off.length){ cls += ' warn'; txt = `ComfyUI ${v} · ${off.map(routeName).join('、')}不可用`; }
    else if (s.last_check && !passed) cls += ' warn';
  }
  el.className = cls + (S.popOpen ? ' open' : '');
  el.innerHTML = `<i class="dot"></i>${txt}${chev}`;
  el.setAttribute('aria-expanded', S.popOpen ? 'true' : 'false');
  const pop = $('#pop'); pop.hidden = !S.popOpen;
  pop.innerHTML = !S.popOpen ? '' : S.statusError ? errorPop(S.statusError) : s ? popHTML(s) : '';
}

/* 顶栏的模型选择。选项没变就不重画，免得把正在打开的列表关掉 */
function renderModel(){
  const sel = $('#model'), name = $('#modelName');
  const models = S.status && S.status.models || [];
  const id = currentModelId();
  const picked = currentModel(S.status);
  if (name) name.textContent = `${picked ? picked.label : 'Qwen-Image 2.1'} · 本机`;
  if (!sel) return;
  const ids = models.map(m => m.id).join('|');
  if (sel.dataset.ids !== ids){
    sel.innerHTML = models.map(m => `<option value="${esc(m.id)}">${esc(m.label)}</option>`).join('');
    sel.dataset.ids = ids;
  }
  if (sel.value !== id) sel.value = id;
  sel.disabled = !!S.job || !models.length;
  const off = picked && Object.values(picked.routes).some(r => !r.available);
  sel.title = off ? '这个模型的权重或节点还没齐，生成时会说明缺什么' : '切换生图模型';
}

/* 后端本身连不上 / 出错：说清楚是哪个请求、后端给了什么 */
function errorPop(err){
  return `<div class="pop-h">后端服务<span class="tag bad">${err.trouble.tag}</span></div>
    <div class="pop-row"><div class="pop-k"><span>原因</span><span>${esc(err.source)}</span></div>${err.message !== err.trouble.title ? `<div class="pop-op">${esc(err.message)}</div>` : ''}${err.raw ? `<div class="pop-raw">${esc(err.raw)}</div>` : ''}</div>
    ${S.status ? `<div class="pop-row"><div class="pop-k">上次读到的版本</div><div class="pop-v">${esc(verOf(S.status.version))}</div></div>` : ''}`;
}

/* 版本浮层：只显示，不放任何操作按钮（升级 / 回滚走命令行） */
function popHTML(s){
  const off = routesOff(s);
  const tag = s.upgrading ? ['warn', '正在升级'] : !s.online ? ['bad', '没开'] : off.length ? ['warn', off.map(routeName).join('、') + '不可用'] : ['ok', '在线'];
  const cur = s.upgrading ? `${verOf(s.version)} → ${s.upgrading.target}` : [s.version.tag, s.version.commit].filter(Boolean).join(' · ');
  const c = s.last_check;
  const checks = c
    ? `<div class="pop-row"><div class="pop-k"><span>上次检查（${fmtT(c.at)}）</span><span>${esc(verOf(c.version))}</span></div><div class="pop-checks">${c.items.map(x => x.ok
        ? `<span class="ok">${icon('check', 14)}${esc(x.label)}</span>`
        : `<span class="no">${icon('x', 14)}${esc(x.label)}${x.note ? `<em>${esc(x.note)}</em>` : ''}</span>`).join('')}</div></div>`
    : `<div class="pop-row"><div class="pop-k">检查</div><div class="pop-op">${s.upgrading ? `升级完自动检查，没过就退回 ${esc(verOf(s.version))}` : '还没检查过'}</div></div>`;
  const rs = Object.entries(s.routes), same = rs.every(([, r]) => !r.available && r.reason === rs[0][1].reason);
  const routes = same ? `<span class="no">${icon('x', 14)}都用不了<em>${esc(rs[0][1].reason)}</em></span>`
    : rs.map(([k, r]) => r.available ? `<span class="ok">${icon('check', 14)}${routeName(k)}</span>`
                                     : `<span class="no">${icon('x', 14)}${routeName(k)}</span><span class="pop-why">${esc(r.reason)}</span>`).join('');
  const o = s.last_operation;
  const op = s.upgrading ? ['正在升级', `${fmtT(s.upgrading.since)} 开始：${esc(verOf(s.version))} → ${esc(s.upgrading.target)}`]
    : o ? ['最近一次操作', `${fmtT(o.at)} ${o.kind === 'upgrade' ? '升级' : '回滚'} ${esc(verOf(o.from))} → ${esc(verOf(o.to))}，${o.result === 'passed' ? '检查通过' : '检查没过，已退回' + (o.note ? `（${esc(o.note)}）` : '')}`] : null;
  return `<div class="pop-h">ComfyUI<span class="tag ${tag[0]}">${tag[1]}</span></div>
    <div class="pop-row"><div class="pop-k">当前版本</div><div class="pop-v">${esc(cur)}</div></div>
    ${checks}
    <div class="pop-row"><div class="pop-k"><span>现在能用</span><span>实时</span></div><div class="pop-checks pop-routes">${routes}</div></div>
    ${op ? `<div class="pop-row"><div class="pop-k">${op[0]}</div><div class="pop-op">${op[1]}</div></div>` : ''}
    <div class="pop-cli">检查只在升级、回滚时跑。升级、回滚用命令行：<code>image-api comfy upgrade &lt;版本&gt;</code> · <code>image-api comfy rollback</code></div>`;
}
