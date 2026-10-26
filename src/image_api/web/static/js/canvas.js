/* 中间画布：头部（结果切换、涂蒙版入口）、图框（结果 / 底图 / 占位）、横幅、进度、底部信息行 */
import { $, esc, cssVar, mountToast, routeName, when } from './dom.js';
import { icon } from './icons.js';
import { S, pl, isDone, prevImage, routeOf, isSoft, rejectOf, fits, stepIdx, runningIdx, pctOf, routesOff, routesOn } from './store.js';
import { rawHTML } from './raw.js';

/* 现在画布上显示什么（由 S.view 决定） */
export function resolveView(){
  const v = S.view;
  if (!v) return { empty:true };
  if (v.kind === 'history'){
    const h = S.history.find(x => x.id === v.hid);
    return h ? { label:`<b>历史</b> · ${when(h)} · 第 ${h.step} 步`, img:h, info:h } : { empty:true };
  }
  const i = stepIdx(v.id), st = S.steps[i];
  if (!st) return { empty:true };
  const res = isDone(st) ? st.run.image : null;
  if (res && (v.kind === 'result' || (v.kind === 'step' && !S.job))) return { label:`<b>第 ${i + 1} 步结果</b>`, img:res, info:res, st, i };
  const idx = v.kind === 'item' ? v.idx : 0, it = st.items[idx], p = pl(i), rt = routeOf(i);
  if (it){
    const lead = idx === 0, what = lead && rt === 'mask' ? '底图' : `图 ${idx + 1}`;
    const base = { st, i, idx, what, out:lead && p ? p.output : null, mask:lead ? st.mask : null };
    if (it.kind === 'prev'){
      const r = prevImage(i), label = `<b>第 ${i + 1} 步</b> · ${what}（上一步的图）`;
      if (r) return { ...base, label, img:r, canPaint:lead };
      const po = pl(i - 1) && pl(i - 1).output, sz = po || S.status.limits.default_size;   // 占位框按上一步的预计出图尺寸
      return { ...base, label, ph:{ width:sz.width, height:sz.height, known:!!po, t:`第 ${i} 步的图`, s:'还没出图' }, waitPrev:lead };
    }
    return { ...base, label:`<b>第 ${i + 1} 步</b> · ${what}`, img:it, canPaint:lead };
  }
  if (S.steps.length === 1 && !st.instruction.trim() && !st.width && !st.height) return { empty:true, st, i, label:`<b>第 ${i + 1} 步</b>` };
  const o = p && p.output, sz = o || S.status.limits.default_size, rej = rejectOf(st, i);
  return { label:`<b>第 ${i + 1} 步</b>${rt ? ' · ' + routeName(rt) : ''}`, st, i, t2i:true,
           ph:{ width:sz.width, height:sz.height, known:!!o, t:o ? `${o.width} × ${o.height}` : rej && !isSoft(rej) ? '这一步要改一下' : '写一句指令',
                s:o ? '点「生成」出图' : rej && !isSoft(rej) ? '看左边卡片上的说明' : '写好就能出图' } };
}

function headActs(V, paint){
  const results = S.steps.map((s, k) => ({ s, k })).filter(o => isDone(o.s) || (o.s.run && o.s.run.state === 'failed'));
  let acts = '';
  if (results.length && !S.job){
    acts += `<div class="seg">${results.map(o => {
      const on = S.view.kind === 'result' && S.view.id === o.s.id ? 'on' : '';
      return isDone(o.s)
        ? `<button class="${on}" data-act="view-result" data-id="${o.s.id}"><img src="${esc(o.s.run.image.url)}" alt="">第 ${o.k + 1} 步</button>`
        : `<button class="fail" data-act="goto-step" data-id="${o.s.id}"><span class="fx">${icon('alert', 11)}</span>第 ${o.k + 1} 步</button>`;
    }).join('')}</div>`;
  }
  if (V.canPaint && !paint && !S.job) acts += V.mask && !fits(V.mask, V.img)
    ? `<button class="chip" data-act="repaint">${icon('brush', 14)}重新涂</button>`
    : `<button class="chip" data-act="paint">${icon('brush', 14)}${V.mask ? '改蒙版' : '涂蒙版'}</button>`;
  return acts;
}

/* 画笔条上的笔刷预览点：跟着笔刷大小变，限制在 3–16 px 好看清 */
export const brushDot = size => Math.max(3, Math.min(16, size / 8));

function brushbarHTML(){
  const sz = S.brush.size, d = brushDot(sz);
  return `<div class="brushbar">
      <button class="tb ${S.brush.erase ? '' : 'on'}" data-act="tool-brush">${icon('brush', 15)}画笔</button>
      <button class="tb ${S.brush.erase ? 'on' : ''}" data-act="tool-erase">${icon('eraser', 15)}橡皮</button>
      <span class="sep"></span>
      <label class="size"><span class="pv"><i style="width:${d}px;height:${d}px"></i></span>
        <input type="range" min="8" max="240" step="4" value="${sz}" data-field="brush"><span id="bsz">${sz}</span></label>
      <span class="sep"></span>
      <button class="tb" data-act="mask-clear">${icon('trash', 15)}清空</button>
      <button class="tb done" data-act="mask-done">${icon('check', 15)}涂好了</button></div>`;
}

