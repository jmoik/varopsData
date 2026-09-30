#!/usr/bin/env python3
"""Restyle a generated varops joint-calibration report.

Keeps every chart, measurement and table from the generated HTML and replaces the
presentation: light/dark theme, sidebar navigation, a summary price schedule,
colour-blind-checked machine colours and hover tooltips.

    python3 restyle_report.py 0.4.0/joint-calibration.html

With one argument the file is restyled in place and the untouched original is kept
next to it as *.orig.html (re-running restyles from that original).
"""
import html
import re
import shutil
import sys
from pathlib import Path

MARKER = '<meta name="varops-restyled" content="1">'

# Generated hex -> CSS token. Charts keep their hex attributes; CSS attribute
# selectors remap them so the file size does not change.
SERIES = {
    '#2563eb': 'm1',
    '#dc6b18': 'm4',
    '#7c3aed': 'ryzen',
    '#b91c1c': 'intel',
    '#be185d': 'i7',
    '#4a3aa7': 'r5',
    '#172536': 'basis',
}
MACHINES = {
    'Apple M1 Pro': 'm1',
    'Apple M4 Pro': 'm4',
    'AMD Ryzen 9 9950X': 'ryzen',
    'Intel Core i5-12500': 'intel',
    'Intel Core i7-7700': 'i7',
    'AMD Ryzen 5 3600': 'r5',
}
# Colour words in the generated prose that must follow the new palette.
TEXT_FIXES = [
    ('(solid black)', '(bold solid line)'),
    ('The black curve is the unrounded', 'The bold solid line is the unrounded'),
    ('Solid black is the envelope', 'The bold solid line is the envelope'),
    ('orange squares', 'amber squares'),
    ('purple diamonds', 'teal diamonds'),
]
REPEATED_NOTE = 'The bold solid line is the unrounded envelope used for pricing.'
W_NOTE = 'W(n) rounds bytes up to a multiple of eight.'

