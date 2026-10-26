/* 原文框：ComfyUI 失败原文、后端原文。可展开、可复制；src 写明原文从哪来（只在这一处写） */
import { esc } from './dom.js';
import { icon } from './icons.js';

export function rawHTML(text, open, src, toggle){
  return `<div class="raw-wrap"><pre class="raw ${open ? 'open' : ''}"><span class="rt">${esc(text)}</span></pre>
    <div class="raw-bar">${toggle ? `<button data-act="raw-toggle">${icon(open ? 'up' : 'down', 12)}${open ? '收起' : '展开原文'}</button>` : ''}<button data-act="copy-raw">${icon('copy', 12)}复制</button><span class="src">${esc(src)}</span></div></div>`;
}
