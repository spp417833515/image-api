/* 页面状态 + 从状态推出来的结论。只读后端的返回值（/api/status、/api/plan、/api/jobs），这里没有路由 / 拒绝 / 尺寸规则 */
import { ApiError } from './api.js';
import { routeName } from './dom.js';

let uid = 0;
/* 一个步骤卡。items：{kind:'img', id, url, width, height} 或 {kind:'prev'}（用上一步的图）；
   mask：画板（paint.js 的 newMask）；run：GET /api/jobs/{id} 里这一步的记录 */
export const newStep = (o = {}) => ({ id:'s' + (++uid), instruction:'', items:[], mask:null, seed:'', width:'', height:'',
                                     moreOpen:false, run:null, rawOpen:false, ...o });
export const PREV = () => ({ kind:'prev' });

const first = newStep();
export const S = {
  steps:[first], sel:first.id, view:{ kind:'step', id:first.id }, mode:'view', brush:{ size:64, erase:false },
  status:null, statusError:null,   // GET /api/status 最近一次的返回 / 失败（ApiError）
  model:localStorage.getItem('image-api.model') || 'qwen-image-2.1',
  plan:{}, planError:null,         // POST /api/plan：plan[步骤 id] = {p: 这一步的规划, key: 规划依据}，见 pl()
  job:null,                        // 正在跑的任务：{segs, si, after, id, shown, failSince, fails}
  history:[], historyDone:false, historyLoading:false, historyFailed:false, fresh:new Set(),   // historyFailed：第一页没读到
  popOpen:false, pickFor:null,
  fault:null,                      // 最近一次失败的请求：{what, err:ApiError, open, title, note}
};

/* 记下失败的请求（画布顶上显示，附后端原文）。title 默认「<what>失败」，note 补一句现在怎样。
   不是 ApiError 的是页面自己的错，原样抛出 */
export function fail(what, err, { title = `${what}失败`, note = '' } = {}){
  if (!(err instanceof ApiError)) throw err;
  console.error(`${what}失败`, err.endpoint, err.status, err.code, err.message, err.raw);
  S.fault = { what, err, open:S.fault && S.fault.what === what ? S.fault.open : false, title, note };
}

/* 读完一个请求：成功了就收起之前同一件事的失败说明 */
export function clearFault(what){ if (S.fault && S.fault.what === what) S.fault = null; }

/* 当前选中的模型。状态里没有这个 id（或还没读到状态）时用列表第一项，再不行用已保存的 id */
export function currentModel(status){
  const models = status && status.models || [];
  return models.find(m => m.id === S.model) || models[0] || null;
}
export function currentModelId(){
  const model = currentModel(S.status);
  return model ? model.id : S.model;
}
/* 各路由现在能不能用：看选中的模型，没有模型列表时看默认 routes */
export function activeRoutes(status){
  const model = currentModel(status);
  return model ? model.routes : status.routes;
}
export const routesOff = status => Object.keys(activeRoutes(status)).filter(k => !activeRoutes(status)[k].available);
export const routesOn = status => Object.keys(activeRoutes(status)).filter(k => activeRoutes(status)[k].available);
/* 正在跑的那一步，和它的进度百分比（GET /api/jobs 的 progress，0–1） */
export const runningIdx = () => S.steps.findIndex(s => s.run && s.run.state === 'running');
export const pctOf = run => Math.floor((run.progress || 0) * 100);

export const stepIdx = id => S.steps.findIndex(s => s.id === id);
export const stepById = id => S.steps.find(s => s.id === id);
export const isDone = st => !!(st && st.run && st.run.state === 'done');
export const prevImage = i => i > 0 && isDone(S.steps[i - 1]) ? S.steps[i - 1].run.image : null;
/* 第 1 张是「上一步的图」，但上一步还没出图 */
export const waitsPrev = i => { const it = S.steps[i].items[0]; return !!(it && it.kind === 'prev' && i > 0 && !prevImage(i)); };
export const leadImage = i => { const it = S.steps[i].items[0]; return !it ? null : it.kind === 'prev' ? prevImage(i) : it; };
export const routeOf = i => isDone(S.steps[i]) ? S.steps[i].run.image.route : (pl(i) && pl(i).route) || null;
export const outOf = i => isDone(S.steps[i]) ? S.steps[i].run.image : (pl(i) && pl(i).output) || null;
export const painting = st => !!(st && S.mode === 'mask' && S.view && S.view.id === st.id);
export const isSoft = r => !!(r && r.code === 'empty_instruction');   // 契约：没写指令显示成灰色提示，不算红色错误
/* 第 i 步现在要显示的拒绝（来自 /api/plan）。正在涂的那一步，蒙版类结论等点「涂好了」重新规划后再说 */
export function rejectOf(st, i){ const p = pl(i), r = p && p.reject; return !r || (painting(st) && /^mask_/.test(r.code)) ? null : r; }
/* 蒙版画板和这张图一样大：才叠上去显示、才能接着改（不一样大就不拉伸，只能重新涂；拒不拒绝看 /api/plan） */
export const fits = (m, b) => !!(m && b && m.w === b.width && m.h === b.height);