CSS = r'''
:root{
  color-scheme:light;
  --bg:#f5f6f8;--surface:#ffffff;--surface-2:#f3f4f7;--surface-3:#eceef2;
  --border:#e3e6eb;--border-strong:#d3d7de;
  --text:#11151c;--text-2:#4a5260;--muted:#6f7785;
  --grid:#eceef2;--axis:#cfd4dc;
  --accent:#2f5fd0;--accent-soft:#e8eefc;
  --warn:#9a5b00;--warn-bg:#fff6e5;--warn-border:#f0c46b;
  --hi:#b42318;--hi-bg:#fdecea;--mid:#8a5a00;--mid-bg:#fff4dc;
  --m1:#2a78d6;--m4:#eda100;--ryzen:#1baf7a;--intel:#e34948;--i7:#a3548c;--r5:#4a3aa7;--basis:#11151c;
  --shadow:0 1px 2px rgba(16,24,40,.04),0 1px 3px rgba(16,24,40,.06);
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  color-scheme:dark;
  --bg:#0e1014;--surface:#16181d;--surface-2:#1b1e24;--surface-3:#23272f;
  --border:#262a32;--border-strong:#323741;
  --text:#e9ebef;--text-2:#b0b6c1;--muted:#838b98;
  --grid:#23262d;--axis:#3a3f49;
  --accent:#8fb0ff;--accent-soft:#1d2536;
  --warn:#f4c46a;--warn-bg:#2a2214;--warn-border:#6b5020;
  --hi:#ff8f84;--hi-bg:#3a1d1b;--mid:#f0c46a;--mid-bg:#2f2615;
  --m1:#3987e5;--m4:#b38e00;--ryzen:#199e70;--intel:#e66767;--i7:#a3548c;--r5:#9085e9;--basis:#f1f2f4;
  --shadow:none;
}}
:root[data-theme="dark"]{
  color-scheme:dark;
  --bg:#0e1014;--surface:#16181d;--surface-2:#1b1e24;--surface-3:#23272f;
  --border:#262a32;--border-strong:#323741;
  --text:#e9ebef;--text-2:#b0b6c1;--muted:#838b98;
  --grid:#23262d;--axis:#3a3f49;
  --accent:#8fb0ff;--accent-soft:#1d2536;
  --warn:#f4c46a;--warn-bg:#2a2214;--warn-border:#6b5020;
  --hi:#ff8f84;--hi-bg:#3a1d1b;--mid:#f0c46a;--mid-bg:#2f2615;
  --m1:#3987e5;--m4:#b38e00;--ryzen:#199e70;--intel:#e66767;--i7:#a3548c;--r5:#9085e9;--basis:#f1f2f4;
  --shadow:none;
}
*{box-sizing:border-box}
html{scroll-padding-top:72px}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
code,.mono{font-family:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace;font-size:.88em}
p{margin:0 0 12px}strong{font-weight:600}

/* top bar */
.topbar{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:16px;height:56px;padding:0 20px;
  background:color-mix(in srgb,var(--bg) 82%,transparent);backdrop-filter:saturate(1.4) blur(12px);-webkit-backdrop-filter:saturate(1.4) blur(12px);border-bottom:1px solid var(--border)}
.brand{display:flex;align-items:baseline;gap:10px;white-space:nowrap;font-weight:650;letter-spacing:-.01em}
.brand .ver{font:500 12px ui-monospace,monospace;color:var(--muted);padding:2px 7px;border:1px solid var(--border);border-radius:99px}
.tabs{display:flex;gap:4px;overflow-x:auto;scrollbar-width:none;flex:1;min-width:0}
.tabs a{padding:6px 11px;border-radius:8px;color:var(--text-2);font-size:14px;white-space:nowrap}
.tabs a:hover{background:var(--surface-3);text-decoration:none;color:var(--text)}
.tabs a.active{background:var(--accent-soft);color:var(--accent);font-weight:550}
.theme-btn{margin-left:auto;display:grid;place-items:center;width:34px;height:34px;border-radius:9px;border:1px solid var(--border);background:var(--surface);color:var(--text-2);cursor:pointer}
.theme-btn:hover{color:var(--text);border-color:var(--border-strong)}
.theme-btn svg{width:17px;height:17px}

/* layout */
.layout{display:grid;grid-template-columns:232px minmax(0,1fr);gap:32px;max-width:1320px;margin:0 auto;padding:28px 24px 80px}
.sidebar{position:sticky;top:84px;align-self:start;max-height:calc(100vh - 104px);overflow-y:auto;font-size:14px;padding-right:4px}
.sidebar h4{margin:18px 0 6px;padding:0 10px;font-size:11px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;color:var(--muted)}
.sidebar h4:first-child{margin-top:0}
.sidebar a{display:flex;justify-content:space-between;align-items:baseline;gap:10px;padding:5px 10px;border-radius:7px;color:var(--text-2)}
.sidebar a:hover{background:var(--surface-3);color:var(--text);text-decoration:none}
.sidebar a.current{background:var(--accent-soft);color:var(--accent)}
.sidebar a .p{font:500 13px ui-monospace,monospace;letter-spacing:.01em}
.sidebar a .v{font:12px ui-monospace,monospace;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:120px}
main{min-width:0}

/* cards */
.card,article{background:var(--surface);border:1px solid var(--border);border-radius:14px;box-shadow:var(--shadow);padding:24px 26px;margin-bottom:22px}
.hero{padding:30px 30px 26px}
.eyebrow{font-size:12px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);margin-bottom:6px}
h1{font-size:28px;line-height:1.2;letter-spacing:-.02em;margin:0 0 14px;font-weight:680}
h2{font-size:20px;letter-spacing:-.01em;margin:0 0 14px;font-weight:650}
.section-title{display:flex;align-items:baseline;gap:12px;margin:36px 0 14px}
.section-title h2{margin:0}
.section-title .count{color:var(--muted);font-size:14px}
.section-intro{color:var(--text-2);max-width:86ch;margin:0 0 18px}
.group-title{font-size:15px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;color:var(--muted);margin:30px 0 12px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 16px}
.chip{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:99px;background:var(--surface-2);border:1px solid var(--border);font-size:13px;color:var(--text-2);white-space:nowrap}
.lede{color:var(--text-2);font-size:15.5px;max-width:78ch}
.card-title{font-size:13px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:0 0 12px}
.prose p{color:var(--text-2);max-width:82ch}
.prose p strong{color:var(--text)}
.muted{color:var(--muted)}
.warning{position:relative;background:var(--warn-bg);border:1px solid var(--warn-border);border-radius:10px;padding:12px 14px 12px 42px;color:var(--text)!important}
.warning::before{content:"!";position:absolute;left:13px;top:12px;width:18px;height:18px;border-radius:50%;background:var(--warn);color:var(--surface);font:700 12px/18px system-ui;text-align:center}

/* details */
details{margin:12px 0}
summary{cursor:pointer;list-style:none;display:inline-flex;align-items:center;gap:8px;color:var(--text-2);font-weight:550;font-size:14px;padding:4px 0;user-select:none}
summary::-webkit-details-marker{display:none}
summary::before{content:"";width:6px;height:6px;border-right:1.6px solid currentColor;border-bottom:1.6px solid currentColor;transform:rotate(-45deg);transition:transform .15s;margin:0 3px}
details[open]>summary::before{transform:rotate(45deg)}
summary:hover{color:var(--text)}
details>:not(summary){margin-top:10px}
ul{padding-left:20px;color:var(--text-2)}

/* tables */
.table-wrap{overflow-x:auto;border:1px solid var(--border);border-radius:10px;margin:16px 0 4px}
table{width:100%;border-collapse:collapse;font-size:14px;font-variant-numeric:tabular-nums}
th,td{padding:9px 14px;text-align:left;border-bottom:1px solid var(--border);vertical-align:middle}
tr:last-child>td{border-bottom:0}
th{background:var(--surface-2);font-size:12px;font-weight:600;letter-spacing:.03em;color:var(--muted);text-transform:uppercase}
td code{white-space:nowrap;color:var(--text)}
tbody tr:hover>td{background:var(--surface-2)}
tr.row-basis>td{background:var(--surface-2);font-weight:600}
tr.row-basis code{font-weight:650}
td .plot-mark,td .plot-line{margin-right:10px}
.x{display:inline-block;padding:1px 8px;border-radius:6px;font-weight:550}
.x.mid{background:var(--mid-bg);color:var(--mid)}
.x.hi{background:var(--hi-bg);color:var(--hi)}
.schedule td:first-child a{font:600 13.5px ui-monospace,monospace}
.schedule td.cat{color:var(--muted);font-size:13px}
.schedule td.pcol code{font-size:13.5px;font-weight:600}
.schedule td.basis code{color:var(--text-2)}
.schedule tr.group>td{background:var(--surface-2);font-size:12px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;color:var(--muted);padding-top:7px;padding-bottom:7px}
.footnote{font-size:13px;color:var(--muted);margin-top:10px}

/* article heads */
article{scroll-margin-top:72px}
.art-head{display:flex;flex-wrap:wrap;align-items:flex-start;justify-content:space-between;gap:14px 24px;margin-bottom:12px}
.art-head h3{font:680 24px/1.2 ui-monospace,"SF Mono",Menlo,monospace;letter-spacing:-.01em;margin:0}
.art-head h3 a{color:inherit}
.price{display:flex;flex-direction:column;align-items:flex-end;gap:2px}
.price-label{font-size:11px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;color:var(--muted)}
.price-val{font:650 20px ui-monospace,"SF Mono",Menlo,monospace;color:var(--text);background:var(--surface-2);border:1px solid var(--border);padding:4px 12px;border-radius:9px;white-space:nowrap}
.price-val .u{font:500 12px system-ui;color:var(--muted);margin-left:6px}
.rounding{display:flex;flex-wrap:wrap;align-items:center;gap:6px 8px;font-size:13px;color:var(--muted);margin:0 0 10px}
.rounding .chip{font-family:ui-monospace,monospace;font-size:12px}
.note{font-size:14px;color:var(--text-2)}
article>p,article>details>p,.chart>p{color:var(--text-2);max-width:86ch}

/* legends */
.legend,.plot-legend{display:flex;flex-wrap:wrap;gap:6px 18px;margin:10px 0 4px;font-size:13px;color:var(--text-2)}
.legend>span,.plot-legend>span{display:inline-flex;align-items:center;gap:7px;white-space:nowrap}
.legend-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:8px 22px;font-size:14px;color:var(--text-2)}
.legend-grid>span{display:flex;align-items:center;gap:10px}
.plot-mark{display:inline-block;width:9px;height:9px;flex:0 0 9px}
.plot-mark.m1{background:var(--m1);border-radius:50%}
.plot-mark.m4{background:var(--m4);border-radius:1px}
.plot-mark.ryzen{background:var(--ryzen);transform:rotate(45deg) scale(.85)}
.plot-mark.intel{background:var(--intel);clip-path:polygon(50% 0,100% 100%,0 100%);width:10px;height:10px}
.plot-mark.i7{background:var(--i7);clip-path:polygon(0 0,100% 0,50% 100%);width:10px;height:10px}
.plot-mark.r5{background:var(--r5);clip-path:polygon(0 50%,100% 0,100% 100%);width:10px;height:10px}
.plot-mark.dot{width:9px;height:9px;border-radius:50%;transform:none;clip-path:none}
.plot-mark.hollow{background:transparent;border:1.5px solid var(--muted);border-radius:50%}
.plot-line{display:inline-block;width:22px;height:0;border-top:2px dashed;flex:0 0 22px}
.plot-line.m1{border-color:var(--m1)}.plot-line.m4{border-color:var(--m4)}.plot-line.ryzen{border-color:var(--ryzen)}.plot-line.intel{border-color:var(--intel)}.plot-line.i7{border-color:var(--i7)}.plot-line.r5{border-color:var(--r5)}
.plot-line.basis{border-color:var(--basis);border-top-style:solid;border-top-width:3px}
.legend-icon{width:14px;height:14px;flex:0 0 14px}
.swatch{display:inline-block;width:22px;border-top:2px solid;vertical-align:middle}

/* charts */
.chart{margin:14px 0 18px}
.chart>svg{display:block;width:100%;height:auto;max-height:560px;background:var(--surface);overflow:visible}
.facet-title{display:inline-block;margin:18px 0 0;padding:2px 9px;border-radius:6px;background:var(--surface-2);border:1px solid var(--border);font:600 12.5px ui-monospace,monospace;color:var(--text-2)}
.facet-title+.chart{margin-top:6px}
.tick{font:12px system-ui,sans-serif;fill:var(--muted)}
.axis{font:500 13px system-ui,sans-serif;fill:var(--text-2)}
svg [stroke="#cbd5e1"]{stroke:var(--axis)}
svg [stroke="#e2e8f0"]{stroke:var(--grid)}
svg [fill="white"]{fill:var(--surface)}
svg [fill="#526174"]{fill:var(--text-2)}svg [stroke="#526174"]{stroke:var(--text-2)}
''' + ''.join(
    f'svg [fill="{h}"]{{fill:var(--{t})}}svg [stroke="{h}"]{{stroke:var(--{t})}}\n'
    for h, t in SERIES.items()) + r'''
.chart svg path[stroke-dasharray]{stroke-width:1.6;stroke-opacity:.9}
.chart svg path[fill="none"][stroke="#172536"]:not([stroke-dasharray]){stroke-width:2.6}
.chart svg g>g[fill],.chart svg g>circle,.chart svg g>rect,.chart svg g>path[d$="Z"]{fill-opacity:.82}
.chart svg g.hot>*,.chart svg g.hot>g>*{stroke-width:2.2;fill-opacity:1}
.chart svg g.hot{filter:drop-shadow(0 0 2px var(--surface))}

/* tooltip */
.tip{position:fixed;z-index:50;pointer-events:none;background:var(--surface);color:var(--text);border:1px solid var(--border-strong);border-radius:9px;
  box-shadow:0 8px 24px rgba(0,0,0,.18);padding:8px 11px;font-size:13px;line-height:1.45;max-width:340px;opacity:0;transform:translateY(4px);transition:opacity .08s,transform .08s}
.tip.on{opacity:1;transform:none}
.tip .m{display:flex;align-items:center;gap:7px;color:var(--text-2);font-size:12px}
.tip .f{font:12.5px ui-monospace,monospace;color:var(--text);margin:2px 0}
.tip .val{font-weight:650;font-variant-numeric:tabular-nums}

.category[hidden]{display:none}
.mobile-nav{display:none}
@media(max-width:1000px){
  .layout{grid-template-columns:minmax(0,1fr);padding:18px 16px 60px;gap:0}
  .sidebar{display:none}
  .mobile-nav{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 14px}
  .mobile-nav a{padding:4px 10px;border:1px solid var(--border);border-radius:99px;background:var(--surface);font:500 13px ui-monospace,monospace;color:var(--text-2)}
}
@media(max-width:640px){
  .topbar{padding:0 12px;gap:10px}.brand .ver{display:none}
  .card,article{padding:16px;border-radius:12px}.hero{padding:20px 16px}
  h1{font-size:22px}.art-head h3{font-size:20px}.price{align-items:flex-start}
}
'''

