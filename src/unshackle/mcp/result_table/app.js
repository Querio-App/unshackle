import { App, applyDocumentTheme, applyHostStyleVariables } from '@modelcontextprotocol/ext-apps';

const app = new App({name: 'unshackle query results', version: '1.0.0'});
const $ = (id) => document.getElementById(id);
const MAX_ROWS = 1000;
let columns = [], rows = [], visible = [], returned = 0, truncated = false;
let sortColumn = -1, ascending = true, kinds = [], downloading = false;
const collator = new Intl.Collator(undefined, {numeric: false, sensitivity: 'base'});
const numeric = /^[+-]?(?:(?:0|[1-9]\d*)(?:\.\d*)?|\.\d+)(?:e[+-]?\d{1,6})?$/i;
const iso = /^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?$/;
const display = (value) => value === null ? 'NULL' : typeof value === 'object' ? JSON.stringify(value) : String(value);
function columnKind(index) {
  const values = rows.map(row => row[index]).filter(value => value !== null);
  if (values.length && values.every(value => (typeof value === 'number' && Number.isFinite(value)) ||
    (typeof value === 'string' && numeric.test(value)))) return 'numeric';
  if (values.length && values.every(value => typeof value === 'string' && iso.test(value) && Number.isFinite(Date.parse(value)))) return 'date';
  return 'text';
}
// Compare decimal strings without converting warehouse DECIMALs / big integers to floats.
function decimalParts(value) {
  const text = String(value).toLowerCase();
  const [mantissa, exp = '0'] = text.replace(/^[+-]/, '').split('e');
  const [integer, fraction = ''] = mantissa.split('.');
  const digits = (integer + fraction).replace(/^0+/, '').replace(/0+$/, '') || '0';
  const leading = (integer + fraction).search(/[1-9]/);
  return {negative: text.startsWith('-') && digits !== '0', digits,
    magnitude: digits === '0' ? -Infinity : integer.length - leading + Number(exp)};
}
function compareNumeric(a, b) {
  const x = decimalParts(a), y = decimalParts(b);
  if (x.negative !== y.negative) return x.negative ? -1 : 1;
  const order = x.magnitude === y.magnitude
    ? x.digits.padEnd(Math.max(x.digits.length,y.digits.length),'0').localeCompare(y.digits.padEnd(Math.max(x.digits.length,y.digits.length),'0'))
    : x.magnitude < y.magnitude ? -1 : 1;
  return x.negative ? -order : order;
}
function compare(a, b) {
  const x = a[sortColumn], y = b[sortColumn];
  if (x === null || y === null) return x === y ? 0 : x === null ? 1 : -1;
  const order = kinds[sortColumn] === 'numeric' ? compareNumeric(x,y)
    : kinds[sortColumn] === 'date' ? Date.parse(x)-Date.parse(y)
    : collator.compare(display(x), display(y));
  return ascending ? order : -order;
}
function csvCell(value) {
  let text = value === null ? '' : display(value);
  // Quote alone doesn't prevent spreadsheet formulas. Include invisible/control prefixes.
  if (typeof value !== 'number' && /^[\s\p{Cc}\p{Cf}]*[=+\-@]/u.test(text)) text = "'" + text;
  return '"' + text.replaceAll('"', '""') + '"';
}
function csv() { return [columns, ...visible].map(row => row.map(csvCell).join(',')).join('\r\n') + '\r\n'; }
function showCsv() { $('csv-panel').hidden = false; $('csv').value = csv(); }
function render() {
  const term = $('search').value.toLocaleLowerCase();
  visible = rows.filter(row => row.some(value => display(value).toLocaleLowerCase().includes(term)));
  if (sortColumn >= 0) visible.sort(compare);
  $('count').textContent = `${visible.length} of ${returned} returned rows shown.` +
    (returned > MAX_ROWS ? ` Table and CSV limited to the first ${MAX_ROWS} returned rows.` : '') +
    (truncated ? ' Query truncated: more warehouse rows exist.' : '') + ' Search and export apply only to these returned rows.';
  const header = document.createElement('tr');
  columns.forEach((name,index) => {
    const th = document.createElement('th'); th.scope = 'col';
    th.setAttribute('aria-sort', index === sortColumn ? ascending ? 'ascending' : 'descending' : 'none');
    const button = document.createElement('button');
    button.textContent = name + (index === sortColumn ? ascending ? ' ↑' : ' ↓' : ' ↕');
    button.setAttribute('aria-label', `${name}, column ${index+1}, sort`);
    button.onclick = () => {
      ascending = sortColumn === index ? !ascending : true; sortColumn = index; render();
      $('head').querySelectorAll('button')[index].focus();
    };
    th.append(button); header.append(th);
  });
  $('head').replaceChildren(header);
  const fragment = document.createDocumentFragment();
  visible.forEach(row => {
    const tr = document.createElement('tr');
    row.forEach((value,index) => {
      const td = document.createElement('td'); td.textContent = display(value);
      if (value === null) td.classList.add('null');
      if (kinds[index] === 'numeric') td.classList.add('numeric');
      tr.append(td);
    }); fragment.append(tr);
  });
  $('body').replaceChildren(fragment);
  if (!$('csv-panel').hidden) $('csv').value = csv();
}
function receive(result) {
  let data = result.structuredContent;
  if (!data && !result.isError) {
    try { data = JSON.parse(result.content?.find(item => item.type === 'text')?.text); } catch {}
  }
  if (result.isError || !data || !Array.isArray(data.columns) || !data.columns.every(c => typeof c === 'string') ||
      !Array.isArray(data.rows) || !data.rows.every(row => Array.isArray(row) && row.length === data.columns.length)) {
    columns = []; rows = []; visible = []; $('head').replaceChildren(); $('body').replaceChildren();
    $('search').disabled = $('show-csv').disabled = $('download').disabled = true;
    $('csv-panel').hidden = true; $('csv').value = '';
    $('count').textContent = 'Rows are unavailable in this view. Large results may be stored as a file by the host. Try selecting fewer columns or a smaller SQL LIMIT; see the tool response in the conversation.'; return;
  }
  columns = data.columns; returned = data.rows.length; rows = data.rows.slice(0,MAX_ROWS);
  truncated = data.truncated === true; kinds = columns.map((_,index) => columnKind(index));
  sortColumn = -1; ascending = true; $('search').value = ''; $('status').textContent = '';
  $('search').disabled = $('show-csv').disabled = false; $('download').disabled = downloading;
  render();
}
function theme(context) {
  if (context?.theme) applyDocumentTheme(context.theme);
  if (context?.styles?.variables) applyHostStyleVariables(context.styles.variables);
}
$('search').addEventListener('input',render);
$('show-csv').onclick = () => { showCsv(); $('csv').focus(); $('csv').select(); };
$('download').onclick = async () => {
  if (downloading || !app.getHostCapabilities()?.downloadFile) return;
  downloading = true; $('download').disabled = true;
  try {
    const result = await app.downloadFile({contents:[{type:'resource',resource:{
      uri:'file:///query-results-filtered.csv',mimeType:'text/csv',text:csv()}}]}, {timeout:600000});
    $('status').textContent = result.isError ? 'Download did not finish. Copy the CSV below instead.' : 'Download started.';
    if (result.isError) showCsv();
  } catch {
    $('status').textContent = 'Download could not be confirmed. You can copy the CSV below.'; showCsv();
  } finally { downloading = false; $('download').disabled = false; }
};
// Handlers precede connect: hosts may deliver the first result during initialization.
app.ontoolresult = receive;
app.addEventListener('hostcontextchanged', theme);
app.connect().then(() => {
  theme(app.getHostContext()); $('download').hidden = !app.getHostCapabilities()?.downloadFile;
}).catch(() => { $('count').textContent = 'Connect this view through an MCP Apps host. The query JSON remains available in the conversation.'; });
