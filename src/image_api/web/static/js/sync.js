/* 和后端保持同步：规划（POST /api/plan）、状态（GET /api/status）。只更新 S，不重画 */
import * as api from './api.js';
import { S, segments, bodyFor, setPlan, fail, clearFault } from './store.js';

const PLAN_DEBOUNCE_MS = 250;
let planSeq = 0, planTimer = null;

/* 按现在的步骤重新规划。比后发的请求晚回来的旧结果丢掉（序号）；收下的规划按步骤 id 和规划依据存（见 store.pl）。
   失败时规划清空、生成按钮不可用，原因和原文走失败说明卡 */
const PLAN_FAULT = '检查步骤';
export async function replan(){
  clearTimeout(planTimer); planTimer = null;
  const seq = ++planSeq;
  const batches = segments().map(seg => ({ ids:seg.map(i => S.steps[i].id), body:bodyFor(seg) }));   // 发请求那一刻的步骤
  try {
    const res = await Promise.all(batches.map(b => api.planSteps(b.body)));
    if (seq !== planSeq) return;
    S.plan = {}; S.planError = null; clearFault(PLAN_FAULT);
    res.forEach((plan, k) => setPlan(batches[k].ids, batches[k].body, plan));
  } catch (err){
    if (!(err instanceof api.ApiError)) throw err;
    if (seq !== planSeq) return;
    S.plan = {}; S.planError = err; fail(PLAN_FAULT, err);
  }
}

/* 打字时：停手约 250 ms 再规划，然后调 then */
export function schedulePlan(then){
  clearTimeout(planTimer);
  planTimer = setTimeout(() => replan().then(then), PLAN_DEBOUNCE_MS);
}

/* 读一次状态。返回状态有没有变（变了要重新规划、重画） */
export async function refreshStatus(){
  const before = JSON.stringify([S.status, S.statusError && S.statusError.message]);
  try { S.status = await api.getStatus(); S.statusError = null; }
  catch (err){
    if (!(err instanceof api.ApiError)) throw err;
    console.error('GET /api/status 失败', err.status, err.code, err.message, err.raw);
    S.statusError = err;
  }
  return JSON.stringify([S.status, S.statusError && S.statusError.message]) !== before;
}