SCRIPT = r'''
<script>
(() => {
  const root = document.documentElement;
  const store = { get(){ try { return localStorage.getItem('varops-theme'); } catch { return null; } },
                  set(v){ try { localStorage.setItem('varops-theme', v); } catch {} } };
  const saved = store.get(); if (saved) root.dataset.theme = saved;
  const btn = document.querySelector('.theme-btn');
  const isDark = () => root.dataset.theme ? root.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
  btn.addEventListener('click', () => { const t = isDark() ? 'light' : 'dark'; root.dataset.theme = t; store.set(t); });

  function showCategory(){
    const target = document.getElementById(decodeURIComponent(location.hash.slice(1)));
    const selected = target?.classList.contains('category') ? target.id
      : (target?.closest('.category')?.id || document.querySelector('.category')?.id);
    document.querySelectorAll('.category').forEach(el => { el.hidden = el.id !== selected; });
    document.querySelectorAll('.tabs a').forEach(el => {
      const on = el.getAttribute('href') === '#' + selected;
      el.classList.toggle('active', on); on ? el.setAttribute('aria-current', 'page') : el.removeAttribute('aria-current');
    });
    const art = target?.closest('article')?.id;
    document.querySelectorAll('.sidebar a').forEach(el => el.classList.toggle('current', !!art && el.getAttribute('href') === '#' + art));
    if (target && target.id !== selected) requestAnimationFrame(() => target.scrollIntoView());
  }
  addEventListener('hashchange', showCategory); showCategory();

  // Point tooltips: move native <title>s into data attributes on first hover.
  const MACHINE = { m1: ['Apple M1 Pro', 'm1'], m4: ['Apple M4 Pro', 'm4'], ryzen: ['AMD Ryzen 9 9950X', 'ryzen'], intel: ['Intel Core i5-12500', 'intel'], i7: ['Intel Core i7-7700', 'i7'], r5: ['AMD Ryzen 5 3600', 'r5'] };
  const tip = document.createElement('div'); tip.className = 'tip'; document.body.appendChild(tip);
  const esc = s => s.replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
  function prepare(svg){
    if (svg.dataset.prepped) return; svg.dataset.prepped = 1;
    svg.querySelector(':scope>title')?.remove();
    svg.querySelectorAll('g>title').forEach(t => { t.parentNode.dataset.tip = t.textContent; t.remove(); });
  }
  let hot = null;
  document.addEventListener('mouseover', e => {
    const svg = e.target.closest?.('.chart svg'); if (!svg) return;
    prepare(svg);
    const g = e.target.closest('g[data-tip]');
    if (g === hot) return;
    hot?.classList.remove('hot'); hot = g;
    if (!g) { tip.classList.remove('on'); return; }
    g.classList.add('hot');
    const text = g.dataset.tip, m = text.match(/^(\w+) · (.*?): (\S+) (varops|ns)(.*)$/s);
    if (m) {
      const [name, cls] = MACHINE[m[1]] || [m[1], ''];
      tip.innerHTML = `<div class="m"><i class="plot-mark ${cls}"></i>${esc(name)}</div><div class="f">${esc(m[2])}</div>` +
        `<div><span class="val">${esc(m[3])}</span> ${m[4]}${m[5] ? `<span class="muted">${esc(m[5])}</span>` : ''}</div>`;
    } else tip.textContent = text;
    tip.classList.add('on');
  });
  document.addEventListener('mousemove', e => {
    if (!hot) return;
    const r = tip.getBoundingClientRect(); let x = e.clientX + 14, y = e.clientY + 14;
    if (x + r.width > innerWidth - 8) x = e.clientX - r.width - 14;
    if (y + r.height > innerHeight - 8) y = e.clientY - r.height - 14;
    tip.style.left = x + 'px'; tip.style.top = y + 'px';
  });
  document.addEventListener('mouseleave', () => { hot?.classList.remove('hot'); hot = null; tip.classList.remove('on'); });
})();
</script>
'''

