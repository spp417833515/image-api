/* 图标：内联 SVG（24×24 描边路径），不用图片、不用字体 */
const ICONS = {
  plus:'M12 5v14M5 12h14', x:'M6 6l12 12M18 6L6 18', check:'M5 12.5l4.5 4.5L19 7',
  link:'M10 13.5a4 4 0 0 0 5.7.3l2.8-2.8a4 4 0 0 0-5.7-5.7l-1.2 1.2M14 10.5a4 4 0 0 0-5.7-.3l-2.8 2.8a4 4 0 0 0 5.7 5.7l1.2-1.2',
  brush:'M14.5 4.5l5 5L10 19l-5.5.5L5 14z M12.5 6.5l5 5',
  eraser:'M4 15.5l9-9 6.5 6.5-7 7H8.5z M9 10.5l6.5 6.5 M10 20h10',
  trash:'M4 7h16M9.5 7V4.5h5V7M6.5 7l1 13h9l1-13',
  download:'M12 4v11M7 10.5l5 5 5-5M5 20h14',
  arrow:'M5 12h13M13 6.5l5.5 5.5-5.5 5.5',
  sparkle:'M12 3.5l1.9 5.6 5.6 1.9-5.6 1.9L12 18.5l-1.9-5.6L4.5 11l5.6-1.9z M18.5 16.5l.7 2 2 .7-2 .7-.7 2-.7-2-2-.7 2-.7z',
  dice:'M5 4h14a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1z M8.5 8.5h.01M15.5 15.5h.01M12 12h.01M15.5 8.5h.01M8.5 15.5h.01',
  right:'M9 6l6 6-6 6', down:'M6 9l6 6 6-6', up:'M6 15l6-6 6 6',
  alert:'M12 3.5l9.5 16.5h-19z M12 10v4.5 M12 17.3v.2',
  copy:'M8.5 8.5h11v11h-11z M15.5 8.5V4.5h-11v11h4',
  image:'M3.5 5h17v14h-17z M3.5 16l5-5 4 4 2.5-2.5 5.5 5.5 M15.5 8.5h.01',
  plug:'M9 3v5M15 3v5M6.5 8h11v3.5a5.5 5.5 0 0 1-11 0z M12 17v4',
  refresh:'M19.5 12a7.5 7.5 0 1 1-2.2-5.3 M19.5 4.5v4h-4',
  play:'M8 5.5v13l10.5-6.5z',
};

export function icon(name, size = 16){
  const d = ICONS[name];
  if (!d) throw new Error(`icon: no icon named "${name}"; known: ${Object.keys(ICONS).join(', ')}`);
  return `<svg class="ic" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${d}"/></svg>`;
}
