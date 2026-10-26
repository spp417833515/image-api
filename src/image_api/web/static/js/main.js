/* 入口：启动（读状态、历史、规划）、定时读状态、把页面事件接到各模块 */
import * as api from './api.js';
import { $, toast, autoGrow, copyText } from './dom.js';
import { icon } from './icons.js';
import { S, PREV, newStep, stepById, stepIdx, painting, touch, fail } from './store.js';
import { replan, schedulePlan, refreshStatus } from './sync.js';
import { renderAll, refreshDyn, refreshEditing, update, track, setComposing } from './render.js';
import { renderTop } from './topbar.js';
import { fitFrame, resolveView, brushDot } from './canvas.js';
import { loadHistory, loadMoreHistory, wantsMore } from './history.js';
import { select, startPaint, stopPaint, clearMask, onPointerDown, onPointerMove, onPointerUp } from './paint.js';
import { startRun } from './run.js';

const STATUS_POLL_MS = 10000;

/* 放进来的图先上传，拿到 id 再放进步骤；打不开的图由后端回 422 bad_image */
async function addFiles(sid, files, at){
  const list = [...files]; if (!list.length) return;
  if (S.job){ toast('正在出图，跑完再加图'); return; }
  const st = stepById(sid) || S.steps[0];
  const results = await Promise.allSettled(list.map(f => api.uploadImage(f)));
  const items = [];
  results.forEach(r => {
    if (r.status === 'fulfilled'){ const b = r.value; items.push({ kind:'img', id:b.id, url:b.url, width:b.width, height:b.height }); }
    else if (r.reason instanceof api.ApiError && r.reason.code === 'bad_image') toast(r.reason.message);
    else fail('上传图片', r.reason);
  });
  if (!items.length){ renderAll(); return; }
  if (S.job || !stepById(st.id)){   // 上传期间开始出图了 / 那一步被删了：图没放进去，说出来
    renderAll();
    toast(S.job ? `正在出图，刚传好的 ${items.length} 张图没放进去，跑完再放一次` : `那一步已经删了，刚传好的 ${items.length} 张图没放进去`);
    return;
  }
  touch(st);
  if (at === undefined) st.items.push(...items); else st.items.splice(at, 0, ...items);
  track(select(st.id)); update();
}

/* 「接着改」：开新一轮，把这张图带进第 1 步当图 1；之前的结果都在历史里 */
function continueFrom(x){
  const st = newStep({ items:[{ kind:'img', id:x.id, url:x.url, width:x.width, height:x.height }] });
  S.mode = 'view'; S.steps = [st]; select(st.id);
  update();   // 新步骤马上出现，规划随后到
  toast('已把这张图带进新的一步'); $(`.card[data-step="${st.id}"] textarea`).focus();
}

/* 读状态；变了就重画并重新规划（后端读不到时不规划，免得再报一遍）。
   后端从连不上 / 出错里恢复过来：重读历史第一页（之前可能没读到或已过期）。都不等规划和历史回来 */
async function syncStatus(){
  const wasDown = !S.status || !!S.statusError;
  const changed = await refreshStatus();
  const recovered = wasDown && !S.statusError;
  if (recovered) loadHistory().then(renderAll);
  if (changed || recovered){ if (S.status && !S.statusError) update(); else renderAll(); }
  return { changed, recovered };
}

async function pollStatus(){
  await syncStatus();
  setTimeout(pollStatus, STATUS_POLL_MS);
}

async function recheck(){
  const { changed, recovered } = await syncStatus();
  if (!changed && !recovered) renderAll();
  toast(S.statusError ? `${S.statusError.trouble.title}，还没好`
    : recovered ? (S.status.online ? '后端连上了' : '后端连上了，ComfyUI 还没开')
    : S.status.online ? 'ComfyUI 连上了' : '还是连不上 ComfyUI');
}

async function copyRaw(a, st){
  const wrap = a.closest('.raw-wrap'), inFault = !!a.closest('.fault');
  const ok = await copyText(wrap.querySelector('.raw').textContent);
  if (ok){ toast('原文已复制'); return; }
  const canOpen = !!wrap.querySelector('[data-act="raw-toggle"]');   // 复制不了：展开原文，方便手动选中
  if (canOpen && inFault && !S.fault.open){ S.fault.open = true; renderAll(); }
  else if (canOpen && st && !st.rawOpen){ st.rawOpen = true; renderAll(); }
  toast('复制失败，请手动选中');
}

