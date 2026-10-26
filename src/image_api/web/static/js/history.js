/* 右栏：历史（GET /api/history，新的在前；滚到底用 before 接着往前翻） */
import * as api from './api.js';
import { $, esc, when, routeName } from './dom.js';
import { S, isDone, stepById, fail, clearFault } from './store.js';

const PAGE = 50;
const HISTORY_FAULT = '读历史';

/* 第一页（启动时、后端恢复后、点「重试」） */
export async function loadHistory(){
  try {
    const { items } = await api.getHistory(PAGE);
    S.history = items; S.historyDone = items.length < PAGE; S.historyFailed = false; clearFault(HISTORY_FAULT);
  } catch (err){ S.historyFailed = true; fail(HISTORY_FAULT, err); }
}

/* 出了新图：取第一页，把新的放到最前面（已经往前翻过的部分留着） */
export async function refreshHistoryHead(){
  try {
    const { items } = await api.getHistory(PAGE), ids = new Set(items.map(x => x.id));
    S.history = [...items, ...S.history.filter(x => !ids.has(x.id))];
  } catch (err){ fail(HISTORY_FAULT, err); }
}

/* 往前翻一页。返回是否失败过（失败要整页重画来显示原因） */
export async function loadMoreHistory(){
  if (S.historyLoading || S.historyDone || !S.history.length) return false;
  S.historyLoading = true; renderFilm();
  try {
    const { items } = await api.getHistory(PAGE, S.history[S.history.length - 1].id), ids = new Set(S.history.map(x => x.id));
    S.history = [...S.history, ...items.filter(x => !ids.has(x.id))]; S.historyDone = items.length < PAGE;
    return false;
  } catch (err){ fail('读更早的历史', err); return true; }
  finally { S.historyLoading = false; renderFilm(); }
}

/* 列表还没滚动条（或已经滚到底附近）而后端还有更早的：该往前翻了 */
export function wantsMore(){
  const el = $('#film');
  return !S.historyLoading && !S.historyDone && S.history.length > 0 && el.scrollHeight - el.scrollTop - el.clientHeight < el.clientHeight / 2;
}

function isShowing(x){
  const v = S.view; if (!v) return false;
  if (v.kind === 'history') return v.hid === x.id;
  if (v.kind === 'result' || (v.kind === 'step' && !S.job)){ const st = stepById(v.id); return isDone(st) && st.run.image.id === x.id; }
  return false;
}

export function renderFilm(){
  if (S.historyFailed && !S.history.length){   // 第一页就没读到：不说「0 张」，说没读到
    $('#filmCount').textContent = '';
    $('#film').innerHTML = `<div class="film-empty">历史没读到<br><button class="linkbtn" data-act="history-retry">重试</button></div>`;
    return;
  }
  $('#filmCount').textContent = S.history.length + (S.historyDone ? '' : '+') + ' 张';
  if (!S.history.length){ $('#film').innerHTML = `<div class="film-empty">出过的图会在这里</div>`; return; }
  let h = '', g = '';
  S.history.forEach(x => {
    const w = when(x); if (w !== g){ g = w; h += `<div class="film-g">${g}</div>`; }
    const fresh = S.fresh.has(x.id);
    h += `<button class="fi ${isShowing(x) ? 'sel' : ''}" data-act="view-hist" data-hid="${esc(x.id)}" title="${esc(x.instruction || '')}">${fresh ? '<span class="new">新</span>' : ''}<img src="${esc(x.url)}" alt="" loading="lazy">
      <span class="fm"><i class="rdot ${x.route}"></i>${fresh ? '第 ' + x.step + ' 步' : routeName(x.route)}</span></button>`;
  });
  if (S.historyLoading) h += `<div class="film-more">加载中…</div>`;
  $('#film').innerHTML = h;
}
