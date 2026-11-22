'use strict';
const $ = id => document.getElementById(id);
const state = {report: null, tab: 'Overview', busy: false, selected: '', selection: 0};
const sections = ['Overview', 'History', 'Companies', 'Sanctions', 'Sources'];
let savedReports = [];
function node(tag, text = '', cls = '') {
  const el = document.createElement(tag);
  if (text) el.textContent = text;
  if (cls) el.className = cls;
  return el;
}
function sourceLink(source) {
  const el = node('a', source?.label || 'Unknown source', 'source-link');
  try { const url = new URL(source.url); if (['https:', 'http:'].includes(url.protocol)) el.href = url.href; } catch (_) {}
  el.target = '_blank'; el.rel = 'noopener noreferrer';
  return el;
}
function dateText(value, short = false) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'Date unavailable';
  return new Intl.DateTimeFormat(undefined, short ? {day:'numeric', month:'short'} : {day:'numeric', month:'short', year:'numeric', hour:'2-digit', minute:'2-digit', timeZoneName:'short'}).format(date);
}
async function api(path, options) {
  const response = await fetch(path, options);
  let data;
  try { data = await response.json(); } catch (_) { throw new Error('The local server returned an unreadable response.'); }
  if (!response.ok) throw new Error(data.error || 'The request could not be completed.');
  return data;
}
function message(text) { $('page-message').textContent = text; $('page-message').hidden = !text; }
function formError(text) { $('form-error').textContent = text; $('form-error').hidden = !text; $('imo-input').setAttribute('aria-invalid', String(Boolean(text))); }
function busy(value) {
  state.busy = value;
  $('scan-button').disabled = value; $('imo-input').disabled = value; $('use-example').disabled = value;
  $('lookup-form').setAttribute('aria-busy', String(value));
  $('progress').hidden = !value;
}
function values(label) { return [...new Set(state.report.records.filter(r => r.label === label).map(r => r.value))]; }
function matches(record) {
  const query = $('filter').value.trim().toLowerCase();
  const source = state.report.sources[record.source]?.label || '';
  return `${record.label} ${record.value} ${record.note || ''} ${source}`.toLowerCase().includes(query);
}
function table(title, records) {
  const block = node('section', '', 'data-block');
  const heading = node('div', '', 'block-header'); heading.append(node('h3', title), node('span', `${records.length} observations`)); block.append(heading);
  const wrap = node('div', '', 'table-scroll'); const grid = node('table');
  const head = node('thead'); const tr = node('tr'); ['Field', 'Finding', 'Source'].forEach(text => { const th = node('th', text); th.scope = 'col'; tr.append(th); }); head.append(tr); grid.append(head);
  const body = node('tbody');
  for (const record of records) {
    const row = node('tr'); const value = node('td', record.value);
    if (record.note) value.append(node('span', record.note, 'record-note'));
    const source = node('td'); source.append(sourceLink(state.report.sources[record.source]));
    row.append(node('td', record.label), value, source); body.append(row);
  }
  grid.append(body); wrap.append(grid); block.append(wrap); return block;
}
function renderContent() {
  const content = $('result-content'); content.replaceChildren();
  const report = state.report; if (!report) return;
  const records = report.records.filter(matches);
  if (state.tab === 'Sources') {
    const grid = node('div', '', 'cards-grid'); const query = $('filter').value.trim().toLowerCase();
    Object.values(report.sources).filter(s => `${s.label} ${s.status} ${s.error || ''}`.toLowerCase().includes(query)).forEach(source => {
      const card = node('article', '', 'insight-card');
      const statusClass = source.status === 'collected' ? '' : (source.error ? ' unavailable' : ' neutral');
      card.append(node('span', source.status, 'source-status' + statusClass), node('h3', source.label));
      card.append(node('p', source.error || (source.status === 'collected' ? 'Public records were collected for this vessel.' : 'This source supports discovery or returned no matching public record.')));
      card.append(node('p', `Retrieved: ${dateText(source.retrieved)}${source.cached ? ' Â· from the six-hour cache' : ''}`, 'source-time'), sourceLink(source));
      grid.append(card);
    });
    if (grid.children.length) content.append(grid);
  } else if (state.tab === 'History') {
    const events = records.filter(r => r.label.startsWith('VesselFinder change'));
    if (events.length) {
      const timeline = node('div', '', 'timeline');
      for (const record of events) {
        const item = node('article', '', 'timeline-item');
        item.append(node('div', record.label.split('Â·').slice(1).join('Â·').trim(), 'timeline-date'), node('h3', record.value), node('p', record.note || ''), sourceLink(report.sources[record.source])); timeline.append(item);
      }
      content.append(timeline);
    }
    const other = records.filter(r => r.section === 'History' && !r.label.startsWith('VesselFinder change'));
    if (other.length) content.append(table('Historical identity records', other));
  } else {
    const groups = state.tab === 'Overview' ? [['Identity','Vessel identity'],['Specifications','Vessel particulars'],['Activity','Reported activity']] : state.tab === 'Companies' ? [['Companies','Ownership & management']] : [['Sanctions','Sanctions records'],['UK official record','Official UK record'],['Programs','Programs & cited resolutions']];
    for (const [section, title] of groups) { const group = records.filter(r => r.section === section); if (group.length) content.append(table(title, group)); }
  }
  if (!content.children.length) content.append(node('p', $('filter').value ? 'No findings match this filter.' : 'No public information was collected for this section. Check Sources for coverage details.', 'no-results'));
}
function renderReport() {
  const report = state.report;
  $('results').hidden = false;
  $('vessel-name').textContent = values('Reported vessel name').join(' / ') || `Vessel ${report.imo}`;
  $('vessel-meta').textContent = `IMO ${report.imo} Â· Collected ${dateText(report.generated_at)}`;
  const base = '/reports/' + report.report_id;
  $('html-report').href = base + '/report.html'; $('json-download').href = base + '/data.json';
  const collected = Object.values(report.sources).filter(s => s.status === 'collected').length;
  const metrics = [['Reported current flag', values('Current flag').join(' / ') || 'Unavailable', 'Source-reported identity'], ['MMSI', values('MMSI').join(' / ') || 'Unavailable', 'Reported radio identifier'], ['Observations', String(report.records.length), 'May overlap across sources'], ['Sources collected', String(collected), 'Public records retrieved']];
  $('metrics').replaceChildren();
  for (const [label, value, sub] of metrics) { const card = node('div', '', 'metric'); card.append(node('div', label, 'metric-label'), node('div', value, 'metric-value'), node('div', sub, 'metric-sub')); $('metrics').append(card); }
  $('warnings').replaceChildren();
  if (report.warnings?.length) {
    const details = node('details'); const conflicts = report.warnings.filter(w => /disagree|different reported/i.test(w));
    details.append(node('summary', conflicts.length ? `${conflicts.length} source ${conflicts.length > 1 ? 'discrepancies' : 'discrepancy'} to review Â· coverage notes` : 'Coverage notes Â· read before drawing conclusions'));
    const list = node('ul'); report.warnings.forEach(w => list.append(node('li', w))); details.append(list); $('warnings').append(details);
  }
  $('tabs').replaceChildren();
  sections.forEach(section => { const button = node('button', section, 'tab'); button.type = 'button'; button.setAttribute('aria-pressed', String(state.tab === section)); button.addEventListener('click', () => { state.tab = section; Array.from($('tabs').children).forEach(b => b.setAttribute('aria-pressed', String(b === button))); renderContent(); }); $('tabs').append(button); });
  $('coverage-summary').textContent = `${report.records.length} observations from ${collected} collected sources`;
  renderContent();
}
async function loadReport(id) {
  const selection = ++state.selection;
  const report = await api('/api/reports/' + id);
  if (selection !== state.selection) return;
  state.report = report; state.selected = id; state.tab = 'Overview'; $('filter').value = '';
  document.querySelectorAll('.history-item').forEach(b => b.classList.toggle('selected', b.dataset.report === id));
  renderReport();
}
function renderHistory() {
  const query = $('history-filter').value.trim().toLowerCase();
  const reports = savedReports.filter(report => (report.name + ' ' + report.imo).toLowerCase().includes(query));
  $('history').replaceChildren();
  for (const report of reports) {
    const button = node('button', '', 'history-item'); button.type = 'button'; button.dataset.report = report.id;
    button.classList.toggle('selected', state.selected === report.id);
    const sub = node('span', '', 'history-sub'); sub.append(node('span', 'IMO ' + report.imo), node('span', dateText(report.generated_at)));
    button.append(node('span', report.name, 'history-name'), sub); button.title = `${report.name} Â· ${dateText(report.generated_at)}`;
    button.addEventListener('click', async () => {
      try {
        message('');
        await loadReport(report.id);
        if (state.selected !== report.id) return;
        $('vessel-name').focus({preventScroll:true});
        $('results').scrollIntoView({block:'start'});
      } catch (error) { message(error.message); }
    });
    $('history').append(button);
  }
  if (!reports.length) $('history').append(node('p', query ? 'No saved reports match this search.' : 'Completed vessel lookups will appear here.', 'muted'));
}
async function refreshHistory() {
  const data = await api('/api/history');
  savedReports = data.reports;
  renderHistory();
  return data;
}
async function pollJob(id) {
  busy(true); let failures = 0;
  while (state.busy) {
    let job;
    try { job = await api('/api/jobs/' + id); failures = 0; }
    catch(error) {
      failures++;
      $('progress-subtitle').textContent = 'Reconnecting to the local serverâ€¦';
      if (failures >= 5) { busy(false); message('Connection lost. The lookup may still be running. Refresh this page after the server is available.'); return; }
      await new Promise(resolve => setTimeout(resolve, 2000)); continue;
    }
    $('progress-title').textContent = `Investigating IMO ${job.imo}`;
    $('progress-subtitle').textContent = 'Collecting public records. Some sources may take a minute.';
    $('progress-events').replaceChildren(...job.events.map(text => node('li', text)));
    if (job.state === 'complete') {
      busy(false);
      try { await loadReport(job.report_id); message(job.observations ? `Investigation complete. ${job.observations} source observations collected.` : 'The lookup finished without findings. Review Sources for access failures or missing records.'); }
      catch(error) {message(error.message);}
      return;
    }
    if (job.state === 'failed') { busy(false); formError(job.error || 'The lookup failed. Please try again.'); return; }
    await new Promise(resolve => setTimeout(resolve, 900));
  }
}
if ($('lookup-form')) {
  if (location.hash === '#saved') location.replace('/saved');
  window.addEventListener('hashchange', () => { if (location.hash === '#saved') location.replace('/saved'); });
  $('lookup-form').addEventListener('submit', async event => {
    event.preventDefault(); if (state.busy) return;
    const imo = $('imo-input').value.trim().replace(/^IMO\s*/i, '');
    if (!/^\d{7}$/.test(imo)) { formError('Enter a seven-digit IMO number.'); $('imo-input').focus(); return; }
    const checksum = [...imo.slice(0,6)].reduce((sum, number, index) => sum + Number(number) * (7-index), 0) % 10;
    if (checksum !== Number(imo[6])) { formError('That IMO has an invalid check digit. Please check the number.'); $('imo-input').focus(); return; }
    formError(''); message(''); busy(true); $('progress-events').replaceChildren();
    try { const job = await api('/api/scans', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({imo})}); await pollJob(job.job_id); }
    catch(error) {busy(false); formError(error.message);}
  });
  $('use-example').addEventListener('click', () => { $('imo-input').value = '9299898'; formError(''); $('imo-input').focus(); });
  $('imo-input').addEventListener('input', () => formError(''));
  api('/api/history').then(data => {
    if (data.active_job) return pollJob(data.active_job);
  }).catch(error => message(error.message));
} else {
  $('history-filter').addEventListener('input', renderHistory);
  $('refresh-history').addEventListener('click', () => refreshHistory().catch(error => message(error.message)));
  refreshHistory().catch(error => { $('history').textContent = 'Saved reports are unavailable.'; message(error.message); });
}
$('filter').addEventListener('input', renderContent);
