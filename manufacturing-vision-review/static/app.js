const $ = id => document.getElementById(id);
let data, selected = 6, busy = false, oldReport, selectedReport;
const name = n => `P-${String(n).padStart(3, '0')}`;
const safe = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const reviewed = p => p.reviewed_defective !== null;
const time = value => new Date(value).toLocaleTimeString('en-GB', {timeZone:'UTC', hour:'2-digit', minute:'2-digit'});
const pct = value => value === null ? '—' : `${value}%`;
const plural = (count, singular, multiple = singular+'s') => count === 1 ? singular : multiple;

function mark(appearance, scale = 1) {
  const marks = {
    clean:'',
    crack:'<path d="M-42 -24 L-17 -7 L-7 -13 L6 5 L25 13 L38 32 M6 5 L10 -10 L23 -24" fill="none" stroke="#24362f" stroke-width="3"/>',
    hairline:'<path d="M-35 -10 L-14 -4 L-6 -7 L8 3 L19 4 L33 14 M8 3 L15 -8" fill="none" stroke="#475b50" stroke-width="1.2"/>',
    scratch:'<path d="M-38 16 Q0 -2 38 -19 M-36 19 Q0 1 39 -16" fill="none" stroke="#f3f7f0" stroke-width="2"/>',
    dust:'<g fill="#aca28b" opacity=".9"><ellipse cx="-12" cy="4" rx="14" ry="7"/><ellipse cx="9" cy="-3" rx="10" ry="5"/><circle cx="25" cy="9" r="3"/><circle cx="-29" cy="-8" r="3"/></g>',
  };
  return `<g transform="scale(${scale})">${marks[appearance] || ''}</g>`;
}
function partSVG(p, large = false) {
  if (!large) return `<svg viewBox="0 0 100 65" aria-hidden="true"><rect x="14" y="9" width="72" height="47" rx="8" fill="#bfd0c0" stroke="#748f7c"/><circle cx="25" cy="21" r="4" fill="#eef3e9"/><circle cx="75" cy="44" r="4" fill="#eef3e9"/><g transform="translate(50 33)">${mark(p.appearance,.42)}</g></svg>`;
  return `<svg viewBox="0 0 600 320" role="img" aria-label="Illustrated metal part ${name(p.number)}: ${{clean:'unmarked surface',crack:'branching crack',hairline:'fine branching crack',scratch:'light surface scratch',dust:'removable dust'}[p.appearance]}, with a magnified detail"><defs><linearGradient id="metal" x2="1" y2="1"><stop stop-color="#d4ddd1"/><stop offset=".5" stop-color="#a8bcab"/><stop offset="1" stop-color="#c6d2c2"/></linearGradient></defs><g stroke="#31483c" stroke-width="1"><path d="M20 60H580 M20 120H580 M20 180H580 M20 240H580 M80 25V295 M160 25V295 M240 25V295 M320 25V295 M400 25V295 M480 25V295 M560 25V295"/></g><text x="24" y="30" fill="#97b5a0" font-size="11" font-family="monospace">CAM 01 / ${name(p.number)} / SURFACE INSPECTION</text><rect x="48" y="79" width="288" height="171" rx="22" fill="url(#metal)" stroke="#e0e8d9" stroke-width="2"/><g fill="#20372c" stroke="#8ba593" stroke-width="4"><circle cx="77" cy="108" r="12"/><circle cx="307" cy="108" r="12"/><circle cx="77" cy="221" r="12"/><circle cx="307" cy="221" r="12"/></g><g transform="translate(197 165)">${mark(p.appearance)}</g><rect x="148" y="118" width="98" height="93" rx="4" stroke="#e6c573" stroke-dasharray="4 3" fill="none"/><path d="M246 118 L407 107 M246 211 L407 225" stroke="#cfb568" opacity=".7"/><circle cx="472" cy="165" r="72" fill="url(#metal)" stroke="#e6c573" stroke-width="2"/><g transform="translate(472 165)">${mark(p.appearance,1.6)}</g><text x="416" y="270" fill="#dcc789" font-size="11" font-family="monospace">MAGNIFIED DETAIL</text><text x="24" y="302" fill="#97b5a0" font-size="10" font-family="monospace">ILLUSTRATIVE IMAGE · NOT A REAL CAMERA FEED</text></svg>`;
}
async function request(path, method = 'GET', body) {
  const response = await fetch(path, {method, headers:{'Content-Type':'application/json'}, ...(body ? {body:JSON.stringify(body)} : {})});
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'The request could not be completed.');
  return result;
}
function showError(error) { $('error').textContent=error.message; $('error').hidden=false; }
function notify(message) { $('notice').textContent=message; $('notice').hidden=false; }
async function action(kind, defective) {
  if (busy) return;
  busy=true;
  $('error').hidden=true;
  document.querySelectorAll('button').forEach(b=>b.disabled=true);
  try {
    const base=data ? `/api/runs/${data.run_id}` : '';
    if (kind==='replay') {
      selectedReport=$('report-select').value;
      oldReport=await request(`${base}/reports/${selectedReport}`);
      renderComparison();
      $('comparison').scrollIntoView({behavior:'smooth',block:'nearest'});
      return;
    }
    const path=kind==='new'?'/api/runs':kind==='review'?`${base}/parts/${selected}/review`:kind==='save'?`${base}/reports`:`${base}/complete`;
    data=await request(path,'POST',kind==='review'?{defective}:undefined);
    if (kind==='new') {
      selected=6;oldReport=undefined;selectedReport=undefined;
      history.replaceState(null,'',`?batch=${data.run_id}`);
      localStorage.setItem('xtdb-vision-batch',data.run_id);
      $('notice').hidden=true;
    } else if (kind==='save') {
      selectedReport=data.reports.at(-1)._id;oldReport=undefined;
      notify('Report saved. Revise a review, then reproduce this report to recover the earlier figures.');
    } else if (kind==='complete') {
      notify('The remaining parts have been reviewed using the scripted scenario. Your existing reviews were preserved.');
    } else {
      const p=data.parts.find(p=>p.number===selected);
      notify(`${name(selected)} recorded as ${defective?'defective':'acceptable'}. ${outcome(p)} The analytics below now include this assessment.`);
    }
    render();
  } catch(error) {showError(error);}
  finally {busy=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);}
}
function outcome(p) {
  if (!reviewed(p)) return 'This part has not been reviewed yet.';
  if (p.predicted_defective && !p.reviewed_defective) return 'False positive: the model rejected a part you consider acceptable.';
  if (!p.predicted_defective && p.reviewed_defective) return 'False negative: the model accepted a part you consider defective.';
  return p.reviewed_defective?'The model and the latest review agree: this part is defective.':'The model and the latest review agree: this part is acceptable.';
}
function render() {
  $('parts').innerHTML=data.parts.map(p=>`<button class="part ${p.predicted_defective?'reject':''} ${selected===p.number?'selected':''}" data-part="${p.number}" aria-pressed="${selected===p.number}" aria-label="${name(p.number)}, model ${p.predicted_defective?'rejected':'accepted'}, ${reviewed(p)?'reviewed':'unreviewed'}">${partSVG(p)}<span>${name(p.number)}</span><small>${reviewed(p)?'✓ reviewed':'not reviewed'}</small></button>`).join('');
  renderPart();renderGuide();renderAnalytics();
  selectedReport=selectedReport || data.reports[0]._id;
  $('report-select').innerHTML=data.reports.map((r,i)=>`<option value="${r._id}" ${r._id===selectedReport?'selected':''}>${i+1}. ${safe(r.title)} · ${time(r.recorded_at)}</option>`).join('');
  $('sql').textContent=data.sql;
  renderComparison();
}
function renderPart() {
  const p=data.parts.find(p=>p.number===selected);
  $('part-title').textContent=`${name(p.number)} · inspect the surface`;
  $('part-time').textContent=`Produced ${time(p.produced_at)} UTC`;
  $('image').innerHTML=partSVG(p,true);
  $('prediction').innerHTML=`<div class="prediction-label ${p.predicted_defective?'reject':''}">${p.predicted_defective?'Defective → held':'Acceptable → released'}</div><div class="prediction-meta"><div>Model<b>${p.model_version}</b></div><div>Defect score<b>${Number(p.defect_score).toFixed(2)} / 1.00</b></div><div>Original action<b>${p.line_action==='held'?'Held in stock':'Released into stock'}</b></div></div>`;
  $('review-status').textContent=reviewed(p)?`Latest review: ${p.reviewed_defective?'defective':'acceptable'} (${p.review_source.toLowerCase()}). You can revise this judgement.`:'Inspect the image, then record your assessment.';
  $('outcome').textContent=reviewed(p)?outcome(p):'';
  $('outcome').className=`outcome ${reviewed(p)&&p.predicted_defective!==p.reviewed_defective?'mismatch':''}`;
}
function renderGuide() {
  const six=data.parts.find(p=>p.number===6),five=data.parts.find(p=>p.number===5);
  let message,button;
  if (!reviewed(six)) {
    message='<b>Start with P-006.</b> The model rejected it. Inspect the mark: is it a crack or a light surface scratch? Your review will tell us whether the model was right.';
    button=selected!==6?'<button data-part="6">Inspect P-006</button>':'';
  } else if (!reviewed(five)) {
    message='<b>Now inspect an accepted part: P-005.</b> Reviewing only rejected parts would miss defects the model allowed through. Look closely at the magnified detail.';
    button=selected!==5?'<button data-part="5">Inspect P-005</button>':'';
  } else if(data.metrics.unreviewed) {
    message='<b>Both sides of the inspection matter.</b> You have reviewed an accepted and a rejected part. Inspect the other parts yourself, or complete the remaining reviews with the scripted scenario.';
    button=`<button data-action="complete">Complete ${data.metrics.unreviewed} remaining reviews</button>`;
  } else {
    message='<b>Every part now has a review.</b> Compare the two quality views below, then reproduce the initial report. You can also save a report and revise a review to see how XTDB preserves the earlier assessment.';
    button='';
  }
  $('guide').innerHTML=`<p>${message}</p>${button}`;
}
function renderAnalytics() {
  const m=data.metrics;
  $('coverage').textContent=`${m.reviewed} of ${m.total} parts reviewed · ${m.unreviewed} unresolved`;
  $('quality').innerHTML=`<div class="metric-row"><div><div class="big">${m.defects}<small> / ${m.reviewed}</small></div><div class="metric-label">reviewed parts judged defective</div></div><div class="secondary"><b>${m.acceptable}</b> reviewed parts acceptable<br><b>${m.unreviewed}</b> parts still unreviewed<br><b>${m.model_rejected}</b> originally rejected by the model</div></div>`;
  $('quality-scope').textContent=m.unreviewed?`These are findings from ${m.reviewed} reviewed parts, not an estimated defect rate for the whole production line. ${m.unreviewed} parts still have unknown outcomes.`:`All ${m.total} parts in this batch have been reviewed. ${pct(m.defect_pct)} are currently judged defective. This describes this synthetic batch, not a wider production process.`;
  $('model').innerHTML=`<div class="metric-row"><div><div class="big">${pct(m.agreement_pct)}</div><div class="metric-label">accuracy on ${m.reviewed} reviewed parts</div></div><div class="secondary"><b>${m.false_negative}</b> ${plural(m.false_negative,'missed defect')}<br><b>${m.false_positive}</b> ${plural(m.false_positive,'unnecessary rejection')}</div></div>`;
  $('matrix').innerHTML=[['true_positive','Defects caught','Model rejected · human says defective',false],['false_negative','Defects missed','Model accepted · human says defective',true],['false_positive','Unnecessary rejections','Model rejected · human says acceptable',true],['true_negative','Correct acceptances','Model accepted · human says acceptable',false]].map(([key,title,description,mistake])=>`<div class="cell ${mistake?'mistake':''}"><strong>${m[key]}</strong><span>${title}</span><small>${description}</small></div>`).join('');
}
function renderComparison() {
  $('comparison').hidden=!oldReport;
  if (!oldReport) return;
  const a=oldReport.metrics,b=data.metrics;
  const fields=[['Parts reviewed','reviewed',v=>`${v} / ${b.total}`],['Defects found','defects',String],['Model accuracy on reviewed parts','agreement_pct',pct],['Missed defects','false_negative',String],['Unnecessary rejections','false_positive',String]];
  const changedReviews=data.parts.filter(p=>p.reviewed_defective!==oldReport.parts.find(old=>old.number===p.number)?.reviewed_defective).length;
  $('comparison').innerHTML=`<table><thead><tr><th>For the same 12 parts</th><th>${safe(oldReport.report.title)}</th><th>Known now</th></tr></thead><tbody>${fields.map(([label,key,format])=>`<tr><td>${label}</td><td>${format(a[key])}</td><td class="${a[key]!==b[key]?'changed':''}">${format(b[key])}</td></tr>`).join('')}</tbody></table><p class="history-note">${changedReviews?`${changedReviews} ${plural(changedReviews,'part assessment differs','part assessments differ')} between these snapshots. The model has not changed; later or revised human reviews changed what we know about its predictions.`:'These snapshots currently contain the same part assessments. Record or revise a review to see the figures diverge.'} The earlier figures were recomputed from XTDB’s historical data.</p><p class="basis">Saved database snapshot: ${safe(oldReport.report.basis)}</p><details><summary>Show the historical query that just ran</summary><pre>${safe(oldReport.sql)}\n\nBatch parameter: ${safe(data.run_id)}</pre></details>`;
}
document.addEventListener('click',event=>{
  const button=event.target.closest('button');
  if (!button || busy) return;
  if (button.dataset.part) {selected=Number(button.dataset.part);render();}
  if (button.dataset.action) action(button.dataset.action);
});
$('acceptable').onclick=()=>action('review',false);
$('defective').onclick=()=>action('review',true);
$('new').onclick=()=>action('new');
$('save').onclick=()=>action('save');
$('replay').onclick=()=>action('replay');
$('report-select').onchange=()=>{selectedReport=$('report-select').value;oldReport=undefined;renderComparison();};
async function init() {
  const id=new URLSearchParams(location.search).get('batch') || localStorage.getItem('xtdb-vision-batch');
  if(!id) return action('new');
  busy=true;
  document.querySelectorAll('button').forEach(b=>b.disabled=true);
  try {data=await request(`/api/runs/${encodeURIComponent(id)}`);history.replaceState(null,'',`?batch=${data.run_id}`);render();}
  catch(error){showError(error);}
  finally {busy=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);}
}
init();
