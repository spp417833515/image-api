/* 左栏：步骤卡（指令、图块托盘、蒙版控件、拒绝 / 失败消息、「更多」） */
import { $, esc, bgImage, cssVar, autoGrow, getImg, routeName } from './dom.js';
import { icon } from './icons.js';
import { S, pl, prevImage, waitsPrev, leadImage, routeOf, outOf, painting, isSoft, rejectOf, fits, stepIdx, pctOf, checking } from './store.js';
import { rawHTML } from './raw.js';

/* 整个 ComfyUI 的问题：顶栏、横幅、生成按钮说，不在每张卡上重复 */
const GLOBAL_UNAV = ['comfy_down', 'upgrading'];
/* 拒绝原因 → 修复按钮：页面只按 reject.code 选按钮（没列出的 code 没有按钮） */
const FIX = { mask_without_image:['drop-mask', '去掉蒙版'], size_with_images:['clear-size', '清掉宽高'], mask_empty:['paint', '接着涂'],
              mask_size_mismatch:['repaint', '重新涂'], prev_on_first_step:['toggle-prev', '不用上一步的图'],
              size_out_of_range:['clear-size', '清掉宽高'], seed_out_of_range:['seed-clear', '清空 seed'] };

function cardClass(st, i){
  const r = st.run, p = pl(i), rej = rejectOf(st, i);
  const bad = (r && r.state === 'failed') || (rej && !isSoft(rej)) || (p && p.unavailable && !GLOBAL_UNAV.includes(p.unavailable.code));
  return ['card', S.sel === st.id ? 'sel' : '', bad ? 'bad' : '', r && r.state === 'running' ? 'running' : '', S.job ? 'locked' : ''].join(' ');
}

export function renderSteps(){
  if (!S.status){ $('#stepCount').textContent = ''; $('#steps').innerHTML = ''; return; }   // 还没读到状态（limits 等），见画布上的说明
  $('#stepCount').textContent = S.steps.length + ' 步';
  let h = '';
  S.steps.forEach((st, i) => {
    if (i > 0) h += st.items.some(it => it.kind === 'prev')
      ? `<div class="link-line"><span>${icon('link', 12)}接上一步的图</span></div>` : `<div class="gap"></div>`;
    h += cardHTML(st, i);
  });
  if (!S.job) h += `<button class="add-step" data-act="add-step">${icon('plus', 16)}下一步</button>`;
  $('#steps').innerHTML = h;
  document.querySelectorAll('.c-prompt').forEach(autoGrow);
  drawMaskThumbs();
}

/* 打字时只刷新会变的部分，不重建输入框（保持焦点） */
export function refreshCards(){
  S.steps.forEach((st, i) => {
    const el = $(`.card[data-step="${st.id}"]`); if (!el) return;
    el.className = cardClass(st, i);
    $('.c-head', el).innerHTML = headHTML(st, i);
    $('.c-tray', el).innerHTML = trayHTML(st, i);
    $('.c-tools', el).innerHTML = toolsHTML(st, i);
    $('.c-msg', el).innerHTML = msgHTML(st, i);
    $('.more-sum', el).textContent = moreSum(st, i);
    $('.round-hint', el).textContent = roundHint(i);
    el.querySelectorAll('.f-size').forEach(f => { f.hidden = !showSize(st); });
    el.querySelectorAll('[data-act="preset"]').forEach(b => b.classList.toggle('on', presetOn(st, b.dataset.w, b.dataset.h)));
  });
  drawMaskThumbs();
}

/* 进度变化时只换掉这一步的卡头 */
export function refreshHead(i){
  const st = S.steps[i], el = st && $(`.card[data-step="${st.id}"] .c-head`);
  if (el) el.innerHTML = headHTML(st, i);
}

function cardHTML(st, i){
  return `<section class="${cardClass(st, i)}" data-step="${st.id}">
    <header class="c-head">${headHTML(st, i)}</header>
    <div class="c-body">
      <textarea class="c-prompt" data-field="instruction" rows="2" placeholder="${i === 0 ? '想要什么？比如：画一只坐在雪地里的赤狐' : '这一步怎么改？比如：把狐狸换成北极狐'}">${esc(st.instruction)}</textarea>
      <div class="c-tray">${trayHTML(st, i)}</div>
      <div class="c-tools">${toolsHTML(st, i)}</div>
    </div>
    <div class="c-msg">${msgHTML(st, i)}</div>
    ${moreHTML(st, i)}
  </section>`;
}

