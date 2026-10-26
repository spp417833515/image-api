/* 后端 HTTP 接口（契约 v1）：页面里唯一调用 fetch 的地方，一个函数对应一个接口。
   成功的响应先按 shapes.js 检查页面用到的字段；非 2xx、网络不通、不是 JSON、字段不对，一律抛 ApiError，
   带上后端给的 status / code / message / raw / plan（raw 只放后端原样返回的内容）。
   图片字节（GET /api/images/{id}）不走 fetch：<img> 和下载链接直接用图片记录里的 url。 */
import { SHAPES, problem } from './shapes.js';

const TROUBLE = { down:{ title:'连不上后端服务', tag:'连不上' }, error:{ title:'后端出错', tag:'出错' } };

export class ApiError extends Error {
  constructor({ endpoint, status, code, message, raw = null, plan = null }){
    super(message);
    this.name = 'ApiError';
    Object.assign(this, { endpoint, status, code, raw, plan });
  }
  /* 后端连不上（网络不通）还是出错（回了错误码或坏数据） */
  get trouble(){ return TROUBLE[this.status ? 'error' : 'down']; }
  /* 哪个请求 + HTTP 状态：界面上只在一处显示 */
  get source(){ return this.status ? `${this.endpoint} · HTTP ${this.status}` : this.endpoint; }
}

const badResponse = (endpoint, status, what, raw) =>
  new ApiError({ endpoint, status, code:'bad_response', message:`后端返回的数据不对：${what}`, raw });

/* 契约里的错误体：{code, message, raw?, plan?} */
const isContractError = b => !!b && typeof b === 'object' && typeof b.code === 'string' && typeof b.message === 'string';

async function request(method, path, { json, form, shape, endpoint = `${method} ${path.split('?')[0]}` }){
  const init = { method, headers:{ Accept:'application/json' } };
  if (json !== undefined){ init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(json); }
  if (form !== undefined) init.body = form;
  let res;
  try { res = await fetch(path, init); }
  catch (err){ throw new ApiError({ endpoint, status:0, code:'network', message:TROUBLE.down.title, raw:String(err) }); }
  const text = await res.text();
  let body;
  try { body = text ? JSON.parse(text) : undefined; }
  catch { body = undefined; }
  if (res.ok){
    if (body === undefined) throw badResponse(endpoint, res.status, '不是 JSON', text);
    const p = problem(body, shape, '响应');
    if (p) throw badResponse(endpoint, res.status, p, text);
    return body;
  }
  if (isContractError(body)){
    const plan = body.code === 'rejected' ? body.plan : null;
    const p = body.code === 'rejected' ? problem(plan, SHAPES.plan, '响应.plan') : null;
    if (p) throw badResponse(endpoint, res.status, p, text);
    const raw = typeof body.raw === 'string' ? body.raw : body.raw == null ? null : JSON.stringify(body.raw);
    throw new ApiError({ endpoint, status:res.status, code:body.code, message:body.message, raw, plan });
  }
  throw new ApiError({ endpoint, status:res.status, code:`http_${res.status}`, message:TROUBLE.error.title, raw:text || null });
}

/* plan / job 里的步号必须正好是这批请求的步号（页面按步号对回卡片） */
function sameSteps(endpoint, status, got, want){
  const nums = got.steps.map(s => s.step);
  if (nums.length !== want.length || nums.some((n, k) => n !== want[k]))
    throw badResponse(endpoint, status, `步号是 [${nums}]，这批请求的是 [${want}]`, JSON.stringify(got));
}
const batchSteps = body => body.steps.map((_, k) => body.first_step + k);

/* GET /api/status → 在线、版本、检查结果、各路由能不能用、limits、size_presets */
export const getStatus = () => request('GET', '/api/status', { shape:SHAPES.status });

/* GET /api/history?limit=&before= → {items}（新的在前） */
export function getHistory(limit, before = null){
  const q = new URLSearchParams({ limit:String(limit) });
  if (before) q.set('before', before);
  return request('GET', `/api/history?${q}`, { shape:SHAPES.history });
}

/* POST /api/images（multipart file）→ 201 {id, url, width, height}；422 bad_image */
export function uploadImage(blob, filename){
  const form = new FormData();
  if (filename) form.append('file', blob, filename); else form.append('file', blob);
  return request('POST', '/api/images', { form, shape:SHAPES.upload });
}

/* POST /api/plan → {ok, steps:[{step, route, output, requested, reject, unavailable}]} */
export async function planSteps(body){
  const plan = await request('POST', '/api/plan', { json:body, shape:SHAPES.plan });
  sameSteps('POST /api/plan', 200, plan, batchSteps(body));
  return plan;
}

/* POST /api/generate → 202 {job_id}；422 {code:'rejected', message, plan}；503 {code, message, raw} */
export async function generate(body){
  try { return await request('POST', '/api/generate', { json:body, shape:SHAPES.generate }); }
  catch (err){
    if (err instanceof ApiError && err.code === 'rejected') sameSteps('POST /api/generate', err.status, err.plan, batchSteps(body));
    throw err;
  }
}

/* GET /api/jobs/{id} → {id, state, first_step, steps:[{step, route, state, progress, image, error}]}；steps 要正好是这批的步号 */
export async function getJob(id, steps){
  const job = await request('GET', `/api/jobs/${encodeURIComponent(id)}`, { shape:SHAPES.job, endpoint:'GET /api/jobs/{id}' });
  sameSteps('GET /api/jobs/{id}', 200, job, steps);
  return job;
}
