const $ = id => document.getElementById(id);
let data, busy = false, running = true, selected = 'P-007', filter = 'all', replayResult;
let renderedStage, renderedRun;
const mm = value => value == null ? 'unknown' : Number(value).toFixed(3);
const time = value => new Date(value).toLocaleTimeString('en-GB', {timeZone:'UTC', hour:'2-digit', minute:'2-digit'});
const safe = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const disposition = value => ({released:'Released', held:'Held', quality_hold:'Quality hold'}[value]);
async function request(path, method = 'GET', body) {
  const response = await fetch(path, {method, headers: {'Content-Type':'application/json'}, ...(body ? {body:JSON.stringify(body)} : {})});
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || 'Request failed');
  return result;
}
async function act(action) {
  if (busy) return;
  busy = true;
  $('error').hidden = true;
  document.querySelectorAll('button[data-action]').forEach(b => b.disabled = true);
  try {
    const path = action === 'new' ? '/api/runs' : `/api/runs/${data.run_id}/${action}`;
    data = await request(path, 'POST', action === 'tick' ? {expected_sequence:data.next_sequence} : undefined);
    if (action === 'new') {
      selected = 'P-007'; filter = 'all'; replayResult = undefined;
      history.replaceState(null, '', `?shift=${data.run_id}`);
      localStorage.setItem('xtdb-live-shift', data.run_id);
    }
    if (action === 'reassess') replayResult = undefined;
    render();
  } catch (error) { showError(error); }
  finally { busy = false; document.querySelectorAll('button[data-action]').forEach(b => b.disabled = false); }
}
function showError(error) { $('error').textContent = error.message; $('error').hidden = false; }
function render() {
  const incident = data.incident, stage = incident?.stage;
  const step = !stage ? 0 : stage === 'reported' ? 1 : stage === 'assessed' ? 2 : 3;
  $('released').textContent = data.counts.released;
  $('held').textContent = data.counts.held;
  const latest = data.recent.at(-1);
  $('measurement').textContent = `${latest.pixel_width} px → ${mm(latest.width_mm)} mm`;
  $('verdict').textContent = `${latest.part} · ${latest.assessment === 'pass' ? 'Pass' : 'Hold'} · ${time(latest.inspected_at)}`;
  $('verdict').className = `verdict ${latest.assessment === 'pass' ? '' : 'fail'}`;
  $('recent').innerHTML = data.recent.map(p => `<div class="recent-part ${p.disposition}"><b>${p.part}</b><span>${time(p.inspected_at)} · ${mm(p.width_mm)} mm</span><span>${disposition(p.disposition)}</span></div>`).join('');
  if (renderedRun !== data.run_id || renderedStage !== stage) {
    renderedRun = data.run_id;
    renderedStage = stage;
    $('step-number').textContent = `0${step+1} / 04`;
    $('steps').innerHTML = ['Discover the problem','Find & reassess','Hold affected stock','Explain the decision'].map((s,i) => `<div class="step ${i<=step ? 'active' : ''}">${i+1}. ${s}</div>`).join('');
    $('step-title').textContent = ['An earlier measurement may be wrong','12 parts need a closer look','Five released parts no longer pass','The stock is held. The history is intact.'][step];
    const messages = [
      '<p>A routine check reveals that the camera’s calibration was wrong <b>between 09:00 and 11:00</b>. The correct conversion was <b>0.051 mm per pixel</b>, rather than 0.050. Report that discovery to begin the investigation.</p><button data-action="report">Report calibration problem</button>',
      '<p>These <b>12 parts</b> were measured during the affected period. Their original decisions are shown below. Apply the corrected conversion to find out which results change.</p><button data-action="reassess">Apply correction & reassess</button>',
      `<p><b>${data.counts.review} released parts now fail</b> the width check. One previously held part now passes, and six results are unchanged. Place the five affected releases on hold so they cannot leave stock.</p><button data-action="contain">Place ${data.counts.review} released parts on hold</button>`,
      `<p><b>${data.counts.quality_holds} new quality holds are recorded.</b> The original releases remain in the history. Select a part and use “Why was this released?” to reproduce the assessment behind its original decision.</p>`
    ];
    $('guidance').innerHTML = messages[step];
    $('candidates').hidden = !incident;
    if (incident) renderCandidates();
    renderReplay();
  }
  $('sql').textContent = data.sql;
  $('control').hidden = !(step >= 2 && data.control);
  if (!$('control').hidden) $('control').textContent = `Outside the affected period: P-019 was inspected at 11:00. Its 201 px reading still gives 10.050 mm and passes. P-007 has the same reading, but was inspected at 09:00, so the correction applies to it.`;
}
function renderCandidates() {
  const assessed = data.incident.stage !== 'reported';
  $('candidate-title').textContent = assessed ? 'Select a part to compare its two assessments' : 'Parts measured from 09:00 to 11:00';
  $('filters').hidden = !assessed;
  $('filters').innerHTML = [['all','All 12'],['review',`Released, now fail · ${data.counts.review}`],['now_passes',`Held, now pass · ${data.counts.now_passes}`],['unchanged',`Unchanged · ${data.counts.unchanged}`]].map(([key,label]) => `<button data-filter="${key}" class="${filter===key?'selected':''}" aria-pressed="${filter===key}">${label}</button>`).join('');
  const visible = data.candidates.filter(p => filter==='all'||p.group===filter);
  if (!visible.some(p => p.part===selected)) { selected=visible[0]?.part; replayResult=undefined; }
  $('parts').innerHTML = visible.map(p => `<button data-part="${p.part}" class="part ${p.group} ${p.part===selected?'selected':''}" aria-pressed="${p.part===selected}"><b>${p.part}<small>${time(p.inspected_at)}</small></b><span>${mm(p.decision.width_mm)} mm${assessed ? ` → ${mm(p.width_mm)}` : ''}</span><span>${assessed ? ({review:p.hold?'Quality hold':'Released → needs review',now_passes:'Now passes · still held',unchanged:'Result unchanged'}[p.group]) : disposition(p.disposition)}</span></button>`).join('');
  const p = visible.find(p => p.part===selected);
  if (!p) return;
  $('detail').innerHTML = `<div class="detail-top"><h3>${p.part} · inspected at ${time(p.inspected_at)}</h3><button class="quiet" data-replay="${Number(p.part.slice(2))}">${p.decision.released?'Why was this released?':'Why was this held?'}</button></div><div class="comparison"><div class="calculation"><span>ORIGINAL ASSESSMENT</span><strong>${p.pixel_width} px × 0.050 = ${mm(p.decision.width_mm)} mm</strong><small>${p.decision.assessment.toUpperCase()} · ${p.decision.released?'Released into stock':'Held in stock'}</small></div><div class="arrow">→</div><div class="calculation"><span>${assessed?'WITH THE CORRECTION':'AWAITING REASSESSMENT'}</span><strong>${assessed?`${p.pixel_width} px × ${Number(p.mm_per_pixel).toFixed(3)} = ${mm(p.width_mm)} mm`:'Same camera reading'}</strong><small>${assessed?`${p.assessment.toUpperCase()} · acceptable: 9.800–10.200 mm`:'The original reading will be checked again.'}</small></div></div><p>${!assessed ? 'Being in the affected period makes this part a candidate. Reassessment will determine whether its result changes.' : p.group==='review' ? (p.hold ? 'This part is now on quality hold. Its original release is still recorded, along with the new hold.' : 'This part was released, but its corrected width is outside tolerance. It needs a quality hold.') : p.group==='now_passes' ? 'The corrected width passes. This part remains held until someone authorises its release; reassessment does not release it automatically.' : 'The corrected measurement gives the same pass/fail result, so this investigation does not change its stock status.'}</p>`;
}
function renderReplay() {
  $('replay').hidden = !replayResult;
  if (!replayResult) return;
  const r=replayResult;
  $('replay').innerHTML = `<h3>${r.original.part}: the original decision, reproduced</h3><p>At the time of the decision, the database held a conversion of <b>${Number(r.original.mm_per_pixel).toFixed(3)} mm per pixel</b> for this inspection. The same query against that earlier snapshot returns <b>${mm(r.original.width_mm)} mm</b>: ${r.original.assessment==='pass'?'within':'outside'} the accepted range. That explains why the part was ${r.decision.released?'released':'held'}.</p><p>The current assessment gives <b>${mm(r.current.width_mm)} mm</b> (${r.current.assessment}). ${r.current.width_mm !== r.original.width_mm?'The later correction changes what we know about the part without erasing the original decision.':'Both snapshots currently give the same result.'}</p><p class="time">Original database snapshot: ${safe(r.decision.basis)}</p><details><summary>Show the historical query that just ran</summary><pre>${safe(r.sql)}\n\nParameters: ${safe(JSON.stringify(r.parameters))}</pre></details>`;
}
document.addEventListener('click', async event => {
  const button = event.target.closest('button');
  if (!button) return;
  if (button.dataset.action) return act(button.dataset.action);
  if (button.dataset.filter) {filter=button.dataset.filter;renderCandidates();renderReplay();}
  if (button.dataset.part) {selected=button.dataset.part;replayResult=undefined;renderCandidates();renderReplay();}
  if (button.dataset.replay) {
    if (busy) return;
    busy=true;button.disabled=true;
    try { replayResult=await request(`/api/runs/${data.run_id}/parts/${button.dataset.replay}/replay`);renderReplay();$('replay').scrollIntoView({behavior:'smooth',block:'nearest'}); }
    catch(error){showError(error);} finally{busy=false;button.disabled=false;}
  }
});
$('new').onclick=()=>act('new');
$('pause').onclick=()=>{running=!running;$('pause').textContent=running?'Pause line':'Resume line';$('production-title').textContent=running?'Production is running':'Production is paused';$('live').textContent=running?'● LIVE':'● PAUSED';$('live').classList.toggle('paused',!running);document.body.classList.toggle('paused-line',!running);};
async function init() {
  const id=new URLSearchParams(location.search).get('shift') || localStorage.getItem('xtdb-live-shift');
  if (id) {
    busy=true;
    try {data=await request(`/api/runs/${encodeURIComponent(id)}`);history.replaceState(null,'',`?shift=${data.run_id}`);render();}
    catch(error){showError(error);}finally{busy=false;}
  } else await act('new');
  setInterval(()=>{if(data&&running&&!document.hidden&&!busy) act('tick');},4000);
}
init();