function headHTML(st, i){
  const r = st.run, rej = rejectOf(st, i), rt = routeOf(i), out = outOf(i);
  /* route 为 null（被拒）时不显示路由名：没写指令是灰色「待写指令」，其它是红色「要改一下」 */
  const tag = rej ? (isSoft(rej) ? `<span class="tag mute">待写指令</span>` : `<span class="tag bad">要改一下</span>`)
            : rt ? `<span class="tag ${rt}">${routeName(rt)}</span>`
            : checking(i) && !S.planError ? `<span class="tag mute">正在检查…</span>` : '';
  const size = !rej && out ? `<span class="c-size">${out.width}×${out.height}</span>` : '';
  let stt = '';
  if (r){
    if (r.state === 'running') stt = `<span class="st run"><i class="spin"></i>进行中 ${pctOf(r)}%</span>`;
    else if (r.state === 'queued') stt = `<span class="st">排队中</span>`;
    else if (r.state === 'done') stt = `<span class="st ok">${icon('check', 14)}</span><img class="st-thumb" src="${esc(r.image.url)}" data-act="view-result" title="看这一步的图" alt="">`;
    else if (r.state === 'failed') stt = `<span class="st fail">${icon('alert', 14)}失败</span>`;
    else if (r.state === 'skipped') stt = `<span class="st">没跑</span>`;
  }
  const del = S.steps.length > 1 && !S.job ? `<button class="ib" data-act="del-step" title="删掉这一步">${icon('x', 14)}</button>` : '';
  return `<span class="c-no">${i + 1}</span>${tag}${size}<span class="c-sp"></span>${stt}${del}`;
}

function trayHTML(st, i){
  const max = S.status.limits.max_images, n = st.items.length, cols = Math.max(max, n);
  const rej = rejectOf(st, i), tooMany = !!(rej && rej.code === 'too_many_images'), isMask = routeOf(i) === 'mask';
  let t = '';
  st.items.forEach((it, k) => {
    const cls = [k >= max && tooMany ? 'over' : '', k === 0 ? 'lead' : '',
                 S.view && S.view.kind === 'item' && S.view.id === st.id && S.view.idx === k ? 'viewing' : ''].join(' ');
    const badge = k === 0 && isMask ? '底图' : String(k + 1);
    if (it.kind === 'prev'){
      const r = prevImage(i);
      t += `<div class="tile prev ${r ? 'has' : ''} ${cls}" draggable="true" data-act="view-item" data-idx="${k}" ${r ? `style="${bgImage(r.url)}"` : ''} title="上一步出的图">
        <span class="t-badge">${badge}</span><span class="t-prev">${icon('link', r ? 11 : 16)}上一步</span>
        <button class="t-x" data-act="rm-item" data-idx="${k}" title="不用上一步的图">${icon('x', 11)}</button></div>`;
    } else {
      t += `<div class="tile ${cls}" draggable="true" data-act="view-item" data-idx="${k}" style="${bgImage(it.url)}" title="图 ${k + 1}">
        <span class="t-badge">${badge}</span>
        <button class="t-x" data-act="rm-item" data-idx="${k}" title="去掉这张">${icon('x', 11)}</button></div>`;
    }
  });
  for (let k = n; k < max; k++)
    t += k === n ? `<button class="tile empty first" data-act="pick" title="点击选图，也可以拖进来或粘贴">${icon('plus', 16)}<span>加图</span></button>`
                 : `<button class="tile empty ghost" data-act="pick" tabindex="-1"></button>`;
  return `<div class="tray-lab"><span>图片 · 拖动换顺序</span><span class="${tooMany ? 'over' : ''}">${n}/${max}</span></div>
    <div class="tray ${n ? '' : 'is-empty'}" style="--cols:${cols}" data-drop="1">${t}</div>`;
}

function toolsHTML(st, i){
  let h = '';
  if (i > 0){
    const on = st.items.some(it => it.kind === 'prev');
    h += `<button class="chip ${on ? 'on' : ''}" data-act="toggle-prev">${icon('link', 14)}用上一步的图</button>`;
  }
  if (st.mask){
    const rej = rejectOf(st, i), m = st.mask, canEdit = fits(m, leadImage(i)) && !painting(st);
    const lab = painting(st) ? ['run', '涂蒙版中']
      : m.uploading ? ['mute', '上传中…']
      : waitsPrev(i) ? ['warn', '蒙版是在旧图上涂的']
      : m.dirty || !m.id ? ['bad', '蒙版没传上']
      : rej && /^mask_/.test(rej.code) ? ['bad', '蒙版要改'] : ['', '已涂蒙版'];
    h += `<div class="mask-ctl"><canvas class="m-thumb" width="60" height="60" data-mthumb="${st.id}" ${canEdit ? 'data-act="paint" title="看 / 改蒙版"' : ''}></canvas>
      <span class="lab ${lab[0]}">${lab[1]}</span>${canEdit ? `<button class="chip" data-act="paint">改</button>` : ''}<button class="chip" data-act="drop-mask">去掉</button></div>`;
  } else if (st.items.length){
    /* 第 1 张是「上一步的图」但上一步还没出图：蒙版没处涂，先只跑到上一步 */
    if (waitsPrev(i)){ if (!S.job) h += `<div class="mask-ctl"><button class="chip" data-act="run-upto" data-upto="${i - 1}" title="只跑到第 ${i} 步，出了图再在上面涂蒙版">${icon('play', 13)}先出第 ${i} 步的图</button></div>`; }
    else h += `<div class="mask-ctl"><button class="chip" data-act="paint">${icon('brush', 14)}涂蒙版</button></div>`;
  }
  return h;
}

