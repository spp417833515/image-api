/* 生成：POST /api/generate 拿 job_id，再轮询 GET /api/jobs/{id}。
   upto / after 给「只跑到前面某一步」用：「先出第 n 步的图」和「蒙版是在旧图上涂的」。
   点「生成」从不等什么：规划或蒙版还没好就说一声、让人好了再点（不排队，免得卡住后来的点击，或者过时的点击晚些自己跑起来）。
   任务进行中也不等规划和历史：先画、后台再规划，规划或历史卡住都不挡任务进度 */
import * as api from './api.js';
import { toast } from './dom.js';
import { S, isDone, pl, segments, bodyFor, runRange, gate, fail, clearFault, runningIdx, pendingIdx, setPlan } from './store.js';
import { replan, refreshStatus } from './sync.js';
import { renderAll, updateProgress, update, track } from './render.js';
import { select, startPaint, stopPaint } from './paint.js';
import { refreshHistoryHead } from './history.js';

const POLL_MS = 600;
/* 查进度失败时后端可能还在跑这个任务：接着跟、退避重试（0.6 → 1 → 2 → 5 秒，之后一直 5 秒），连续 60 秒都不行才放手 */
const RETRY_MS = [600, 1000, 2000, 5000];
const GIVE_UP_MS = 60e3;
const retryable = err => err.status === 0 || err.status >= 500 || err.status === 408 || err.status === 429;   // 网络、5xx、超时、限流：等等可能就好
const POLL_FAULT = '查进度';

export function startRun(upto, after){
  if (S.job) return;   // 任务进行中按钮不可用；这里只挡同一瞬间的连点
  if (upto === undefined) ({ upto, after } = runRange());
  const upload = S.mode === 'mask' ? stopPaint(true) : null;   // 涂到一半直接点生成：先当「涂好了」
  if (upload){ track(upload); update(); toast('蒙版上传中，传完再点「生成」'); return; }   // 传完、规划好了再点
  if (pendingIdx(upto).some(i => !pl(i))){   // 规划和现在的步骤对不上：重新规划，好了再点
    update(); toast('正在检查步骤，好了再点「生成」'); return;
  }
  const g = gate(upto);
  if (g.dis){ renderAll(); if (g.why) toast(g.why); return; }
  S.job = { segs:segments(upto), si:0, after:after === undefined ? null : after, id:null, shown:false, failSince:null, fails:0 };
  S.fresh = new Set();
  renderAll();
  sendSeg();
}

async function sendSeg(){
  const J = S.job, seg = J.segs[J.si], ids = seg.map(i => S.steps[i].id), body = bodyFor(seg);
  let res;
  try { res = await api.generate(body); }
  catch (err){
    if (!(err instanceof api.ApiError)) throw err;
    if (S.job !== J) return;
    S.job = null;
    if (err.status === 422 && err.code === 'rejected' && err.plan){   // 每步的原因显示在卡片上，提示条只说一句
      setPlan(ids, body, err.plan); renderAll(); toast(err.message); return;
    }
    fail('生成', err);   // 原因和原文在失败说明卡上，不再用提示条重复
    if (err.status === 503) await refreshStatus();
    update(); return;
  }
  if (S.job !== J) return;
  J.id = res.job_id; J.shown = false;
  poll();
}

/* 不再跟踪这个任务：说清楚状态不明、去历史里看；已经出的图在历史里 */
function abandon(J, err, note){
  S.job = null;
  S.steps.forEach(st => { if (st.run && (st.run.state === 'queued' || st.run.state === 'running')) st.run = null; });
  fail(POLL_FAULT, err, { title:'任务状态不明', note });
  update();
}

/* 查进度失败：能等的接着跟、退避重试，画布上说「进度暂时读不到」并附原文，生成仍不可用；
   连续 60 秒都不行，或者这种错误重试也没用（4xx、返回的数据不对），就不再跟踪 */