THEME_ICON = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
              'stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6'
              'l1.4 1.4M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4"/></svg>')


def strip_tags(s):
    return html.unescape(re.sub(r'<[^>]+>', '', s))


def fmt_x(match):
    value = float(match.group(1))
    cls = ' hi' if value >= 2 else ' mid' if value >= 1.2 else ''
    return f'<td><span class="x{cls}">{match.group(1)}×</span></td>'


def marker_for(name):
    if name in MACHINES:
        return f'<i class="plot-mark {MACHINES[name]}" aria-hidden="true"></i>'
    if name.startswith('Envelope'):
        return '<i class="plot-line basis" aria-hidden="true"></i>'
    return ''


def restyle_table_rows(body):
    def row(m):
        name = m.group(1)
        cls = ' class="row-basis"' if name.startswith('Envelope') else ''
        return f'<tr{cls}><td>{marker_for(name)}{name}</td>'
    body = re.sub(r'<tr><td>([^<]+)</td>', row, body)
    return re.sub(r'<td>(\d+(?:\.\d+)?)×</td>', fmt_x, body)


def sort_facets(body):
    """Order facet runs such as v=1, v=1024, v=128 … numerically."""
    facet = r'<div class="facet-title">[^<]*</div><div class="chart">\x00SVG\d+\x00</div>'

    def reorder(m):
        items = re.findall(r'<div class="facet-title">([^<]*)</div>(<div class="chart">\x00SVG\d+\x00</div>)', m.group(0))
        nums = [re.fullmatch(r'\w+=(\d+)', t) for t, _ in items]
        if all(nums):
            items = sorted(items, key=lambda it: int(it[0].split('=')[1]))
        return ''.join(f'<div class="facet-title">{t}</div>{c}' for t, c in items)
    return re.sub(f'(?:{facet})+', reorder, body)