/* 请求失败的说明卡：哪个操作失败、后端说了什么、原文；哪个请求 + HTTP 状态只写一处 */
function faultHTML(f){
  const e = f.err;
  return `<div class="fault"><div class="msg">${icon('alert', 15)}<div class="txt"><b>${esc(f.title)}</b>：${esc(e.message)}${f.note ? `<span class="mut">· ${esc(f.note)}</span>` : ''}${e.raw
      ? rawHTML(e.raw, f.open, e.source, true) : `<span class="mut">${esc(e.source)}</span>`}</div>
    <button class="ib" data-act="fault-close" title="知道了">${icon('x', 14)}</button></div></div>`;
}

/* 画布顶上的横幅：同时只显示一条，按重要程度挑 */
function bannerHTML(){
  const st = S.status, failed = S.steps.findIndex(s => s.run && s.run.state === 'failed'), off = routesOff(st);
  if (S.fault) return faultHTML(S.fault);
  if (!S.job && failed >= 0)
    return `<div class="banner bad">${icon('alert', 15)}第 ${failed + 1} 步失败<button class="chip" data-act="goto-step" data-id="${S.steps[failed].id}">看原文</button></div>`;
  if (S.statusError)
    return `<div class="banner bad">${icon('plug', 15)}${S.statusError.trouble.title}<span class="mut">· 现在没法生成，原因看右上角；好了点「重新检测」</span><button class="chip" data-act="recheck">${icon('refresh', 13)}重新检测</button></div>`;
  if (st.upgrading)
    return `<div class="banner warn">${icon('refresh', 15)}ComfyUI 正在升级到 ${esc(st.upgrading.target)}<span class="mut">· 图和历史照常看</span></div>`;
  if (!st.online)
    return `<div class="banner bad">${icon('plug', 15)}ComfyUI 没开<span class="mut">· 打开后点「重新检测」，图和历史照常看</span><button class="chip" data-act="recheck">${icon('refresh', 13)}重新检测</button></div>`;
  if (off.length){
    const ok = routesOn(st);
    return `<div class="banner warn">${icon('alert', 15)}${off.map(routeName).join('、')}暂时用不了${ok.length ? `<span class="mut">· ${ok.map(routeName).join('、')}照常</span>` : ''}</div>`;
  }
  return '';
}

function progressHTML(){
  const ri = runningIdx(); if (!S.job || ri < 0) return '';
  const r = S.steps[ri].run, pct = pctOf(r);
  return `<div class="over-card" id="prog"><div class="prog-k"><i class="spin"></i>第 ${ri + 1} / ${S.steps.length} 步 · ${routeName(r.route)}</div>
      <div class="prog-pct"><span id="pp">${pct}</span><small>%</small></div>
      <div class="prog-bar"><i id="pb" style="width:${pct}%"></i></div>
      <div class="prog-steps">${S.steps.map((s, k) => { const x = s.run && s.run.state, rt = routeOf(k);
        const t = x === 'done' ? '已出图' : x === 'running' ? '在跑' : x === 'queued' ? '排队' : '这次不跑';
        return `<span class="${x === 'done' ? 'ok' : x === 'running' ? 'on' : ''}">${x === 'done' ? icon('check', 12) : `<i class="rdot ${rt || ''}"></i>`}第 ${k + 1} 步 ${t}</span>`; }).join('')}</div></div>`;
}

function footHTML(V, paint, showMask){
  const sz = o => `${o.width}×${o.height}`, st = S.status;
  if (V.info && !S.job){
    const x = V.info;
    return `<span class="tag ${x.route}">${routeName(x.route)}</span><span class="meta">${sz(x)}</span><span class="sepdot"></span><span class="meta">seed ${x.seed}</span>
      <span class="f-sp"></span><a class="btn" href="${esc(x.url)}" download>${icon('download', 15)}下载</a>
      <button class="btn pri" data-act="continue">接着改${icon('arrow', 15)}</button>`;
  }
  if (paint) return `<span class="hint"><i class="sw"></i>涂到的地方会重画，其它保持不变</span><span class="f-sp"></span><span class="meta k">底图</span><span class="meta">${sz(V.img)}</span>`;
  if (V.waitPrev) return `<span class="meta k">第 ${V.i} 步的图</span>${V.ph.known ? `<span class="meta k">预计</span><span class="meta">${sz(V.ph)}</span>` : ''}
${V.mask ? '' : `<span class="meta k">· 想涂蒙版，先出第 ${V.i} 步的图</span>`}`;
  if (showMask) return `<span class="hint"><i class="sw"></i>粉色区域会重画</span><span class="f-sp"></span>${V.out ? `<span class="meta k">出图</span><span class="meta">${sz(V.out)}</span>` : ''}`;
  if (V.t2i){
    const p = pl(V.i);
    return p && p.output ? `<span class="meta k">出图尺寸</span><span class="meta">${sz(p.output)}</span>${p.requested ? `<span class="warnt">（填的 ${sz(p.requested)}，已取整到 ${st.limits.size_multiple} 的倍数）</span>` : ''}` : '';
  }
  if (V.img) return `<span class="meta k">${V.what}</span><span class="meta">${sz(V.img)}</span>${V.out ? `<span class="meta k">· 出图</span><span class="meta">${sz(V.out)}</span>` : ''}`;
  if (V.empty) return `<span class="meta k">支持点击、拖入、粘贴图片，最多 ${st.limits.max_images} 张</span>`;
  return '';
}