/* ---------- 请求体：只发还没完成的步骤；已完成步骤的图用它的 id 引用 ---------- */
export const pendingIdx = (upto = Infinity) => S.steps.map((s, i) => i).filter(i => i <= upto && !isDone(S.steps[i]));
/* 没完成的步骤按连续段分批（每批的步号从 first_step 起连续编号） */
export function segments(upto){
  const out = [];
  pendingIdx(upto).forEach(i => { const g = out[out.length - 1]; if (g && g[g.length - 1] === i - 1) g.push(i); else out.push([i]); });
  return out;
}
/* 第 i 步的规划：只有规划依据和现在的步骤对得上才算数（改了内容、增删了步骤，就当还没规划，显示「正在检查」）。
   依据 = 这一批从开头到这一步的请求内容：步号、指令、图、蒙版、seed、宽高（「上一步的图」要看前面的步骤） */
const planBasis = (body, k) => JSON.stringify([body.first_step, body.steps.slice(0, k + 1)]);
export function pl(i){
  const st = S.steps[i], e = st && S.plan[st.id];
  if (!e) return null;
  const seg = segments().find(g => g.includes(i));
  return seg && e.key === planBasis(bodyFor(seg), seg.indexOf(i)) ? e.p : null;
}
/* 收下一批的规划。ids / body 是发请求那一刻的步骤和请求体 */
export function setPlan(ids, body, plan){
  plan.steps.forEach((p, k) => { S.plan[ids[k]] = { p, key:planBasis(body, k) }; });
}
/* 改了步骤、规划还没回来（或刚失败）：这一步正在检查 */
export const checking = i => !isDone(S.steps[i]) && !pl(i);

export function bodyFor(seg){
  return { model:currentModelId(), first_step:seg[0] + 1, steps:seg.map(i => {
    const st = S.steps[i], prev = prevImage(i);
    return { instruction:st.instruction,
             images:st.items.map(it => it.kind === 'prev' ? (prev ? prev.id : '$prev') : it.id),
             mask:st.mask && st.mask.id || null,          // 只发已经上传好的蒙版 id
             seed:st.seed ? +st.seed : null,
             width:st.width ? +st.width : null, height:st.height ? +st.height : null };
  }) };
}

/* 改了某一步的输入：这一步的结果作废；后面「用上一步的图」的步骤跟着作废（它们的蒙版留着）。返回是否有变化 */
export function touch(st){
  if (!st || S.job) return false;
  let changed = false;
  for (let j = stepIdx(st.id); j < S.steps.length; j++){
    const s = S.steps[j];
    if (j > stepIdx(st.id) && !(changed && s.items.some(it => it.kind === 'prev'))) break;
    if (s.run){ s.run = null; changed = true; }
  }
  return changed;
}

/* ---------- 「生成」跑哪几步、能不能跑 ---------- */
/* 从第一个没完成的步骤开始；某一步的蒙版是在旧图上涂的，就只跑到它前一步，出了新图先让人看一眼 */
export function runRange(){
  const k = S.steps.findIndex((st, i) => st.mask && waitsPrev(i));
  return k > 0 ? { upto:k - 1, after:k } : { upto:S.steps.length - 1, after:null };
}
const maskSending = st => !!(st.mask && st.mask.uploading);
const maskUnsent = st => !!(st.mask && !st.mask.uploading && !painting(st) && (st.mask.dirty || !st.mask.id));
/* 只看 /api/status、/api/plan 的结论和蒙版上传状态。返回 {dis, why, tone, alert} */
export function gate(upto){
  const todo = pendingIdx(upto), nums = a => a.map(i => i + 1).join('、');
  if (!todo.length) return { dis:true, why:'改哪一步，就从哪一步重跑' };
  if (S.statusError) return { dis:true, tone:'bad', alert:true, why:`${S.statusError.trouble.title}，恢复后再生成` };
  if (S.status.upgrading) return { dis:true, tone:'bad', alert:true, why:'ComfyUI 正在升级，升完再生成' };
  if (!S.status.online) return { dis:true, tone:'bad', alert:true, why:'ComfyUI 没开，打开后再生成' };
  if (todo.some(i => maskSending(S.steps[i]))) return { dis:true, why:'蒙版上传中，传完就能生成' };
  const unsent = todo.filter(i => maskUnsent(S.steps[i]));
  if (unsent.length) return { dis:true, tone:'bad', alert:true, why:`第 ${nums(unsent)} 步的蒙版没传上，点「改」再点「涂好了」重传` };
  if (S.planError) return { dis:true, tone:'bad', alert:true, why:`检查步骤出错：${S.planError.message}` };
  if (todo.some(i => !pl(i))) return { dis:true, why:'正在检查步骤…' };   // 改了步骤、规划还没回来
  const hard = todo.filter(i => { const r = rejectOf(S.steps[i], i); return r && !isSoft(r); });
  if (hard.length) return { dis:true, tone:'bad', alert:true, why:`第 ${nums(hard)} 步要改一下，改好就能生成` };
  const un = todo.filter(i => pl(i).unavailable);
  if (un.length) return { dis:true, tone:'bad', alert:true, why:`第 ${nums(un)} 步要用的${routeName(pl(un[0]).route)}现在用不了` };
  const soft = todo.filter(i => isSoft(rejectOf(S.steps[i], i)));
  if (soft.length) return { dis:true, why:S.steps.length > 1 ? `第 ${nums(soft)} 步还没写指令` : '写一句指令就能生成' };
  return { dis:false };
}
