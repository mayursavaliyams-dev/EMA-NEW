// Browser test of every tracker filter. Needs the mock running (MOCK_TEST.bat or: python mock_market.py) and Node + Playwright.
// Run: node test_ui_filters.js   -> prints PASS/FAIL per check (an empty table, e.g. no deals today, is reported as FAIL "table empty").
// Exhaustive filter test: every table, every control, verified on the rows actually shown.
const { chromium } = require('playwright');
const R = []; const ok = (name, pass, info='') => { R.push([pass?'PASS':'FAIL', name, info]); };
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ viewport: { width: 1500, height: 1000 } });
  const p = await ctx.newPage();
  const errs = []; p.on('pageerror', e => { if(!/Internal S/.test(e.message)) errs.push(e.message); });
  await p.goto('http://localhost:8001/'); await p.click('button[data-tab=tracker]'); await p.waitForTimeout(9000);
  await p.evaluate(()=>{ localStorage.removeItem('trkTF'); });
  const bars = await p.$$eval('.tfbar', x=>x.map(e=>e.id.slice(4)));
  ok('filter bar on every table/list (12)', bars.length===12, bars.join(','));
  // sign of the row in the active "by" column, computed the same way a person reads it
  const visSigns = id => p.evaluate(id => {
    const el=document.getElementById(id), st=(TF[id]||{}), cfg=TF_CFG[id];
    if(cfg.items) return [...el.children].filter(c=>c.style.display!=='none'&&c.dataset.dir!==undefined).map(c=>c.dataset.dir===''?null:+c.dataset.dir);
    const dc=st.dc??cfg.dir;
    return [...el.tBodies[0].rows].filter(r=>r.style.display!=='none'&&r.cells.length>1).map(r=>tfCellSign(r.cells[dc]));
  }, id);
  const count = id => p.evaluate(id=>{ const t=document.getElementById('tfc-'+id).textContent.match(/(\d+) \/ (\d+)/); return t?[+t[1],+t[2]]:[0,0]; }, id);
  for (const id of bars) {
    const [, total] = await count(id);
    if (!total) { ok(`${id}: has rows to test`, false, 'table empty'); continue; }
    for (const side of ['UP','DOWN']) {
      await p.click(`#tfb-${id} button[data-ud="${side}"]`); await p.waitForTimeout(id==='trk-movers'||id==='trk-alerts'?2500:400);
      const s = await visSigns(id); const want = side==='UP'?1:-1;
      const [sh, tot] = await count(id);
      ok(`${id}: ${side} shows only ${side} rows`, s.every(x=>x===want), `${sh}/${tot} shown, wrong=${s.filter(x=>x!==want).length}`);
    }
    await p.click(`#tfb-${id} button[data-ud=""]`); await p.waitForTimeout(id==='trk-movers'||id==='trk-alerts'?2500:300);
    const [shA, totA] = await count(id);
    ok(`${id}: All shows every row`, shA===totA, `${shA}/${totA}`);
  }
  // search, several stocks
  const sym = await p.evaluate(()=>TRK_SUM.slice(0,2).map(r=>r.symbol));
  await p.fill('#tfb-trk-sum input[data-k=q]', sym.join(',')); await p.waitForTimeout(600);
  const shownSyms = await p.$$eval('#trk-sum tbody tr', rs=>rs.filter(r=>r.style.display!=='none').map(r=>r.cells[0].textContent.trim().split(' ')[0]));
  ok('search: 2 stocks with comma', shownSyms.length===2 && sym.every(s=>shownSyms.includes(s)), shownSyms.join(','));
  await p.fill('#tfb-trk-sum input[data-k=q]', ''); await p.waitForTimeout(500);
  // CE / PE
  for (const o of ['CE','PE']) {
    await p.click(`#tfb-trk-movers button[data-opt="${o}"]`); await p.waitForTimeout(400);
    const st = await p.$$eval('#trk-movers tbody tr', rs=>rs.filter(r=>r.style.display!=='none').map(r=>r.cells[1].textContent));
    ok(`movers: ${o} only`, st.length>0 && st.every(t=>t.includes(o)), `${st.length} rows`);
  }
  await p.click('#tfb-trk-movers button[data-opt=""]');
  // buildup
  for (const bu of ['LB','SB']) {
    await p.click(`#tfb-trk-movers button[data-bu="${bu}"]`); await p.waitForTimeout(400);
    const n = await p.$$eval('#trk-movers tbody tr', (rs,bu)=>rs.filter(r=>r.style.display!=='none').map(r=>!!r.querySelector('.bu-'+bu)), bu);
    ok(`movers: buildup ${bu}`, n.every(Boolean), `${n.length} rows`);
  }
  await p.click('#tfb-trk-movers button[data-bu=""]');
  // min/max on a column
  await p.selectOption('#tfb-trk-movers select[data-k=fc]', '9'); // IV
  let allIv = [];
  for (let t = 0; t < 10 && !allIv.length; t++) {   // IV is computed every 30 s: wait until the column has values
    allIv = (await p.$$eval('#trk-movers tbody tr', rs=>rs.map(r=>parseFloat(r.cells[9].textContent)))).filter(v=>!isNaN(v)).sort((a,b)=>a-b);
    if (!allIv.length) await p.waitForTimeout(3000);
  }
  const lo = allIv[Math.floor(allIv.length*0.25)], hi = allIv[Math.floor(allIv.length*0.75)];
  await p.fill('#tfb-trk-movers input[data-k=fmin]', String(lo)); await p.fill('#tfb-trk-movers input[data-k=fmax]', String(hi)); await p.waitForTimeout(600);
  const snap = await p.$$eval('#trk-movers tbody tr', rs=>rs.map(r=>[r.style.display!=='none', parseFloat(r.cells[9].textContent)]));
  const inR = v => !isNaN(v) && v>=lo && v<=hi;
  const vis = snap.filter(x=>x[0]), hid = snap.filter(x=>!x[0]);
  ok(`min/max: IV between ${lo} and ${hi}`, allIv.length>0 && vis.length>0 && vis.every(x=>inR(x[1])) && hid.every(x=>!inR(x[1])),
     `${vis.length} shown all inside, ${hid.length} hidden all outside`);
  await p.click('#tfb-trk-movers button[data-reset]'); await p.waitForTimeout(400);
  // sort by header
  await p.click('#trk-movers thead th:nth-child(10)'); await p.waitForTimeout(300);
  let col = await p.$$eval('#trk-movers tbody tr', rs=>rs.map(r=>parseFloat(r.cells[9].textContent)).filter(v=>!isNaN(v)));
  ok('sort: IV high→low', col.every((v,i)=>i===0||col[i-1]>=v), col.slice(0,3).join(','));
  await p.click('#trk-movers thead th:nth-child(10)'); await p.waitForTimeout(300);
  col = await p.$$eval('#trk-movers tbody tr', rs=>rs.map(r=>parseFloat(r.cells[9].textContent)).filter(v=>!isNaN(v)));
  ok('sort: IV low→high (2nd click)', col.every((v,i)=>i===0||col[i-1]<=v), col.slice(0,3).join(','));
  await p.waitForTimeout(6000);
  col = await p.$$eval('#trk-movers tbody tr', rs=>rs.map(r=>parseFloat(r.cells[9].textContent)).filter(v=>!isNaN(v)));
  ok('sort survives the 5-second refresh', col.every((v,i)=>i===0||col[i-1]<=v));
  // row limit
  await p.selectOption('#tfb-trk-sum select[data-k=lim]', '10'); await p.waitForTimeout(400);
  ok('row limit 10', (await count('trk-sum'))[0]===10);
  // columns hide
  await p.click('#tfb-trk-sum button[data-cols]'); await p.click('.tfcols input[data-i="1"]'); await p.click('.tfcols [data-close]');
  ok('hide a column', await p.$eval('#trk-sum thead th:nth-child(2)', h=>getComputedStyle(h).display)==='none');
  // ladder ATM window
  await p.selectOption('#tfb-trk-ladder select[data-k=atm]', '5'); await p.waitForTimeout(400);
  ok('ladder ATM ±5 = 11 strikes', (await count('trk-ladder'))[0]===11, String(await count('trk-ladder')));
  // alerts type filter
  const kinds = await p.$$eval('#tfb-trk-alerts select[data-k=kind] option', o=>o.map(x=>x.value).filter(Boolean));
  if (kinds.length) { await p.selectOption('#tfb-trk-alerts select[data-k=kind]', kinds[0]); await p.waitForTimeout(400);
    const ks = await p.$$eval('#trk-alerts > div', (d)=>d.filter(x=>x.style.display!=='none').map(x=>x.dataset.kind));
    ok(`alerts: type "${kinds[0]}" only`, ks.length>0 && ks.every(k=>k===kinds[0]), `${ks.length}`); }
  // persistence after a page reload
  await p.click('#tfb-trk-sum button[data-ud="DOWN"]'); await p.waitForTimeout(300);
  await p.reload(); await p.click('button[data-tab=tracker]'); await p.waitForTimeout(9000);
  const s2 = await visSigns('trk-sum');
  ok('filters remembered after page reload (DOWN + 10 rows + hidden col)', s2.length===10 && s2.every(x=>x===-1)
     && await p.$eval('#trk-sum thead th:nth-child(2)', h=>getComputedStyle(h).display)==='none', `${s2.length}`);
  // global UP/DOWN (top row)
  await p.evaluate(()=>{ localStorage.removeItem('trkTF'); for(const k in TF) delete TF[k]; });
  await p.click('#trk-ud button[data-v="DOWN"]'); await p.waitForTimeout(9000);
  for (const id of ['trk-movers','trk-sum','trk-trends','trk-hist']) {
    const s = await visSigns(id);
    ok(`global ▼DOWN applies to ${id}`, s.length>0 && s.every(x=>x===-1), `${s.length} rows, wrong=${s.filter(x=>x!==-1).length}`);
  }
  await p.click('#trk-ud button[data-v=""]');
  ok('no JavaScript errors', errs.length===0, errs.join(' | ').slice(0,200));
  const f = R.filter(r=>r[0]==='FAIL').length;
  R.forEach(r=>console.log(r.join(' | ')));
  console.log(`\n${R.length-f} PASS, ${f} FAIL`);
  await b.close();
})();
