const $ = id => document.getElementById(id);
let data, selected, view = 'current', busy = false;
const escapeHtml = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const time = value => new Date(value).toISOString().slice(11, 16);
const width = value => value == null ? 'Unknown' : Number(value).toFixed(3);
const factor = value => value == null ? 'Unknown' : Number(value).toFixed(3);
const badge = assessment => `<span class="badge ${assessment}">${assessment === 'unknown' ? 'Unknown' : assessment === 'pass' ? 'Pass' : 'Fail'}</span>`;

function controls() {
  $('run').disabled = busy;
  $('release').disabled = busy || data?.stage !== 'inspected';
  $('correct').disabled = busy || data?.stage !== 'released';
  $('original').disabled = busy || !data?.release_basis;
  $('current').disabled = busy;
}

async function request(path, method = 'GET') {
  if (busy) return;
  busy = true;
  controls();
  $('error').hidden = true;
  $('progress').textContent = method === 'GET' ? 'Loading the saved batch…' : 'Writing to XTDB and reading the assessment…';
  try {
    const response = await fetch(path, {method});
    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.detail || 'The request failed.');
    }
    const previousRun = data?.run_id;
    data = await response.json();
    localStorage.setItem('inspection-run', data.run_id);
    history.replaceState(null, '', `?batch=${encodeURIComponent(data.run_id)}`);
    if (previousRun !== data.run_id) {
      selected = data.current[6].inspection_id;
      view = 'current';
    }
    if (method === 'POST') view = 'current';
    render(previousRun !== data.run_id);
  } catch (error) {
    $('error').textContent = error.message;
    $('error').hidden = false;
    $('progress').textContent = 'The action could not be completed. You can retry or run a new batch.';
  } finally {
    busy = false;
    controls();
  }
}

function render(animate = false) {
  $('empty').hidden = true;
  $('dashboard').hidden = false;
  $('run').innerHTML = '<span class="step-number">1</span><span>Run new batch<small>Keep this batch in history</small></span>';
  $('progress').textContent = {
    inspected:'Batch inspected. Release the passing parts to record the decisions and their database basis.',
    released:'Release decisions recorded. Now correct the calibration that applied from 09:00 up to 11:00.',
    corrected:`Calibration corrected. ${data.review_ids.length} released parts need review. Their original release records are preserved.`,
  }[data.stage];
  $('count').textContent = data.current.length;
  $('passed').textContent = data.current.filter(p => p.assessment === 'pass').length;
  $('released').textContent = data.releases.length;
  $('review').textContent = data.review_ids.length;
  $('review-card').classList.toggle('attention', data.review_ids.length > 0);
  $('current').setAttribute('aria-pressed', view === 'current');
  $('original').setAttribute('aria-pressed', view === 'original');
  $('view-note').textContent = view === 'original'
    ? 'Assessments reproduced at the saved release basis. Release records and review flags show the current operational position.'
    : 'Assessments use the latest recorded information. A release record preserves the action already taken.';
  const corrected = data.stage === 'corrected' && view === 'current';
  const timeline = view === 'original' ? data.original_timeline : data.timeline;
  const start = new Date(data.batch_start).getTime(), end = new Date(data.batch_end).getTime();
  $('timeline').innerHTML = timeline.map(interval => {
    const from = Math.max(start, new Date(interval.valid_from).getTime());
    const to = Math.min(end, interval.valid_to ? new Date(interval.valid_to).getTime() : end);
    if (to <= from) return '';
    const changed = Number(interval.mm_per_pixel) !== 0.050;
    return `<div class="${changed ? 'corrected' : ''}" style="flex:${to - from}">${factor(interval.mm_per_pixel)}${changed ? ' · corrected' : ''}</div>`;
  }).join('');
  $('timeline-note').textContent = corrected
    ? 'Recorded now, effective for inspections from 09:00 up to 11:00. Outside that interval, the calibration is unchanged.'
    : view === 'original' ? 'The calibration recorded when the release assessment was made.' : 'The original calibration applies throughout this batch.';
  const parts = view === 'original' ? data.original : data.current;
  const released = new Set(data.releases.map(r => r._id));
  $('parts').innerHTML = parts.map((p, index) => {
    const review = data.review_ids.includes(p.inspection_id);
    const release = released.has(p.inspection_id);
    return `<tr class="${p.inspection_id === selected ? 'selected' : ''} ${review ? 'review-row' : ''} ${animate ? 'new-row' : ''}" style="--index:${index}">
      <td><button class="part-button" data-part="${escapeHtml(p.inspection_id)}" aria-label="Inspect ${escapeHtml(p.part)}" aria-pressed="${p.inspection_id === selected}">${escapeHtml(p.part)}</button><span class="part-clock">${time(p.inspected_at)}</span></td>
      <td>${width(p.width_mm)} mm</td><td>${badge(p.assessment)}</td><td class="release-text ${review ? 'flagged' : ''}">${review ? 'Released · review' : release ? 'Released' : 'Not released'}</td></tr>`;
  }).join('');
  $('sql').textContent = view === 'original' ? data.original_sql : data.sql;
  $('parameter').textContent = `%s = ${data.run_id}`;
  renderPart();
  controls();
}