function msgHTML(st, i){
  const r = st.run, p = pl(i), rej = rejectOf(st, i);
  if (r && r.state === 'failed')
    return `<div class="msg">${icon('alert', 15)}<div class="txt">第 ${i + 1} 步失败${rawHTML(r.error.raw, st.rawOpen, 'ComfyUI 原文', true)}</div></div>`;
  if (rej && !isSoft(rej)){
    const f = FIX[rej.code];
    const btn = !f ? '' : f[0] === 'repaint' && !leadImage(i)        // 新底图还没出：先出图，再重新涂
      ? (S.job ? '' : ` <button class="linkbtn" data-act="run-upto" data-upto="${i - 1}">先出第 ${i} 步的图</button>`)
      : ` <button class="linkbtn" data-act="${f[0]}">${f[1]}</button>`;
    return `<div class="msg">${icon('alert', 15)}<div class="txt">${esc(rej.message)}${btn}</div></div>`;
  }
  if (p && p.unavailable && !GLOBAL_UNAV.includes(p.unavailable.code))
    return `<div class="msg">${icon('alert', 15)}<div class="txt">${routeName(p.route)}现在用不了${p.route === 'mask' ? ` <button class="linkbtn" data-act="drop-mask">去掉蒙版</button>` : ''}${rawHTML(p.unavailable.message, true, '后端原文', false)}</div></div>`;
  return '';
}

/* 宽高只给文生图：没放图时出现；带图但已经填了宽高的留着显示，好让人看到再清掉 */
const showSize = st => !st.items.length || !!st.width || !!st.height;
function presetOn(st, w, h){
  const d = S.status.limits.default_size;
  return st.width || st.height ? +st.width === +w && +st.height === +h : +w === d.width && +h === d.height;   // 没填宽高 = 默认尺寸
}
function moreSum(st, i){
  const seed = st.seed ? 'seed ' + st.seed : 'seed 随机', o = outOf(i);
  return o && !rejectOf(st, i) ? `${seed} · ${o.width}×${o.height}` : seed;
}
function roundHint(i){ const p = pl(i); return p && p.requested && p.output ? `→ 实际 ${p.output.width}×${p.output.height}` : ''; }
function moreHTML(st, i){
  const d = S.status.limits.default_size, sz = showSize(st) ? '' : 'hidden';
  return `<details class="more" data-more="${st.id}" ${st.moreOpen ? 'open' : ''}>
    <summary>${icon('right', 12)}<b>更多</b><span class="more-sum">${moreSum(st, i)}</span></summary>
    <div class="more-body">
      <label class="f"><span>seed</span><input data-field="seed" placeholder="随机" inputmode="numeric" value="${esc(st.seed)}">
        <button class="ib" data-act="seed-rand" title="随机一个">${icon('dice', 15)}</button><button class="ib" data-act="seed-clear" title="清空（每次随机）">${icon('x', 13)}</button></label>
      <div class="f f-size" ${sz}><span>比例</span>${S.status.size_presets.map(z => `<button class="chip sm ${presetOn(st, z.width, z.height) ? 'on' : ''}" data-act="preset" data-w="${z.width}" data-h="${z.height}" title="${z.width}×${z.height}">${esc(z.label)}</button>`).join('')}</div>
      <label class="f f-size" ${sz}><span>宽 × 高</span><input class="sz" data-field="width" placeholder="${d.width}" inputmode="numeric" value="${esc(st.width)}"><span class="x">×</span><input class="sz" data-field="height" placeholder="${d.height}" inputmode="numeric" value="${esc(st.height)}"><em class="round-hint">${roundHint(i)}</em></label>
    </div></details>`;
}

/* 卡片上的蒙版缩略图：底图（尺寸对得上时）+ 半透明蒙版 */
function drawMaskThumbs(){
  document.querySelectorAll('canvas[data-mthumb]').forEach(c => {
    const i = stepIdx(c.dataset.mthumb), st = S.steps[i]; if (!st || !st.mask) return;
    const x = c.getContext('2d'), W = c.width, H = c.height, m = st.mask, b = leadImage(i);
    x.clearRect(0, 0, W, H); x.fillStyle = cssVar('--well'); x.fillRect(0, 0, W, H);
    const k = Math.min(W / m.w, H / m.h), dw = m.w * k, dh = m.h * k, ox = (W - dw) / 2, oy = (H - dh) / 2;
    const paint = () => { x.globalAlpha = .75; x.drawImage(m.canvas, ox, oy, dw, dh); x.globalAlpha = 1; };
    if (fits(m, b)) getImg(b.url, im => { x.drawImage(im, ox, oy, dw, dh); paint(); });
    else { x.fillStyle = cssVar('--card-3'); x.fillRect(ox, oy, dw, dh); paint(); }
  });
}
