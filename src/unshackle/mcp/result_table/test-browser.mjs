// Actual bundled SDK + CSP in an iframe, driven by a minimal JSON-RPC host.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { chromium } from 'playwright';
const html = await readFile(new URL('result-table.html', import.meta.url), 'utf8');
const browser = await chromium.launch({headless:true, ...(process.env.CHROMIUM_PATH ? {executablePath:process.env.CHROMIUM_PATH} : {})});

async function mount(result, capability = false, download = 'ok') {
  const page = await browser.newPage();
  page.setDefaultTimeout(10000);
  const errors = [], requests = [];
  page.on('pageerror', error => errors.push(error.message));
  if (process.env.DEBUG_UI) page.on('console', message => console.log(message.type(), message.text()));
  page.on('request', request => requests.push(request.url()));
  await page.setContent('<iframe title="Query results" style="width:900px;height:650px"></iframe>');
  await page.evaluate(({html,result,capability,download}) => {
    window.downloads = []; window.messages = [];
    const frame = document.querySelector('iframe');
    const send = message => frame.contentWindow.postMessage({jsonrpc:'2.0',...message}, '*');
    window.sendResult = params => send({method:'ui/notifications/tool-result',params});
    window.sendTheme = params => send({method:'ui/notifications/host-context-changed',params});
    window.addEventListener('message', event => {
      if (event.source !== frame.contentWindow) return;
      const message = event.data; window.messages.push(message.method);
      if (message.method === 'ui/initialize') send({id:message.id,result:{
        protocolVersion:message.params.protocolVersion,hostInfo:{name:'test host',version:'1'},
        hostCapabilities:capability ? {downloadFile:{}} : {},hostContext:{theme:'light',displayMode:'inline'}}});
      if (message.method === 'ui/notifications/initialized') window.sendResult(result);
      if (message.method === 'ui/download-file') {
        window.downloads.push(message.params);
        if (download === 'reject') send({id:message.id,error:{code:-32603,message:'Example failure'}});
        else if (download !== 'pending') send({id:message.id,result:{isError:download === 'cancel'}});
      }
    }); frame.srcdoc = html;
  },{html,result,capability,download});
  const frame = page.frameLocator('iframe');
  await frame.locator('#count').filter({hasText:/returned rows shown|Rows are unavailable/}).waitFor();
  return {page,frame,errors,requests};
}
const result = (columns, rows, truncated = false) => ({content:[],structuredContent:{columns,rows,row_count:rows.length,truncated}});
const column = async (frame, index) => frame.locator(`#body tr td:nth-child(${index})`).allTextContents();
test('safe positional rendering, exact numeric/date sorting, keyboard, filter, host theme', async () => {
  const hostile = '<img src=x onerror="window.pwned=1">';
  const {page,frame,errors,requests} = await mount(result([hostile,'same','same','date','id'],[
    [hostile,'9007199254740993','2','2025-01-01T01:00:00+02:00','002'],
    ['normal','9007199254740992','10','2025-01-01T00:00:00Z','010'],
    ['NULL',null,'-1.0000000000000000001',null,'001'],
    ['x','1.234567890123456789','-1.0000000000000000002','2024-12-31T23:30:00Z','100']
  ],true));
  assert.equal(await frame.locator('img').count(),0);
  assert.equal(await frame.locator('#head th').count(),5);
  assert.equal(await frame.locator('#body tr').count(),4);
  await frame.locator('#head button').nth(1).focus();
  await page.keyboard.press('Enter');
  assert.deepEqual(await column(frame,2),['1.234567890123456789','9007199254740992','9007199254740993','NULL']);
  await page.keyboard.press('Space');
  assert.deepEqual(await column(frame,2),['9007199254740993','9007199254740992','1.234567890123456789','NULL']);
  await frame.locator('#head button').nth(2).click();
  assert.deepEqual(await column(frame,3),['-1.0000000000000000002','-1.0000000000000000001','2','10']);
  await frame.locator('#head button').nth(3).click();
  assert.deepEqual(await column(frame,4),['2025-01-01T01:00:00+02:00','2024-12-31T23:30:00Z','2025-01-01T00:00:00Z','NULL']);
  assert.equal(await frame.locator('#body tr td:nth-child(5).numeric').count(),0);
  await frame.locator('#search').fill('normal');
  assert.equal(await frame.locator('#body tr').count(),1);
  assert.match(await frame.locator('#count').textContent(),/1 of 4 returned rows shown.*truncated/);
  await page.evaluate(() => window.sendTheme({theme:'dark',styles:{variables:{'--color-text-primary':'rgb(240, 240, 240)'}}}));
  await frame.locator('html[data-theme="dark"]').waitFor();
  assert.equal(await frame.locator('body').evaluate(el => getComputedStyle(el).color),'rgb(240, 240, 240)');
  await frame.locator('#show-csv').click();
  assert.match(await frame.locator('#csv').inputValue(),/9007199254740992/);
  assert.equal(await frame.locator('#download').isVisible(),false);
  assert.deepEqual(errors,[]); assert.deepEqual(requests,[]);
  await page.screenshot({path:'/tmp/unshackle-193-result-table-preview.png'});
  await page.close();
});
test('CSV scope, quoting, NULL, formula/control prefixes, numeric negatives and exact decimals',async()=>{
  const values = ['=SUM(A1)',' \t\r=1','\u0001+2','\u200b@x','-2','+2','a,"b"\nc',null,-42,'001','9007199254740993','1.234567890123456789'];
  const {page,frame} = await mount(result(['=header','value'],values.map(v=>['keep',v]).concat([['omit','secret']])));
  await frame.locator('#search').fill('keep'); await frame.locator('#show-csv').click();
  const csv = await frame.locator('#csv').inputValue();
  assert.ok(csv.startsWith('"\'=header","value"\n'));
  for (const value of values.slice(0,6)) assert.ok(csv.includes('"\''+value.replaceAll('\r','\n').replaceAll('"','""')+'"'));
  assert.ok(csv.includes('"a,""b""\nc"'));
  assert.ok(csv.includes('"keep",""')); assert.ok(csv.includes('"keep","-42"'));
  assert.ok(csv.includes('"001"')); assert.ok(csv.includes('"9007199254740993"')); assert.ok(csv.includes('"1.234567890123456789"'));
  assert.ok(!csv.includes('secret')); await page.close();
});
for (const outcome of ['ok','cancel','reject','pending']) test(`download capability: ${outcome}`,async()=>{
  const {page,frame,errors} = await mount(result(['n'],[[1],[2]]),true,outcome);
  await frame.locator('#search').fill('2'); await frame.locator('#download').click();
  await page.waitForFunction(()=>window.downloads.length===1);
  const downloads = await page.evaluate(()=>window.downloads);
  assert.deepEqual(downloads[0].contents,[{type:'resource',resource:{uri:'file:///query-results-filtered.csv',mimeType:'text/csv',text:'"n"\r\n"2"\r\n'}}]);
  if(outcome==='pending') assert.equal(await frame.locator('#download').isDisabled(),true);
  else {
    await frame.locator('#status').filter({hasText:outcome==='ok' ? 'Download started' : outcome==='cancel' ? 'did not finish' : 'could not be confirmed'}).waitFor();
    assert.equal(await frame.locator('#csv-panel').isVisible(),outcome!=='ok');
  }
  assert.deepEqual(errors,[]); await page.close();
});
test('UI cap, empty results, text fallback, malformed/file pointer clears stale data',async()=>{
  const {page,frame} = await mount(result(['n'],Array.from({length:1001},(_,i)=>[i]),false));
  assert.equal(await frame.locator('#body tr').count(),1000);
  assert.match(await frame.locator('#count').textContent(),/first 1000/);
  await page.evaluate(()=>window.sendResult({content:[{type:'text',text:JSON.stringify({columns:['empty'],rows:[],row_count:0,truncated:false})}]}));
  await frame.locator('#count').filter({hasText:'0 of 0'}).waitFor();
  await page.evaluate(()=>window.sendResult({content:[{type:'resource_link',uri:'file:///result.json',name:'offloaded'}]}));
  await frame.locator('#count').filter({hasText:'smaller SQL LIMIT'}).waitFor();
  assert.equal(await frame.locator('#show-csv').isDisabled(),true);
  assert.equal(await frame.locator('#body tr').count(),0); await page.close();
});
test.after(async()=>{await browser.close();});