function pollFailed(J, err){
  if (!(err instanceof api.ApiError)) throw err;   // 页面自己的错：照样抛出
  if (S.job !== J) return;
  const now = Date.now();
  if (J.failSince === null) J.failSince = now;
  J.fails += 1;
  const waited = now - J.failSince;
  if (!retryable(err)) return abandon(J, err, '这种错误重试也没用，不再跟踪；后端可能还在跑，出了图会在历史里');
  if (waited >= GIVE_UP_MS) return abandon(J, err, `连续 ${Math.floor(waited / 1000)} 秒读不到进度，不再跟踪；后端可能还在跑，出了图会在历史里`);
  const wait = RETRY_MS[Math.min(J.fails, RETRY_MS.length) - 1];
  fail(POLL_FAULT, err, { title:'进度暂时读不到', note:`任务还在后端跑，${wait / 1000} 秒后重试（第 ${J.fails} 次）` });
  renderAll();
  setTimeout(poll, wait);
}

async function poll(){
  const J = S.job; if (!J || !J.id) return;
  let job;
  try { job = await api.getJob(J.id, J.segs[J.si].map(i => i + 1)); }   // api 已核对：字段齐全、步号正好是这批
  catch (err){ return pollFailed(J, err); }
  if (S.job !== J) return;
  const recovered = J.failSince !== null;
  if (recovered){ J.failSince = null; J.fails = 0; clearFault(POLL_FAULT); }   // 又读到了
  const fresh = [];
  job.steps.forEach(js => {
    const st = S.steps[js.step - 1];
    if (!st) throw new Error(`poll: job step ${js.step} has no step card; the page changed its steps during a job (${S.steps.length} cards)`);
    if (js.state === 'done' && !isDone(st)) fresh.push(js.image.id);
    st.run = js;
  });
  if (fresh.length){ fresh.forEach(id => S.fresh.add(id)); refreshHistoryHead().then(renderAll); }   // 新图放到历史最前面（不等它）
  if (job.state === 'queued' || job.state === 'running'){
    const ri = runningIdx();
    if (!J.shown || recovered || fresh.length || (ri >= 0 && S.steps[ri].id !== S.sel)){
      J.shown = true; if (ri >= 0) track(select(S.steps[ri].id));
      update();
    } else updateProgress();
    setTimeout(poll, POLL_MS); return;
  }
  if (job.state === 'done' && J.si + 1 < J.segs.length){ J.si++; update(); sendSeg(); return; }
  S.job = null;
  if (job.state === 'failed'){
    /* 某步失败：停下，前面出的图保留；选中失败的那步，画布先看前面最后一张图 */
    const f = job.steps.find(x => x.state === 'failed');   // shapes：failed 的任务必有一步 failed 且带原文
    const fi = f.step - 1, good = S.steps.map((s, k) => k).filter(k => k < fi && isDone(S.steps[k])).pop();
    track(select(S.steps[fi].id, good !== undefined ? { kind:'result', id:S.steps[good].id } : undefined));
    update(); toast(`第 ${f.step} 步失败`); return;
  }
  const nx = J.after !== null && S.steps[J.after];
  if (nx && nx.mask){         // 蒙版是在旧图上涂的：切过去，旧蒙版叠在新图上，先看一眼；尺寸变了就得重新涂（要等规划才知道）
    track(select(nx.id, { kind:'item', id:nx.id, idx:0 })); renderAll();
    await replan(); renderAll();
    const p = pl(J.after), mis = !!(p && p.reject && p.reject.code === 'mask_size_mismatch');
    toast(mis ? '新图尺寸变了，蒙版要重新涂' : '新图出来了，蒙版还是之前涂的，看一眼再接着跑'); return;
  }
  if (nx){ track(startPaint(nx)); update(); toast(`第 ${J.after} 步出图了，涂要重画的地方`); return; }   // 「先出第 n 步的图」：切到下一步，直接能涂
  const last = S.steps[job.steps[job.steps.length - 1].step - 1];
  track(select(last.id, { kind:'result', id:last.id })); update(); toast('出图了');
}
