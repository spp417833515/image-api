/* 后端响应里页面用到的字段（契约 v1）。api.js 拿到 2xx（以及 422 rejected 带的 plan）先按这里检查：
   缺字段、类型不对、前后矛盾，就报 bad_response，不让坏数据流进页面。只列页面真用到的字段；可为 null 的字段缺省也算 null。 */

const NAME = { string:'字符串', int:'整数', number:'数字', boolean:'true/false', object:'对象', array:'数组', iso:'带时区的 ISO 8601 时间' };
const ISO_TZ = /^\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d(\.\d+)?)?(Z|[+-]\d\d:?\d\d)$/;
const str = { t:'string' }, int = { t:'int' }, num = { t:'number' }, bool = { t:'boolean' }, iso = { t:'iso' };
const nul = s => ({ ...s, nullable:true });
const arr = (item, min = 0) => ({ t:'array', item, min });
const obj = (fields, rule = null) => ({ t:'object', fields, rule });
const oneOf = (...vals) => ({ t:'enum', vals });
const short = v => { const s = JSON.stringify(v), cut = s === undefined ? '' : s.slice(0, 60); return s === undefined ? '（没有这个字段）' : cut === s ? s : cut + '…'; };

/* 返回第一个问题（「路径 应为…，收到…」），没问题返回 null */
export function problem(v, s, path){
  if ((v === null || v === undefined) && s.nullable) return null;
  const wrong = () => `${path} 应为${NAME[s.t]}，收到 ${short(v)}`;
  switch (s.t){
    case 'string': return typeof v === 'string' ? null : wrong();
    case 'int': return Number.isInteger(v) ? null : wrong();
    case 'number': return typeof v === 'number' && Number.isFinite(v) ? null : wrong();
    case 'boolean': return typeof v === 'boolean' ? null : wrong();
    case 'iso': return typeof v === 'string' && ISO_TZ.test(v) && !Number.isNaN(Date.parse(v)) ? null : wrong();
    case 'enum': return s.vals.includes(v) ? null : `${path} 应为 ${s.vals.join(' | ')} 之一，收到 ${short(v)}`;
    case 'array': {
      if (!Array.isArray(v)) return wrong();
      if (v.length < s.min) return `${path} 至少要有 ${s.min} 项，收到 ${v.length} 项`;
      for (let k = 0; k < v.length; k++){ const p = problem(v[k], s.item, `${path}[${k}]`); if (p) return p; }
      return null;
    }
    case 'object': {
      if (!v || typeof v !== 'object' || Array.isArray(v)) return wrong();
      for (const [k, fs] of Object.entries(s.fields)){ const p = problem(v[k], fs, `${path}.${k}`); if (p) return p; }
      return s.rule ? s.rule(v, path) : null;
    }
  }
  throw new Error(`shapes: unknown type "${s.t}" at ${path}`);
}

const route = oneOf('t2i', 'ref', 'mask');
const size = obj({ width:int, height:int });
const version = obj({ tag:nul(str), commit:str });
const imageFields = { id:str, url:str, width:int, height:int };
const routeState = obj({ available:bool, reason:nul(str) },
  (v, p) => !v.available && !v.reason ? `${p}.reason 在 available 为 false 时要写明原因，收到 ${short(v.reason)}` : null);
const planStep = obj({ step:int, route:nul(route), output:nul(size), requested:nul(size),
                       reject:nul(obj({ code:str, message:str })),
                       unavailable:nul(obj({ code:oneOf('comfy_down', 'missing_file', 'missing_node', 'upgrading'), message:str })) },
  (v, p) => ((v.route ?? null) === null) === ((v.reject ?? null) === null) ? `${p}：route 和 reject 必须正好一个为 null（缺字段算 null）` : null);
const jobStep = obj({ step:int, route, state:oneOf('queued', 'running', 'done', 'failed', 'skipped'), progress:nul(num),
                      image:nul(obj({ ...imageFields, route, seed:int })), error:nul(obj({ raw:str })) },
  (v, p) => v.state === 'done' && !v.image ? `${p}.image 在 state 为 done 时不能为空`
    : v.state === 'failed' && !v.error ? `${p}.error 在 state 为 failed 时不能为空`
    : v.progress !== null && v.progress !== undefined && (v.progress < 0 || v.progress > 1) ? `${p}.progress 应在 0–1 之间，收到 ${v.progress}` : null);

/* 每个接口的成功响应 */
export const SHAPES = {
  upload:obj(imageFields),
  history:obj({ items:arr(obj({ ...imageFields, route, seed:int, instruction:str, created_at:iso, step:int })) }),
  status:obj({
    online:bool,
    upgrading:nul(obj({ target:str, since:iso })),
    version,
    last_check:nul(obj({ at:iso, version, items:arr(obj({ label:str, ok:bool, note:nul(str) }), 1) })),
    last_operation:nul(obj({ at:iso, kind:oneOf('upgrade', 'rollback'), from:version, to:version, result:oneOf('passed', 'reverted'), note:nul(str) })),
    routes:obj({ t2i:routeState, ref:routeState, mask:routeState }),
    limits:obj({ max_images:int, size_multiple:int, default_size:size }),
    size_presets:arr(obj({ label:str, width:int, height:int })),
  }),
  plan:obj({ steps:arr(planStep, 1) }),
  generate:obj({ job_id:str }, (v, p) => v.job_id ? null : `${p}.job_id 不能是空字符串`),
  job:obj({ id:str, state:oneOf('queued', 'running', 'done', 'failed'), steps:arr(jobStep, 1) },
    (v, p) => v.state === 'done' && v.steps.some(s => s.state !== 'done') ? `${p}.state 为 done，但不是每一步都 done`
      : v.state === 'failed' && !v.steps.some(s => s.state === 'failed') ? `${p}.state 为 failed，但没有哪一步 failed` : null),
};
