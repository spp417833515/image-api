/* 左栏底部：步骤链小结 + 「生成」按钮 + 为什么现在不能生成 */
import { $, esc, routeName } from './dom.js';
import { icon } from './icons.js';
import { S, isDone, rejectOf, isSoft, routeOf, pendingIdx, runRange, gate, runningIdx, pctOf, checking } from './store.js';

function genLabel(todo){
  const tot = S.steps.length, a = todo[0] + 1, b = todo[todo.length - 1] + 1, r0 = S.steps[todo[0]].run;
  const verb = r0 && r0.state === 'failed' ? '重试' : '生成';
  if (todo.length === tot) return tot > 1 ? `${verb} · ${tot} 步` : verb;
  if (todo.length === 1) return `${verb}第 ${a} 步`;
  return b - a + 1 === todo.length ? `${verb}第 ${a}–${b} 步` : `从第 ${a} 步${verb} · ${todo.length} 步`;
}

function chainItem(st, i){
  if (isDone(st)) return `<span class="r done">${icon('check', 12)}${routeName(st.run.image.route)}</span>`;
  const r = rejectOf(st, i), rt = routeOf(i);
  if (r) return isSoft(r) ? `<span class="r mute">第 ${i + 1} 步待写指令</span>` : `<span class="r bad">第 ${i + 1} 步要改</span>`;
  return rt ? `<span class="r"><i class="rdot ${rt}"></i>${routeName(rt)}</span>`
    : `<span class="r mute">第 ${i + 1} 步${checking(i) && !S.planError ? '检查中' : ''}</span>`;
}

export function renderDock(){
  if (!S.status){ $('#dock').innerHTML = ''; return; }
  const chain = S.steps.map(chainItem).join(`<span class="arrow">→</span>`);
  let label, dis = true, why = '', tone = '', cls = '', fill = 0;
  if (S.job){
    const ri = runningIdx(), pct = ri >= 0 ? pctOf(S.steps[ri].run) : 0;
    label = ri >= 0 ? `第 ${ri + 1}/${S.steps.length} 步 · ${pct}%` : '排队中'; cls = 'running'; fill = pct;   // 按钮文字和填充条都是当前这一步的进度
    if (S.job.failSince !== null){ tone = 'warn'; why = icon('alert', 14) + '上一个任务还在跑，进度暂时读不到，正在重试'; }   // 不能再点生成：免得重复出图
  } else {
    const { upto, after } = runRange(), todo = pendingIdx(upto), g = gate(upto);
    label = todo.length ? genLabel(todo) : '已全部出图'; dis = g.dis; tone = g.tone || '';
    why = g.why ? (g.alert ? icon('alert', 14) : '') + esc(g.why) : '';
    if (!dis && after !== null){ tone = 'warn'; why = `蒙版是在旧图上涂的，先出第 ${upto + 1} 步的新图，再看蒙版对不对`; }
  }
  $('#dock').innerHTML = `<div class="dock-sum">${S.steps.length} 步：${chain}</div>
    <button class="gen ${cls}" data-act="gen" ${dis ? 'disabled' : ''}><span class="gen-fill" style="width:${fill}%"></span>
      <span class="gl">${S.job ? '<i class="spin"></i>' : icon('sparkle', 18)}${label}</span>${S.job ? '' : '<kbd>Ctrl ↵</kbd>'}</button>
    <div class="dock-why ${tone}">${why}</div>`;
}