/* 一次状态都没读到：页面别的部分都依赖它（限制、预设尺寸），画布上只说清楚原因 */
function renderBoot(){
  const e = S.statusError;
  $('#sHead').innerHTML = '<div class="s-label"><b>画布</b></div>'; $('#sFoot').innerHTML = '';
  $('#stage').innerHTML = !e ? '' : `<div class="s-empty"><div class="big">${icon('plug', 34)}</div><h2>${e.trouble.title}</h2>
    ${e.message !== e.trouble.title ? `<p>${esc(e.message)}</p>` : ''}
    ${e.raw ? rawHTML(e.raw, true, e.source, false) : `<p>${esc(e.source)}</p>`}
    <button class="chip" data-act="recheck">${icon('refresh', 13)}重新检测</button></div>`;
  mountToast($('#stage'));
}

export function renderStage(){
  if (!S.status) return renderBoot();
  const V = resolveView(), stage = $('#stage'), paint = S.mode === 'mask';
  $('#sHead').innerHTML = `<div class="s-label">${V.label || '<b>画布</b>'}</div><div class="s-acts">${headActs(V, paint)}</div>`;
  let h = '';
  if (V.empty){
    h = `<div class="s-empty"><div class="big">${icon('image', 34)}</div><h2>写一句话，点生成</h2>
      <p>想改现有的图？把图拖到这里、粘贴，或者 <button class="linkbtn" data-act="pick">选图</button></p></div>`;
  } else {
    const w = V.img ? V.img.width : V.ph.width, hh = V.img ? V.img.height : V.ph.height;
    const run = S.job && V.st && V.st.run && V.st.run.state === 'running';
    const upto = V.waitPrev && !V.mask && !S.job ? `<button class="chip" data-act="run-upto" data-upto="${V.i - 1}">${icon('play', 13)}先出第 ${V.i} 步的图</button>` : '';
    const cls = ['frame', V.img ? '' : 'ph', paint ? 'painting' : '', run ? 'shimmer' : '', run && V.img ? 'dim' : ''].join(' ');
    h = `<div class="${cls}" id="frame" data-w="${w}" data-h="${hh}">
      ${V.img ? `<img src="${esc(V.img.url)}" alt="" draggable="false">`
              : `<div class="ph-in">${icon(V.t2i ? 'sparkle' : 'link', 22)}<b>${V.ph.t}</b><span>${V.ph.s}</span>${upto}</div>`}
      <div class="cursor" id="cursor"></div></div>`;
  }
  h += paint ? brushbarHTML() : bannerHTML();
  h += progressHTML();
  stage.innerHTML = h;
  mountToast(stage);
  const showMask = !V.empty && fits(V.mask, V.img || V.ph);
  if (showMask){ V.mask.canvas.className = 'mk'; $('#frame').insertBefore(V.mask.canvas, $('#cursor')); }
  fitFrame();
  $('#sFoot').innerHTML = footHTML(V, paint, showMask);
}

/* 运行中只更新进度数字和进度条，不重建画布 */
export function refreshProgress(pct){
  const pp = $('#pp'), pb = $('#pb');
  if (pp) pp.textContent = pct;
  if (pb) pb.style.width = pct + '%';
}

/* 图放在画布中间；顶上有横幅 / 画笔条 / 失败说明时让开，不压住图 */
export function fitFrame(){
  const f = $('#frame'), st = $('#stage'); if (!f) return;
  const pad = parseFloat(cssVar('--stage-pad')), bar = st.querySelector('.banner, .brushbar, .fault');
  const top = bar ? bar.offsetTop + bar.offsetHeight + pad / 2 : pad;
  const W = st.clientWidth - pad * 2, H = st.clientHeight - top - pad;
  const w = +f.dataset.w, h = +f.dataset.h, k = Math.min(W / w, H / h);
  f.style.width = Math.floor(w * k) + 'px'; f.style.height = Math.floor(h * k) + 'px'; f.style.top = (top + H / 2) + 'px';
}
