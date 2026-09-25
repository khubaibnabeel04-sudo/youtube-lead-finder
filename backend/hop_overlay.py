"""
hop_overlay.py — the in-page video picker used by hop_assist.py.

A shadow-DOM overlay injected into the YouTube tab itself, so you can pick the
next video without leaving Chrome. Built with createElement/textContent only
(YouTube enforces Trusted Types, so innerHTML is not allowed).
Clicks call back into Python through functions exposed on the page
(window.hopPick / window.hopSkip, see attach_bridge).
"""

OVERLAY_JS = r"""(a) => {
  const old = document.getElementById('__hop_overlay');
  if (old) old.remove();
  if (!window.__hopTitle) window.__hopTitle = document.title;
  document.title = '\u{1F446} PICK - ' + window.__hopTitle;
  const el = (tag, css, text) => {
    const e = document.createElement(tag);
    if (css) e.style.cssText = css;
    if (text != null) e.textContent = text;
    return e;
  };
  const views = n => n == null ? '' : n >= 1e6 ? (n / 1e6).toFixed(1) + 'M views'
                    : n >= 1e3 ? Math.round(n / 1e3) + 'K views' : n + ' views';
  const dur = s => {
    if (s == null) return '';
    const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), sec = String(s % 60).padStart(2, '0');
    return h ? h + ':' + String(m).padStart(2, '0') + ':' + sec : m + ':' + sec;
  };
  const badge = (text, bg, fg) => el('span',
      'background:' + bg + ';color:' + fg + ';padding:1px 6px;border-radius:3px;font-size:10px;', text);

  const host = el('div');
  host.id = '__hop_overlay';
  const root = host.attachShadow({mode: 'open'});
  const FULL = 'position:fixed;inset:0;z-index:2147483647;';
  const MINI = 'position:fixed;right:16px;bottom:16px;z-index:2147483647;';
  host.style.cssText = FULL;

  const panel = el('div', 'position:absolute;inset:0;background:rgba(0,0,0,0.82);overflow:auto;' +
      'padding:20px;box-sizing:border-box;font-family:Roboto,Arial,sans-serif;');
  const box = el('div', 'max-width:1200px;margin:0 auto;background:#0f0f13;border:1px solid #06b6d4;' +
      'border-radius:10px;padding:16px;');
  const head = el('div', 'display:flex;justify-content:space-between;align-items:center;gap:12px;' +
      'flex-wrap:wrap;margin-bottom:14px;');
  head.appendChild(el('div', 'color:#e4e4e7;font-weight:700;font-size:16px;',
      'Tab ' + (a.tab + 1) + ' · "' + a.keyword + '" · Hop ' + a.hop + ' — pick the next video'));
  const btns = el('div', 'display:flex;gap:8px;');
  const mkBtn = (label, bg, fg) => el('button', 'background:' + bg + ';color:' + fg +
      ';border:none;border-radius:6px;padding:8px 14px;font-size:13px;font-weight:700;cursor:pointer;', label);
  const skip = mkBtn('Skip this keyword', '#3f3f46', '#e4e4e7');
  const hide = mkBtn(a.items.length ? 'Hide (watch video)' : 'Close - I will pick a video myself', '#3f3f46', '#e4e4e7');
  btns.appendChild(skip); btns.appendChild(hide);
  head.appendChild(btns);
  box.appendChild(head);

  if (!a.items.length) {
    box.appendChild(el('div', 'color:#a1a1aa;padding:28px;text-align:center;',
        'Nothing relevant left on this page. Close this and click any video yourself (sidebar or results) - I will carry on from it and look for new ones. Or skip the keyword.'));
  } else {
    const grid = el('div', 'display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px;');
    a.items.forEach(v => {
      const card = el('div', 'background:#18181b;border:1px solid #3f3f46;border-radius:8px;' +
          'cursor:pointer;overflow:hidden;');
      card.onmouseenter = () => card.style.borderColor = '#06b6d4';
      card.onmouseleave = () => card.style.borderColor = '#3f3f46';
      const th = el('div', 'position:relative;');
      const img = el('img', 'width:100%;display:block;aspect-ratio:16/9;object-fit:cover;');
      img.src = v.thumb;
      th.appendChild(img);
      if (v.dur != null) th.appendChild(el('span',
          'position:absolute;right:6px;bottom:6px;background:rgba(0,0,0,0.8);color:#fff;' +
          'font-size:11px;padding:1px 5px;border-radius:3px;', dur(v.dur)));
      card.appendChild(th);
      const info = el('div', 'padding:8px 10px;');
      info.appendChild(el('div', 'color:#e4e4e7;font-size:13px;font-weight:600;line-height:1.3;', v.title));
      info.appendChild(el('div', 'color:#a1a1aa;font-size:12px;margin-top:4px;',
          v.channel + (v.views != null ? ' · ' + views(v.views) : '')));
      const tags = el('div', 'margin-top:6px;display:flex;gap:6px;flex-wrap:wrap;');
      if (v.real_face) tags.appendChild(badge('\u{1F464} real person', '#052e16', '#22c55e'));
      else if (v.face) tags.appendChild(badge('face', '#1c1917', '#a1a1aa'));
      if (v.mono > 0) tags.appendChild(badge('\u{1F4B0} monetizing', '#2e1065', '#c4b5fd'));
      tags.appendChild(badge('niche ' + v.niche, '#082f3a', '#22d3ee'));
      tags.appendChild(badge('score ' + v.score, '#27272a', '#a1a1aa'));
      info.appendChild(tags);
      card.appendChild(info);
      card.onclick = () => { window.hopPick(a.promptId, v.video_id); };
      grid.appendChild(card);
    });
    box.appendChild(grid);
  }
  panel.appendChild(box);

  const mini = el('button', 'display:none;background:#f59e0b;color:#000;border:none;border-radius:8px;' +
      'padding:12px 18px;font-size:14px;font-weight:700;cursor:pointer;box-shadow:0 4px 16px rgba(0,0,0,0.5);',
      a.items.length ? '\u{1F446} Pick next video (' + a.items.length + ')' : '\u{1F446} Pick a video yourself, or skip');
  hide.onclick = () => { panel.style.display = 'none'; mini.style.display = 'block'; host.style.cssText = MINI; };
  mini.onclick = () => { mini.style.display = 'none'; panel.style.display = 'block'; host.style.cssText = FULL; };
  skip.onclick = () => { window.hopSkip(a.promptId); };

  root.appendChild(panel);
  root.appendChild(mini);
  document.documentElement.appendChild(host);
}"""

HIDE_OVERLAY_JS = r"""() => {
  const o = document.getElementById('__hop_overlay');
  if (o) o.remove();
  if (window.__hopTitle) { document.title = window.__hopTitle; window.__hopTitle = null; }
}"""


async def attach_bridge(tab):
    """Expose window.hopPick / window.hopSkip on the tab's page (once per page)."""
    async def hop_pick(prompt_id, video_id):
        tab.q.put_nowait({"type": "pick", "prompt_id": prompt_id, "video_id": video_id})

    async def hop_skip(prompt_id):
        tab.q.put_nowait({"type": "skip", "prompt_id": prompt_id})

    await tab.page.expose_function("hopPick", hop_pick)
    await tab.page.expose_function("hopSkip", hop_skip)