/* 点击：按 data-act 分派。改了某一步输入的操作先让它的结果作废（touch） */
const TOUCHING = ['rm-item', 'toggle-prev', 'drop-mask', 'repaint', 'seed-rand', 'seed-clear', 'clear-size', 'preset'];
function onClick(e){
  if (S.popOpen && !e.target.closest('#pop, #status')){ S.popOpen = false; renderTop(); }
  const a = e.target.closest('[data-act]');
  const card = e.target.closest('.card'), sid = card && card.dataset.step, st = sid && stepById(sid);
  if (!a){
    if (card && !e.target.closest('textarea,input,summary,.more-body') && sid !== S.sel){ track(select(sid)); renderAll(); }
    return;
  }
  const act = a.dataset.act, idx = a.dataset.idx !== undefined ? +a.dataset.idx : null;
  if (TOUCHING.includes(act)) touch(st);
  switch (act){
    case 'ver': S.popOpen = !S.popOpen; renderTop(); return;
    case 'preset': st.width = a.dataset.w; st.height = a.dataset.h; break;
    case 'pick': S.pickFor = sid || S.sel; $('#pick').click(); return;
    case 'rm-item': st.items.splice(idx, 1); if (S.view.kind === 'item') track(select(sid)); break;
    case 'view-item': track(select(sid, { kind:'item', id:sid, idx })); break;
    case 'toggle-prev': { const k = st.items.findIndex(it => it.kind === 'prev'); if (k >= 0) st.items.splice(k, 1); else st.items.unshift(PREV()); break; }
    case 'paint': track(startPaint(st || stepById(S.view.id))); break;
    case 'drop-mask': { const s2 = st || stepById(S.view.id); if (painting(s2)) S.mode = 'view'; s2.mask = null; break; }
    case 'repaint': { const s2 = st || stepById(S.view.id); touch(s2); if (painting(s2)) S.mode = 'view'; s2.mask = null; track(startPaint(s2)); break; }   // 在现在的底图上换一张空白蒙版
    case 'clear-size': st.width = st.height = ''; break;
    case 'seed-rand': st.seed = String(Math.floor(Math.random() * 1e6)); break;
    case 'seed-clear': st.seed = ''; break;
    case 'del-step': {
      const i = stepIdx(sid); if (painting(st)) S.mode = 'view'; S.steps.splice(i, 1);
      if (S.steps[0]) S.steps[0].items = S.steps[0].items.filter(it => it.kind !== 'prev');
      track(select(stepById(S.sel) ? S.sel : S.steps[Math.max(0, i - 1)].id)); break;
    }
    case 'add-step': { const ns = newStep({ items:[PREV()] }); S.steps.push(ns); track(select(ns.id)); update();
      const ta = $(`.card[data-step="${ns.id}"] textarea`); ta.focus(); ta.scrollIntoView({ block:'nearest' }); return; }
    case 'raw-toggle': if (a.closest('.fault')) S.fault.open = !S.fault.open; else st.rawOpen = !st.rawOpen; renderAll(); return;
    case 'copy-raw': copyRaw(a, st); return;
    case 'fault-close': S.fault = null; renderAll(); return;
    case 'view-result': { const id = a.dataset.id || sid; track(select(id, { kind:'result', id })); break; }
    case 'view-hist': track(select(S.sel, { kind:'history', hid:a.dataset.hid })); break;
    case 'goto-step': { const id = a.dataset.id; track(select(id)); renderAll(); const c = $(`.card[data-step="${id}"]`); if (c) c.scrollIntoView({ block:'nearest', behavior:'smooth' }); return; }
    case 'continue': { const x = resolveView().info; if (x) continueFrom(x); return; }
    case 'gen': startRun(); return;
    case 'run-upto': { const n = +a.dataset.upto; startRun(n, n + 1); return; }
    case 'recheck': recheck(); return;
    case 'history-retry': loadHistory().then(renderAll); return;
    case 'tool-brush': S.brush.erase = false; break;
    case 'tool-erase': S.brush.erase = true; break;
    case 'mask-clear': { const s2 = stepById(S.view.id); clearMask(s2); touch(s2); break; }
    case 'mask-done': track(stopPaint(true)); renderAll(); break;   // 马上重画：上传期间「生成」立刻变灰，不等规划回来
    default: throw new Error(`click: no handler for data-act="${act}"`);
  }
  update();
}