function renderPart() {
  const now = data.current.find(p => p.inspection_id === selected);
  const original = data.original.find(p => p.inspection_id === selected);
  const release = data.releases.find(r => r._id === selected);
  $('part-title').textContent = now.part;
  $('part-time').textContent = `${time(now.inspected_at)} UTC`;
  const calculation = (part, title) => `<div class="calculation"><div class="label"><span>${title}</span>${badge(part.assessment)}</div><div class="formula">${part.pixel_width} × ${factor(part.mm_per_pixel)}<br>= <strong>${width(part.width_mm)} mm</strong></div><p>${part.min_mm == null || part.max_mm == null ? 'Product tolerance unavailable.' : `Required width: ${Number(part.min_mm).toFixed(2)}–${Number(part.max_mm).toFixed(2)} mm, inclusive.`}</p></div>`;
  const review = data.review_ids.includes(selected);
  const message = review
    ? 'This part was released using the original calibration. Its corrected width is outside tolerance. It now needs review; the original release remains recorded.'
    : release ? 'This part was released and still passes the dimensional check with the current information.'
    : original ? 'This part was not released. The original decision remains unchanged.'
    : 'This is an inspection assessment. No release decision has been recorded yet.';
  $('part-detail').innerHTML = `<div class="observation"><span>CAMERA OBSERVATION · UNCHANGED</span><strong>${now.pixel_width} <small>pixels</small></strong></div>
    ${original ? calculation(original, 'At release') : ''}${calculation(now, 'Current assessment')}
    <p class="decision-note">${message}</p>${release ? `<p class="basis">Release recorded: ${escapeHtml(release.released_at)}<br>Assessment basis: ${escapeHtml(data.release_basis)}</p>` : ''}`;
}

$('run').addEventListener('click', () => request('/api/runs', 'POST'));
$('release').addEventListener('click', () => request(`/api/runs/${data.run_id}/release`, 'POST'));
$('correct').addEventListener('click', () => request(`/api/runs/${data.run_id}/correct`, 'POST'));
$('current').addEventListener('click', () => {view = 'current'; render();});
$('original').addEventListener('click', () => {view = 'original'; render();});
$('parts').addEventListener('click', event => {
  const button = event.target.closest('[data-part]');
  if (button) {selected = button.dataset.part; render(); document.querySelector(`[data-part="${CSS.escape(selected)}"]`).focus();}
});
const saved = new URLSearchParams(location.search).get('batch') || localStorage.getItem('inspection-run');
if (saved) request(`/api/runs/${encodeURIComponent(saved)}`);
