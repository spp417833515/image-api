/* 重画：整页、只刷新会变的部分、只刷新进度。重画前先把不能再涂的蒙版收起来 */
import { $ } from './dom.js';
import { S, runningIdx, pctOf } from './store.js';
import { replan } from './sync.js';
import { renderTop } from './topbar.js';
import { renderSteps, refreshCards, refreshHead } from './steps.js';
import { renderDock } from './dock.js';
import { renderStage, resolveView, refreshProgress } from './canvas.js';
import { renderFilm } from './history.js';
import { stopPaint } from './paint.js';

/* 状态变了以后：马上重画（改动立刻可见，受影响的步骤显示「正在检查」、生成先不可用），再重新规划，回来后再画一次。
   规划慢或卡住都不挡页面 */
export function update(){ renderAll(); return replan().then(renderAll); }
/* 蒙版上传（paint.js 返回的 Promise）完成后再规划、重画一次 */
export function track(upload){ if (upload) upload.then(update); }

/* 正在涂的底图没了（比如上一步的结果作废了）：结束涂，涂过的照样上传 */
function settlePaint(){
  if (S.mode === 'mask' && S.status && !resolveView().canPaint) track(stopPaint(false));
}

/* 重画会重建输入框：记下焦点和光标，画完放回去 */
function saveFocus(){
  const a = document.activeElement, card = a && a.dataset && a.dataset.field && a.closest('.card');
  return card ? { step:card.dataset.step, field:a.dataset.field, start:a.selectionStart, end:a.selectionEnd } : null;
}
function restoreFocus(f){
  const el = f && $(`.card[data-step="${f.step}"] [data-field="${f.field}"]`);
  if (!el) return;
  el.focus();
  if (f.start !== null) el.setSelectionRange(f.start, f.end);
}

/* 输入法拼写中不重建输入框（会打断拼写）：这期间的整页重画等上屏后补一次 */
let composing = false, deferred = false;
export function setComposing(on){
  composing = on;
  if (!on && deferred){ deferred = false; renderAll(); }
}

export function renderAll(){
  if (composing){ deferred = true; return; }
  settlePaint();
  renderTop();
  const focus = saveFocus();
  renderSteps(); renderDock(); renderStage(); renderFilm();
  restoreFocus(focus);
}

/* 正在打字：卡片头（「正在检查…」）和生成按钮马上跟着变，不重建输入框、不动画布 */
export function refreshEditing(){ refreshCards(); renderDock(); }

/* 打字停下、规划回来：只刷新卡片上会变的部分、生成按钮和画布（画布上可能有规划失败的说明），不重建输入框 */
export function refreshDyn(){
  settlePaint();
  refreshCards(); renderDock(); renderStage();
}

export function updateProgress(){
  const ri = runningIdx(); if (ri < 0) return;
  refreshProgress(pctOf(S.steps[ri].run));
  refreshHead(ri); renderDock();
}
