from __future__ import annotations

from html import escape


def dashboard_html(host_name: str) -> str:
    name = escape(host_name)
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Host Sandbox — {name}</title>
<style>
:root{{color-scheme:dark;--bg:#0b0d10;--panel:#13171c;--panel2:#0f1317;--text:#e9eef4;--muted:#96a0ad;--line:#27303a;--ok:#63d391;--warn:#f0c36a;--bad:#ff7a85;--accent:#7ab7ff}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}}
main{{max-width:1500px;margin:auto;padding:18px}} header{{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:15px}}
h1{{font:700 23px/1.2 system-ui,sans-serif;margin:0 0 5px}} .sub{{color:var(--muted)}} .pill{{border:1px solid var(--line);border-radius:999px;padding:6px 10px;background:var(--panel)}}
.host{{color:var(--accent)}} .danger{{color:var(--warn)}} .grid{{display:grid;grid-template-columns:repeat(4,minmax(150px,1fr));gap:10px;margin-bottom:12px}}
.card,.panel{{background:var(--panel);border:1px solid var(--line);border-radius:10px}} .card{{padding:12px}} .label{{font:12px system-ui,sans-serif;color:var(--muted);text-transform:uppercase;letter-spacing:.07em}} .value{{font-size:19px;margin-top:5px;overflow-wrap:anywhere}}
.columns{{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(320px,.55fr);gap:12px}} .panel h2{{font:600 14px system-ui,sans-serif;margin:0;padding:10px 12px;border-bottom:1px solid var(--line)}}
.toolbar{{display:flex;gap:8px;align-items:center;padding:8px 10px;border-bottom:1px solid var(--line);flex-wrap:wrap}} button{{background:#1b232c;color:var(--text);border:1px solid #34414f;border-radius:7px;padding:6px 9px;cursor:pointer}} button:hover{{border-color:#596b7f}} input{{background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:6px 8px;min-width:180px}}
.events{{max-height:680px;overflow:auto}} .event{{display:grid;grid-template-columns:86px 110px 1fr;gap:8px;padding:8px 10px;border-bottom:1px solid #1d242c}} .event:last-child{{border:0}} .time,.kind{{color:var(--muted)}} .summary{{overflow-wrap:anywhere}} .event.ok .kind{{color:var(--ok)}} .event.error .kind{{color:var(--bad)}} .event.running .kind{{color:var(--warn)}} details{{margin-top:4px}} pre{{white-space:pre-wrap;overflow-wrap:anywhere;margin:5px 0 0;color:#b8c1cc;font-size:12px}}
.jobs{{padding:8px}} .job{{padding:9px;border:1px solid var(--line);border-radius:8px;margin-bottom:8px;background:var(--panel2)}} .jobtop{{display:flex;justify-content:space-between;gap:8px}} .cmd{{margin-top:5px;white-space:pre-wrap;overflow-wrap:anywhere}} .small{{font-size:12px;color:var(--muted)}} .oktxt{{color:var(--ok)}} .badtxt{{color:var(--bad)}} .runtime{{color:var(--warn)}}
footer{{color:var(--muted);font:12px system-ui,sans-serif;margin-top:12px}} @media(max-width:900px){{.grid{{grid-template-columns:repeat(2,1fr)}}.columns{{grid-template-columns:1fr}}}} @media(max-width:500px){{main{{padding:10px}}.grid{{grid-template-columns:1fr}}.event{{grid-template-columns:70px 88px 1fr;font-size:12px}}}}
</style>
</head>
<body><main>
<header><div><h1>Host Sandbox <span class="host" id="hostName">{name}</span></h1><div class="sub">Live observable tool activity on the real host OS</div></div><div class="pill danger">HOST MODE · NOT CONTAINERIZED</div></header>
<section class="grid">
 <div class="card"><div class="label">Connection</div><div class="value" id="connection">starting</div></div>
 <div class="card"><div class="label">Tool calls</div><div class="value" id="calls">0</div></div>
 <div class="card"><div class="label">Running jobs</div><div class="value" id="running">0</div></div>
 <div class="card"><div class="label">Uptime</div><div class="value" id="uptime">0s</div></div>
</section>
<section class="columns">
 <div class="panel"><h2>Activity</h2><div class="toolbar"><button id="pause">Pause</button><button id="clear">Clear view</button><label class="small">filter <input id="filter" placeholder="command, path, tool…"></label></div><div id="events" class="events" aria-live="polite"></div></div>
 <div class="panel"><h2>Command jobs</h2><div class="jobs" id="jobs"></div></div>
</section>
<footer>Shows tool calls, file operations, commands, job output/status and errors. It does not expose hidden model reasoning.</footer>
<script>
(()=>{{
const $=s=>document.querySelector(s), ev=$('#events'), jobs=$('#jobs'); let paused=false, last=0, rows=[];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
const dur=s=>{{s=Math.max(0,Math.floor(s)); if(s<60)return s+'s'; if(s<3600)return Math.floor(s/60)+'m '+s%60+'s'; return Math.floor(s/3600)+'h '+Math.floor(s%3600/60)+'m'}};
function renderEvents(){{const q=$('#filter').value.toLowerCase(); ev.innerHTML=rows.filter(x=>!q||JSON.stringify(x).toLowerCase().includes(q)).map(x=>{{const t=new Date(x.ts*1000).toLocaleTimeString(); const d=Object.keys(x.data||{{}}).length?`<details><summary class="small">details</summary><pre>${{esc(JSON.stringify(x.data,null,2))}}</pre></details>`:'';return `<div class="event ${{esc(x.status)}}"><div class="time">${{esc(t)}}</div><div class="kind">${{esc(x.kind)}}</div><div class="summary">${{esc(x.summary)}}${{d}}</div></div>`}}).join(''); if(!paused)ev.scrollTop=ev.scrollHeight}}
function renderJobs(list){{jobs.innerHTML=list.length?list.map(j=>`<div class="job"><div class="jobtop"><b>${{esc(j.id)}}</b><span class="${{j.status==='running'?'runtime':j.exit_code===0?'oktxt':'badtxt'}}">${{esc(j.status)}}${{j.exit_code==null?'':' · '+j.exit_code}}</span></div><div class="cmd">${{esc(j.command)}}</div><div class="small">pid ${{esc(j.pid)}} · ${{dur(j.duration_seconds||0)}} · ${{esc(j.cwd)}}</div>${{j.output_tail?`<details><summary class="small">output tail</summary><pre>${{esc(j.output_tail)}}</pre></details>`:''}}</div>`).join(''):'<div class="small">No command jobs yet.</div>'}}
async function tick(){{try{{const [s,e,j]=await Promise.all([fetch('/api/status').then(r=>r.json()),fetch('/api/events?after='+last).then(r=>r.json()),fetch('/api/jobs').then(r=>r.json())]); $('#connection').textContent='connected'; $('#connection').className='value oktxt'; $('#calls').textContent=s.tool_calls; $('#running').textContent=s.running_jobs; $('#uptime').textContent=dur(s.uptime_seconds); $('#hostName').textContent=s.host_name; if(!paused&&e.events.length){{rows.push(...e.events); if(rows.length>800)rows=rows.slice(-800); last=e.events[e.events.length-1].id; renderEvents()}} renderJobs(j.jobs||[])}}catch(err){{$('#connection').textContent='disconnected';$('#connection').className='value badtxt'}}}}
$('#pause').addEventListener('click',()=>{{paused=!paused;$('#pause').textContent=paused?'Resume':'Pause';if(!paused)renderEvents()}}); $('#clear').addEventListener('click',()=>{{rows=[];renderEvents()}}); $('#filter').addEventListener('input',renderEvents); tick(); setInterval(tick,1000);
}})();
</script></main></body></html>'''
