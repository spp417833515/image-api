/* 页面通用小工具：选择器、转义、时间显示、提示条、复制、图片缓存 */
export const $ = (sel, root = document) => root.querySelector(sel);
export const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;' }[c]));
/* 内联 style 里的背景图（url 来自后端的图片记录） */
export const bgImage = url => `background-image:url('${esc(String(url).replace(/'/g, '%27'))}')`;
export const cssVar = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/* 路由的中文名，只用来显示；路由本身来自 /api/plan 和图片记录 */
const ROUTE = { t2i:'文生图', ref:'参考图编辑', mask:'蒙版改写' };
export const routeName = route => ROUTE[route] || route;

/* 时间按本地时区显示（后端给 Z 或带偏移量的 ISO 8601） */
const pad2 = n => String(n).padStart(2, '0');
export const fmtT = iso => { const d = new Date(iso); return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`; };
/* 历史分组：刚才 / 今天 / 昨天 / 月-日 */
export function when(item){
  const t = new Date(item.created_at), now = new Date(), day = d => d.toDateString();
  if (now - t >= 0 && now - t < 10 * 60e3) return '刚才';
  if (day(t) === day(now)) return '今天';
  const y = new Date(now); y.setDate(now.getDate() - 1);
  return day(t) === day(y) ? '昨天' : `${t.getMonth() + 1}-${t.getDate()}`;
}

/* 提示条：一个常驻节点，画布每次重画后再挂回去，所以重画不会把正在显示的提示冲掉 */
const toastEl = Object.assign(document.createElement('div'), { className:'toast', id:'toast' });
let toastTimer;
export const mountToast = parent => parent.appendChild(toastEl);
export function toast(text){
  toastEl.textContent = text; toastEl.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => toastEl.classList.remove('show'), 2200);
}

export function autoGrow(t){ t.style.height = 'auto'; t.style.height = Math.min(t.scrollHeight + 2, 150) + 'px'; }

/* 复制文字：先用剪贴板接口，不行再用选中复制；返回是否真的复制成功（提示条按它说实话） */
export async function copyText(text){
  try { await navigator.clipboard.writeText(text); return true; }
  catch (err){ console.warn('clipboard.writeText failed, trying execCommand:', err); }
  const ta = document.createElement('textarea'); ta.value = text; ta.className = 'sr'; document.body.appendChild(ta); ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); }
  catch (err){ console.warn('execCommand(copy) failed:', err); }
  ta.remove(); return ok;
}

/* 图片缓存：蒙版缩略图要把底图画进小画布 */
const imgCache = {};
export function getImg(src, onLoad){
  let im = imgCache[src];
  if (!im){ im = imgCache[src] = new Image(); im.src = src; }
  if (im.complete && im.naturalWidth) onLoad(im); else im.addEventListener('load', () => onLoad(im), { once:true });
}