def restyle(src):
    svgs = []

    def stash(m):
        svgs.append(m.group(0))
        return f'\x00SVG{len(svgs) - 1}\x00'
    doc = re.sub(r'<svg\b.*?</svg>', stash, src, flags=re.S)
    for a, b in TEXT_FIXES:
        doc = doc.replace(a, b)
        svgs = [s.replace(a, b) for s in svgs]

    title = re.search(r'<title>(.*?)</title>', doc).group(1)
    header = re.search(r'<header>(.*?)</header>', doc, re.S).group(1)
    body = doc[doc.index('</header>') + len('</header>'):doc.index('<script>')]

    # Collect categories and primitives for navigation and the schedule table.
    # Sections are BIPs titled "<BIP> · <opcodes>"; navigation uses the short part.
    # Entries without a fitted candidate show their implemented or composed charge.
    cats = []
    for sm in re.finditer(r'<section class="category" id="([^"]+)"><h2>(.*?)</h2>(.*?)</section>', body, re.S):
        prims = []
        for am in re.finditer(r'<article id="([^"]+)"><h[34]>(.*?)</h[34]>(.*?)</article>', sm.group(3), re.S):
            price = (re.search(r'Rounded implementation candidate:</strong> <code>(.*?)</code>', am.group(3)) or
                     re.search(r'(?:Charged as|Implemented in varops\.h):</strong> <code>(.*?)</code>', am.group(3)))
            basis = re.search(r'Envelope \(pricing basis\)</td><td><code>(.*?)</code>', am.group(3))
            prims.append((am.group(1), am.group(2), price.group(1) if price else '', basis.group(1) if basis else ''))
        cats.append((sm.group(1), sm.group(2).split(' · ')[0], prims))
    cat_of = {pid: name for _, name, prims in cats for pid, _, _, _ in prims}

    # Article heads: primitive name + prominent rounded price.
    def art_head(m):
        pid, name, formula, rest, rounding, note = m.groups()
        pieces = [p.strip() for p in rounding.split(',')]
        parts = ['<div class="art-head"><div>',
                 f'<div class="eyebrow">{cat_of.get(pid, "")}</div><h3><a href="#{pid}">{name}</a></h3></div>',
                 '<div class="price"><span class="price-label">Rounded candidate</span>',
                 f'<code class="price-val">{formula}<span class="u">varops</span></code></div></div>',
                 '<div class="rounding"><span>Unrounded → rounded (fixed, then variable)</span>',
                 ''.join(f'<span class="chip">{p}</span>' for p in pieces), '</div>']
        rest = rest.replace(W_NOTE, '').strip()
        note = note.replace(REPEATED_NOTE, '').strip()
        extra = ' '.join(x for x in (W_NOTE if 'W(' in formula else '', rest, note) if x)
        if extra:
            parts.append(f'<p class="note">{extra}</p>')
        return f'<article id="{pid}">' + ''.join(parts)
    body = re.sub(r'<article id="([^"]+)"><h[34]>([^<]+)</h[34]><p><strong>Rounded implementation candidate:</strong> '
                  r'<code>(.*?)</code> varops\.\s*(.*?)</p><p class="muted">Coefficient rounding \(fixed, then variable\): '
                  r'(.*?)\.(?!\d)\s*(.*?)</p>', art_head, body)

    # Entries without a fitted candidate (composition checks, pending calibration) keep a plain head.
    body = re.sub(r'<article id="([^"]+)"><h[34]>([^<]+)</h[34]>',
                  lambda m: (f'<article id="{m.group(1)}"><div class="art-head"><div><div class="eyebrow">'
                             f'{cat_of.get(m.group(1), "")}</div><h3><a href="#{m.group(1)}">{m.group(2)}</a></h3></div></div>'),
                  body)

    # Section headings, intros and in-page entry chips (shown on narrow screens).
    def section_head(m):
        n = len(re.findall(r'<a ', m.group(4)))
        return (f'<section class="category" id="{m.group(1)}"><div class="section-title"><h2>{m.group(2)}</h2>'
                f'<span class="count">{n} entr{"ies" if n != 1 else "y"}</span></div>{m.group(3) or ""}'
                f'<nav class="mobile-nav" aria-label="Entries in this section">{m.group(4)}</nav>')
    body = re.sub(r'<section class="category" id="([^"]+)"><h2>(.*?)</h2>(<p class="section-intro">.*?</p>)?<nav class="nav"[^>]*>(.*?)</nav>',
                  section_head, body)

    body = re.sub(r'<article.*?</article>', lambda m: restyle_table_rows(m.group(0)), body, flags=re.S)
    body = sort_facets(body)

    # Series-coloured legend text -> neutral text with a coloured mark.
    def legend_span(m):
        color, glyph, label = m.groups()
        token = SERIES.get(color.lower(), '')
        if glyph == '●':
            return f'<span><i class="plot-mark dot {token}" aria-hidden="true"></i>{label}</span>'
        cls = token
        return f'<span><i class="plot-line {cls}" aria-hidden="true"></i>{label}</span>'
    body = re.sub(r'<span style="color:(#[0-9a-fA-F]{6})">([●━]) (.*?)</span>', legend_span, body)

    # Header: hero + schedule + notes.
    h1 = re.search(r'<h1>(.*?)</h1>', header).group(1)
    header = header.replace(f'<h1>{h1}</h1>', '', 1)
    first = re.match(r'<p><strong>(.*?)</strong>\s*(.*?)</p>', header, re.S)
    chips, lede = [], ''
    if first:
        chips = [c.strip().rstrip('.') for c in first.group(1).split('·')]
        lede = first.group(2)
        header = header[first.end():]
    header = re.sub(r'<nav class="nav".*?</nav>', '', header, flags=re.S)
    legend = re.search(r'<div class="legend">(.*?)</div>', header, re.S)
    header = header.replace(legend.group(0), '') if legend else header
    header = re.sub(r'<table>(.*?)</table>',
                    lambda m: f'<div class="table-wrap"><table>{m.group(1)}</table></div>', header, flags=re.S)

    legend_items = []
    if legend:
        for m in re.finditer(r'<span><span class="swatch( dash)?" style="border-color:(#[0-9a-fA-F]{6})"></span>(.*?)</span>',
                             legend.group(1)):
            token = SERIES.get(m.group(2).lower(), '')
            label = m.group(3)
            if m.group(1):
                icon = f'<i class="plot-mark {token}"></i><i class="plot-line {token}"></i>'
            else:
                icon = f'<i class="plot-line {token}"></i>'
            legend_items.append(f'<span>{icon}{label}</span>')
        if 'Hollow marks' in legend.group(1):
            legend_items.append('<span><i class="plot-mark hollow"></i>Hollow marks: excluded diagnostics</span>')

    rows = []
    for cid, cname, prims in cats:
        rows.append(f'<tr class="group"><td colspan="3">{cname}</td></tr>')
        for pid, name, price, basis in prims:
            rows.append(f'<tr><td><a href="#{pid}">{name}</a></td><td class="pcol"><code>{price}</code></td>'
                        f'<td class="basis"><code>{basis}</code></td></tr>')

    sidebar = ['<aside class="sidebar" aria-label="Primitives"><h4>Report</h4><a href="#overview"><span>Overview</span></a>']
    for cid, cname, prims in cats:
        sidebar.append(f'<h4>{cname}</h4>')
        sidebar += [f'<a href="#{pid}"><span class="p">{name}</span><span class="v">{html.escape(strip_tags(price))}</span></a>'
                    for pid, name, price, _ in prims]
    sidebar.append('</aside>')

    tabs = ''.join(f'<a href="#{cid}">{cname}</a>' for cid, cname, _ in cats)
    ver = re.search(r'\d+\.\d+\.\d+', title)

    out = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">', MARKER,
        f'<title>{title}</title><style>{CSS}</style></head><body>',
        '<div class="topbar"><div class="brand">Varops calibration',
        f'<span class="ver">{ver.group(0) if ver else ""}</span></div>' if ver else '</div>',
        f'<nav class="tabs" aria-label="Primitive categories">{tabs}</nav>',
        f'<button class="theme-btn" type="button" aria-label="Toggle dark mode" title="Toggle dark mode">{THEME_ICON}</button></div>',
        '<div class="layout">', ''.join(sidebar), '<main>',
        '<header id="overview"><div class="card hero">',
        f'<h1>{h1}</h1>',
        '<div class="chips">' + ''.join(f'<span class="chip">{c}</span>' for c in chips) + '</div>' if chips else '',
        f'<p class="lede">{lede}</p>' if lede else '',
        '</div>',
        '<div class="card"><div class="card-title">Candidate price schedule</div>',
        '<div class="table-wrap"><table class="schedule"><thead><tr><th>Primitive</th><th>Rounded candidate (varops)</th>',
        '<th>Unrounded pricing basis (envelope)</th></tr></thead><tbody>', ''.join(rows), '</tbody></table></div>',
        f'<p class="footnote">{W_NOTE} {REPEATED_NOTE}</p></div>',
        '<div class="card"><div class="card-title">Legend</div><div class="legend-grid">', ''.join(legend_items), '</div></div>'
        if legend_items else '',
        f'<div class="card prose"><div class="card-title">Calibration notes</div>{header}</div>',
        '</header>', body, '</main></div>', SCRIPT, '</body></html>',
    ]
    doc = ''.join(out)
    return re.sub(r'\x00SVG(\d+)\x00', lambda m: svgs[int(m.group(1))], doc)


def main():
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__)
    path = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) == 3 else path
    orig = path.with_name(path.stem + '.orig.html')
    src = path.read_text(encoding='utf-8')
    if MARKER in src:
        if not orig.exists():
            sys.exit(f'{path} is already restyled and {orig} is missing')
        src = orig.read_text(encoding='utf-8')
    elif out == path:
        shutil.copy2(path, orig)
    out.write_text(restyle(src), encoding='utf-8')
    print(out)


if __name__ == '__main__':
    main()
