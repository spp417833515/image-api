/* 蒙版画板：每步一张离屏画布，涂过的地方 = 不透明的蒙版色。
   id = 最近一次上传（POST /api/images）拿到的图片 id；dirty = 上传后又涂过；uploading = 最后一次上传还没落地；
   rev = 涂了几笔（判断上传期间有没有再涂）；seq = 第几次上传（只认最后一次） */
import * as api from './api.js';
import { $, cssVar } from './dom.js';
import { S, stepById, stepIdx, leadImage, fits, fail } from './store.js';

export function newMask(w, h){
  const c = document.createElement('canvas'); c.width = w; c.height = h;
  return { canvas:c, w, h, id:null, dirty:false, uploading:false, rev:0, seq:0 };
}

/* 画板 → 黑白 PNG（白 = 重画，黑 = 不动） */
function maskPng(m){
  const c = document.createElement('canvas'); c.width = m.w; c.height = m.h;
  const x = c.getContext('2d');
  x.drawImage(m.canvas, 0, 0);
  x.globalCompositeOperation = 'source-in'; x.fillStyle = cssVar('--mask-on'); x.fillRect(0, 0, m.w, m.h);
  x.globalCompositeOperation = 'destination-over'; x.fillStyle = cssVar('--mask-off'); x.fillRect(0, 0, m.w, m.h);
  return new Promise((ok, no) => c.toBlob(b => b ? ok(b) : no(new Error('mask canvas.toBlob returned null')), 'image/png'));
}

/* 切换选中的步骤 / 画布内容。离开正在涂的底图就先结束涂；返回蒙版上传的 Promise（没上传就是 null） */
export function select(id, view){
  const v = view || { kind:'step', id };
  const upload = S.mode === 'mask' && !(v.kind === 'item' && v.idx === 0 && v.id === S.view.id) ? stopPaint(false) : null;
  S.sel = id; S.view = v;
  return upload;
}

/* 开始涂：蒙版只能涂在已经有的图上（没有图时入口不会出现）。返回值同 select */
export function startPaint(st){
  if (!st) return null;
  const b = leadImage(stepIdx(st.id)); if (!b) return null;
  const upload = select(st.id, { kind:'item', id:st.id, idx:0 });
  if (!fits(st.mask, b)) st.mask = newMask(b.width, b.height);   // 新蒙版（或尺寸对不上时换一张空白的）先不上传、不规划，点「涂好了」再说
  S.mode = 'mask'; S.brush.erase = false;
  return upload;
}

/* 结束涂：explicit = 点了「涂好了」→ 导出上传（空的由 /api/plan 回 mask_empty）。
   其它方式离开（切到别处、Esc）：一笔没涂的新蒙版直接丢掉，涂过的照样上传。返回上传的 Promise 或 null */
export function stopPaint(explicit){
  const st = stepById(S.view.id), m = st && st.mask; S.mode = 'view';
  if (!m) return null;
  if (!m.id && !m.dirty && !explicit){ st.mask = null; return null; }
  if (m.id && !m.dirty) return null;
  return upload(st, m);
}

/* 上传期间 uploading = true：生成按钮不可用，直到拿到新蒙版的 id。
   同一张蒙版可能连着传几次（涂好了 → 改 → 涂好了），回来的先后不定：只认最后一次，它落地前一直算上传中 */
async function upload(st, m){
  const rev = m.rev, seq = ++m.seq; m.uploading = true;
  let img = null, error = null;
  try { img = await api.uploadImage(await maskPng(m), 'mask.png'); }
  catch (err){
    if (!(err instanceof api.ApiError)){ if (seq === m.seq) m.uploading = false; throw err; }   // 页面自己的错：照样抛出
    error = err;
  }
  if (seq !== m.seq){   // 后面又传过：这次的结果作废（不覆盖新 id，也不报它的错）
    console.warn(`蒙版第 ${seq} 次上传的结果作废，以第 ${m.seq} 次为准：`, img ? img.id : `${error.code} ${error.message}`);
    return;
  }
  m.uploading = false;
  if (error) return fail('上传蒙版', error);
  if (st.mask === m){ m.id = img.id; m.dirty = m.rev !== rev; }
}

export function clearMask(st){
  if (!st || !st.mask) return;
  st.mask.canvas.getContext('2d').clearRect(0, 0, st.mask.w, st.mask.h);
  st.mask.dirty = true; st.mask.rev++;
}

/* ---------- 画笔 ---------- */
let drawing = null;
function maskPoint(e){
  const st = stepById(S.view.id), c = st.mask.canvas, r = c.getBoundingClientRect();
  return { x:(e.clientX - r.left) / r.width * c.width, y:(e.clientY - r.top) / r.height * c.height, st, c };
}
function strokeTo(p, from){
  const x = p.c.getContext('2d');
  x.globalCompositeOperation = S.brush.erase ? 'destination-out' : 'source-over';
  x.strokeStyle = cssVar('--mask'); x.lineWidth = S.brush.size; x.lineCap = x.lineJoin = 'round';
  x.beginPath(); x.moveTo(from.x, from.y); x.lineTo(p.x, p.y); x.stroke();
  x.globalCompositeOperation = 'source-over';
  p.st.mask.dirty = true; p.st.mask.rev++;
}
function moveCursor(e){
  const cur = $('#cursor'), f = $('#frame'); if (!cur || !f || S.mode !== 'mask') return;
  const r = f.getBoundingClientRect(), d = S.brush.size * r.width / (+f.dataset.w);
  cur.style.display = 'block'; cur.style.width = cur.style.height = d + 'px';
  cur.style.left = (e.clientX - r.left) + 'px'; cur.style.top = (e.clientY - r.top) + 'px';
}
export function onPointerDown(e){
  if (S.mode !== 'mask' || !e.target.closest('#frame')) return;
  e.preventDefault(); const p = maskPoint(e); drawing = p; strokeTo(p, p);
  if (e.target.setPointerCapture) e.target.setPointerCapture(e.pointerId);
}
export function onPointerMove(e){ moveCursor(e); if (!drawing) return; const p = maskPoint(e); strokeTo(p, drawing); drawing = p; }
/* 抬笔：返回刚涂完的那一步（没在涂就是 null） */
export function onPointerUp(){ if (!drawing) return null; const st = drawing.st; drawing = null; return st; }