function onInput(e){
  const t = e.target, f = t.dataset.field; if (!f) return;
  if (f === 'brush'){
    S.brush.size = +t.value; $('#bsz').textContent = t.value;
    const dot = t.closest('.size').querySelector('.pv i'), d = brushDot(S.brush.size) + 'px'; dot.style.width = dot.style.height = d;
    return;
  }
  const st = stepById(t.closest('.card').dataset.step);
  if (f === 'instruction'){ st.instruction = t.value; autoGrow(t); }
  else { st[f] = t.value.replace(/\D/g, ''); if (t.value !== st[f]) t.value = st[f]; }
  if (touch(st)) renderAll();   // 结果作废了：整页重画（焦点保留；输入法拼写中会等上屏再画），规划随后到
  else refreshEditing();        // 这一步改了：卡片显示「正在检查…」、生成先不可用，规划随后到
  schedulePlan(refreshDyn);
}

function onKey(e){
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter'){ const b = $('.gen'); if (b && !b.disabled) startRun(); }
  if (e.key === 'Escape' && S.popOpen){ S.popOpen = false; renderTop(); }
  else if (e.key === 'Escape' && S.mode === 'mask'){ track(stopPaint(false)); renderAll(); }
}

/* 拖动：图块换顺序 / 拖文件进托盘或画布 */
let dragSrc = null;
const dropTarget = e => e.target.closest && (e.target.closest('.tile') || e.target.closest('.tray') || e.target.closest('#stage'));
function onDragStart(e){
  const t = e.target.closest && e.target.closest('.tile[draggable]'); if (!t) return;
  dragSrc = { sid:t.closest('.card').dataset.step, idx:+t.dataset.idx }; t.classList.add('dragging');
  e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', 'tile');
}
function clearDrag(){ document.querySelectorAll('.dragging,.drag-over').forEach(x => x.classList.remove('dragging', 'drag-over')); }
function onDragOver(e){
  const t = dropTarget(e); if (!t) return; e.preventDefault();
  document.querySelectorAll('.drag-over').forEach(x => x !== t && x.classList.remove('drag-over')); t.classList.add('drag-over');
}
function onDrop(e){
  if (!dropTarget(e)) return; e.preventDefault(); clearDrag();
  const tile = e.target.closest('.tile'), card = e.target.closest('.card'), sid = card ? card.dataset.step : S.sel;
  const at = tile && tile.dataset.idx !== undefined ? +tile.dataset.idx : undefined;
  if (dragSrc && card){
    const src = dragSrc; dragSrc = null;
    if (S.job) return;
    const from = stepById(src.sid), to = stepById(sid); touch(from); touch(to);
    const it = from.items.splice(src.idx, 1)[0];
    to.items.splice(Math.min(at ?? to.items.length, to.items.length), 0, it);
    track(select(sid)); update(); return;
  }
  dragSrc = null;
  if (e.dataTransfer.files.length) addFiles(sid, e.dataTransfer.files, at);
}

function onFilmScroll(){ if (wantsMore()) loadMoreHistory().then(failed => { if (failed) renderAll(); }); }

function wire(){
  document.addEventListener('click', onClick);
  document.addEventListener('input', onInput);
  document.addEventListener('compositionstart', () => setComposing(true));
  document.addEventListener('compositionend', () => setComposing(false));
  document.addEventListener('toggle', e => { const d = e.target; if (d.dataset && d.dataset.more){ const st = stepById(d.dataset.more); if (st) st.moreOpen = d.open; } }, true);
  document.addEventListener('keydown', onKey);
  $('#pick').addEventListener('change', e => { addFiles(S.pickFor, e.target.files); e.target.value = ''; });
  document.addEventListener('paste', e => {
    const files = [...(e.clipboardData ? e.clipboardData.files : [])];
    if (files.length){ e.preventDefault(); addFiles(S.sel, files); }
  });
  document.addEventListener('dragstart', onDragStart);
  document.addEventListener('dragend', () => { dragSrc = null; clearDrag(); });
  document.addEventListener('dragover', onDragOver);
  document.addEventListener('drop', onDrop);
  /* 画笔：每一笔抬起后立刻重画整页（卡片上的蒙版状态跟着变） */
  document.addEventListener('pointerdown', onPointerDown);
  document.addEventListener('pointermove', onPointerMove);
  document.addEventListener('pointerup', () => { const st = onPointerUp(); if (st){ touch(st); update(); } });
  $('#film').addEventListener('scroll', onFilmScroll);
  new ResizeObserver(fitFrame).observe($('#stage'));
}

async function boot(){
  $('#logo').innerHTML = icon('sparkle', 16);
  wire();
  renderTop();
  await Promise.all([refreshStatus(), loadHistory()]);
  setTimeout(pollStatus, STATUS_POLL_MS);   // 定时读状态不等规划：规划卡住，顶栏照样跟着 ComfyUI 变
  renderAll();
  onFilmScroll();
  if (S.status) await replan();   // 先把页面画出来，规划回来再画一次
  renderAll();
}

await boot();
