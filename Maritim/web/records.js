'use strict';

const $ = id => document.getElementById(id);
const pageSize = 50;
let records = [];
let filtered = [];
let page = 0;

function renderRows() {
  const rows = document.createDocumentFragment();
  const start = page * pageSize;
  for (const {kind, imo, source, ...fields} of filtered.slice(start, start + pageSize)) {
    const row = document.createElement('tr');
    row.insertCell().textContent = kind;
    row.insertCell().textContent = imo;
    row.insertCell().textContent = Object.entries(fields).map(([k, v]) => k + ' · ' + v).join(' / ');
    const link = document.createElement('a');
    link.href = source;
    link.textContent = link.hostname || source;
    link.rel = 'noopener noreferrer';
    link.target = '_blank';
    row.insertCell().append(link);
    rows.append(row);
  }
  $('rows').replaceChildren(rows);
  $('page').textContent = filtered.length ? (start + 1) + '–' + Math.min(start + pageSize, filtered.length) + ' of ' + filtered.length : 'No matching records';
  $('prev').disabled = page === 0;
  $('next').disabled = start + pageSize >= filtered.length;
}

function filterRows() {
  const query = $('record-search').value.trim().toLowerCase();
  filtered = query ? records.filter(row => Object.values(row).some(value => value.toLowerCase().includes(query))) : records;
  page = 0;
  renderRows();
}

$('prev').addEventListener('click', () => { page--; renderRows(); });
$('next').addEventListener('click', () => { page++; renderRows(); });
$('record-search').addEventListener('input', filterRows);
$('records-file').addEventListener('change', () => {
  $('record-list').hidden = true;
  $('status').textContent = '';
});
$('import-form').addEventListener('submit', async event => {
  event.preventDefault();
  const file = $('records-file').files[0];
  if (!file) return;
  $('record-list').hidden = true;
  if (file.size > 1024 * 1024) {
    $('status').textContent = 'Choose a CSV file of up to 1 MB (1,048,576 bytes).';
    return;
  }
  $('status').textContent = 'Reading ' + file.name + '…';
  $('import-button').disabled = $('records-file').disabled = true;
  try {
    const response = await fetch('/api/records', {method: 'POST', headers: {'Content-Type': 'text/csv'}, body: file});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'The CSV could not be read.');
    records = data.records;
    $('record-search').value = '';
    filterRows();
    $('record-list').hidden = false;
    $('status').textContent = records.length + (records.length === 1 ? ' record' : ' records') + ' from ' + file.name;
  } catch (error) {
    $('status').textContent = error.message;
  } finally {
    $('import-button').disabled = $('records-file').disabled = false;
  }
});
