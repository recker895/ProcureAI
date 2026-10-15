// Component tests in an isolated DOM. Fixtures are never stored in the operational DB.
import test from 'node:test';import assert from 'node:assert/strict';import fs from 'node:fs';import path from 'node:path';import {fileURLToPath} from 'node:url';import {createRequire} from 'node:module';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const require=createRequire(process.env.PROCURE_UI_PACKAGE||import.meta.url);const {JSDOM}=require('jsdom');
const code=fs.readFileSync(path.join(root,'static/app.js'),'utf8')+'\n'+fs.readFileSync(path.join(root,'static/automation.js'),'utf8'),theme=fs.readFileSync(path.join(root,'static/theme.js'),'utf8');
const baseCase={id:'PR-TEST',title:'Isolated test fixture',material:'TEST',version:1,commercial_version:1,quantity:'300',unit:'EA',currency:'USD',deadline:'2027-01-15',min_quality:'80',creator:'U-BUYER',status:'QUOTATIONS',quotes:[],weights:{cost:40,quality:20,delivery:15,reliability:15,risk:10},recommendations:[],selected:null,supplier_approval:null,po:null,receipts:[],invoices:[],payments:[],explanations:[],documents:[],jobs:[],summary:{accepted:'0',invoiced:'0',balance:'0.00'},created:'2026-10-14T10:00:00Z',updated:'2026-10-14T10:00:00Z'};
async function until(fn){for(let i=0;i<200;i++){if(fn())return;await new Promise(r=>setTimeout(r,5))}throw Error('UI did not settle')}
async function env({role='buyer',cases=[],hash='#dashboard'}={}){
 const dom=new JSDOM('<div id="app"></div><div id="toast"></div><dialog id="modal"></dialog>',{url:'http://localhost/'+hash,runScripts:'outside-only',pretendToBeVisual:true});const w=dom.window;let calls=[],preferences='system',defaultMode='manual';
 w.matchMedia=()=>({matches:false,addEventListener(){}});w.HTMLElement.prototype.scrollIntoView=function(){};w.HTMLDialogElement.prototype.showModal=function(){this.open=true};w.HTMLDialogElement.prototype.close=function(){this.open=false};w.eval(theme);
 w.fetch=async(url,opt={})=>{calls.push({url,opt});let data;
  if(url==='/api/setup')data={required:false};else if(url==='/api/me')data={user:{id:'U-BUYER',name:'Test User',role,theme:preferences},csrf:'test-csrf'};
  else if(url==='/api/cases')data=cases;
  else if(url.startsWith('/api/dashboard'))data={currencies:['USD'],currency:'USD',scope:'Test fixture only',active:cases.length,pending:0,po_total:'0.00',verification:0,suppliers:[],trend:[]};
  else if(url==='/api/notifications')data=[];else if(url==='/api/integrations')data=Object.fromEntries(['sap','email','payment'].map(k=>[k,{status:'Not connected',available:false}]));
  else if(url==='/api/automation')data={default_mode:defaultMode,working:[],decisions:[],exceptions:[],roles:Object.fromEntries(Array.from({length:13},(_,i)=>[i+1,'Agent '+(i+1)])),metrics:{runs:0}};
  else if(url.startsWith('/api/audit'))data={integrity:true,events:[]};else if(url==='/api/preferences'){const p=JSON.parse(opt.body);if(p.theme)preferences=p.theme;if(p.default_mode)defaultMode=p.default_mode;data=p}
  else if(url.startsWith('/api/cases/'))data=cases.find(c=>url==='/api/cases/'+c.id)||baseCase;
  else if(url==='/api/users')data=[];else throw Error('Unexpected test request '+url);
  return {ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(data))};
 };w.eval(code);await until(()=>w.document.querySelector('.sidebar'));return {w,dom,calls,get preference(){return preferences}};
}
test('Dashboard renders all routes and truthful empty analytics',async()=>{
 const e=await env();try{assert.equal(e.w.document.querySelectorAll('.nav a').length,15);assert.equal(e.w.document.querySelectorAll('.metric').length,4);assert.match(e.w.document.body.textContent,/No approved supplier spend/);assert.match(e.w.document.body.textContent,/Ready for your first workflow/);assert.doesNotMatch(e.w.document.body.textContent,/\$3\.4M/)}finally{e.dom.window.close()}
});
test('Theme changes persist via CSRF-protected API without mutating business state',async()=>{
 const e=await env();try{const select=e.w.document.querySelector('#theme');select.value='dark';select.dispatchEvent(new e.w.Event('change'));await until(()=>e.preference==='dark');assert.equal(e.w.document.documentElement.dataset.theme,'dark');const request=e.calls.find(c=>c.url==='/api/preferences');assert.equal(request.opt.headers['X-CSRF-Token'],'test-csrf');assert.equal(e.calls.filter(c=>c.url.includes('/actions/')).length,0)}finally{e.dom.window.close()}
});
test('Case offers buyer quotation actions and evidence explanation panel',async()=>{
 const e=await env({cases:[structuredClone(baseCase)],hash:'#case/PR-TEST'});try{assert.equal(e.w.document.querySelectorAll('.stage').length,10);assert.ok(e.w.document.querySelector('[data-command="quote"]'));assert.match(e.w.document.body.textContent,/Agent explanations/);e.w.document.querySelector('[data-command="quote"]').click();assert.ok(e.w.document.querySelector('#f-unit_price'));assert.equal(e.w.document.querySelector('#modal').open,true);assert.ok(e.w.document.querySelector('#f-document_id'))}finally{e.dom.window.close()}
});
test('Viewer cannot see write controls',async()=>{
 const e=await env({role:'viewer',cases:[structuredClone(baseCase)],hash:'#case/PR-TEST'});try{assert.equal(e.w.document.querySelector('[data-command="quote"]'),null);assert.equal(e.w.document.querySelector('[data-command="upload"]'),null);assert.match(e.w.document.body.textContent,/no available action for your role/)}finally{e.dom.window.close()}
});
test('Stored untrusted text is escaped instead of executing markup',async()=>{
 const c=structuredClone(baseCase);c.title='<img src=x onerror="alert(1)">';const e=await env({cases:[c],hash:'#case/PR-TEST'});try{assert.equal(e.w.document.querySelector('img[src="x"]'),null);assert.ok(e.w.document.querySelector('h1').textContent.includes('<img'))}finally{e.dom.window.close()}
});
test('Settings clearly distinguish disconnected external services',async()=>{
 const e=await env({hash:'#settings'});try{assert.equal(e.w.document.querySelectorAll('.integration').length,8);assert.equal(e.w.document.querySelectorAll('.integration .badge').length,8);assert.match(e.w.document.body.textContent,/Current workflow reconciles payments executed externally/)}finally{e.dom.window.close()}
});
test('Global search navigates to the matching persisted workflow',async()=>{
 const e=await env({cases:[structuredClone(baseCase)]});try{const input=e.w.document.querySelector('#global-search');input.value='PR-TEST';input.dispatchEvent(new e.w.Event('input'));assert.equal(e.w.document.querySelector('.searchresults a').getAttribute('href'),'#case/PR-TEST')}finally{e.dom.window.close()}
});

test('Default AI switch opens three queues without modifying existing cases',async()=>{
 const e=await env({cases:[structuredClone(baseCase)]});try{e.w.document.querySelector('#mode-ai').click();await until(()=>e.w.document.querySelectorAll('.queue-card').length===3);assert.match(e.w.document.body.textContent,/human approvals required/);assert.equal(e.calls.filter(c=>c.url.includes('/actions/')).length,0)}finally{e.dom.window.close()}
});
test('Case AI switch proposes an audited transition with a required reason',async()=>{
 const e=await env({cases:[{...structuredClone(baseCase),mode:'manual'}],hash:'#case/PR-TEST'});try{e.w.document.querySelector('#mode-ai').click();assert.equal(e.w.document.querySelector('#f-mode').value,'ai');assert.equal(e.w.document.querySelector('#f-reason').required,true);assert.match(e.w.document.querySelector('#modal').textContent,/Processing external actions/)}finally{e.dom.window.close()}
});
test('Agent workspace displays thirteen on-demand roles and factual empty queues',async()=>{
 const e=await env({hash:'#agents'});try{assert.equal(e.w.document.querySelectorAll('.agent-role').length,13);assert.equal(e.w.document.querySelectorAll('.queue-card').length,3);assert.match(e.w.document.body.textContent,/No agents running/)}finally{e.dom.window.close()}
});

test('Uploaded document status appears before document flow and distinguishes stored from applied quotations',async()=>{
 const c=structuredClone(baseCase);c.mode='ai';c.record_kind='test';c.documents=[{id:'DOC-WAIT',name:'Brookfield.txt',size:100},{id:'DOC-DONE',name:'Aster.txt',size:100}];c.intake=[{document_id:'DOC-WAIT',status:'QUEUED'},{document_id:'DOC-DONE',status:'APPLIED'}];c.agent_tasks=[{agent:3,status:'RETRY',name:'Document intake',error:'Groq rate limit reached; waiting for provider capacity.'}];
 const e=await env({cases:[c],hash:'#case/PR-TEST'});try{const panel=e.w.document.querySelector('#upload-status');assert.ok(panel);assert.match(panel.textContent,/Uploaded · awaiting extraction/);assert.match(panel.textContent,/Added to workflow/);assert.match(panel.textContent,/Groq rate limit/);const flow=[...e.w.document.querySelectorAll('h2')].find(x=>x.textContent==='Procurement document flow');assert.ok(panel.compareDocumentPosition(flow)&e.w.Node.DOCUMENT_POSITION_FOLLOWING);}finally{e.dom.window.close()}
});

test('Guided AI review keeps evidence visible and never approves on open',async()=>{
 const c={...baseCase,mode:'ai',record_kind:'test',status:'SUPPLIER_REVIEW',explanations:[{agent:'Optimization',model:'test-model',recommendation:'Choose the eligible lowest cost allocation.',reasons:['Cost and delivery constraints passed.'],alternatives:['Other quote costs more.'],created:'2026-10-15T10:00:00Z'}],intake:[],agent_tasks:[]};
 const e=await env({cases:[c],hash:'#case/'+c.id});try{
 e.w.document.querySelector('[data-command="ai-journey"]').click();await until(()=>e.w.document.querySelector('.journey-dialog form'));
 const d=e.w.document.querySelector('#modal');assert.equal(d.open,true);assert.match(d.textContent,/Cost and delivery constraints passed/);assert.match(d.textContent,/Confirm decision & continue/);
 assert.equal(e.calls.filter(x=>x.url.includes('/actions/')).length,0);
 const reason=d.querySelector('textarea');reason.value='My review remains in the window';assert.equal(reason.value,'My review remains in the window');
 }finally{e.dom.window.close()}
});
test('Completed AI journey shows final state and labels past recommendations',async()=>{
 const c={...baseCase,mode:'ai',record_kind:'test',status:'COMPLETED',intake:[],agent_tasks:[]};const e=await env({cases:[c],hash:'#case/'+c.id});try{
 e.w.document.querySelector('[data-command="ai-journey"]').click();await until(()=>e.w.document.querySelector('.journey-dialog'));
 assert.match(e.w.document.querySelector('#modal').textContent,/No real payment was made/);assert.match(e.w.document.querySelector('#modal').textContent,/historical/);
 assert.equal(e.w.document.querySelectorAll('.journey-dialog form').length,0);
 }finally{e.dom.window.close()}
});

test('Guided human submission advances in the same open dialog',async()=>{
 const c={...baseCase,mode:'ai',record_kind:'test',status:'SUPPLIER_REVIEW',intake:[],agent_tasks:[]};const e=await env({cases:[c],hash:'#case/'+c.id});try{
 e.w.document.querySelector('[data-command="ai-journey"]').click();await until(()=>e.w.document.querySelector('.journey-dialog form'));
 const dialog=e.w.document.querySelector('#modal'),form=dialog.querySelector('form');form.querySelector('textarea').value='Reviewed the fixture evidence';
 c.status='COMPLETED';form.dispatchEvent(new e.w.Event('submit',{bubbles:true,cancelable:true}));
 await until(()=>dialog.textContent.includes('Workflow complete'));assert.equal(dialog.open,true);assert.equal(e.w.document.querySelector('#modal'),dialog);
 assert.equal(e.calls.filter(x=>x.url.includes('/actions/supplier_decision')).length,1);
 }finally{e.dom.window.close()}
});

test('Blocked extraction offers recovery instead of an endless processing screen',async()=>{
 const c={...baseCase,mode:'ai',record_kind:'test',intake:[{document_id:'D-test',status:'QUEUED',kind:'quotation'}],agent_tasks:[{name:'Document intake',status:'BLOCKED',error:'Source validation failed'}]};const e=await env({cases:[c],hash:'#case/'+c.id});try{
 e.w.document.querySelector('[data-command="ai-journey"]').click();await until(()=>e.w.document.querySelector('#journey-retry'));
 assert.match(e.w.document.querySelector('#modal').textContent,/Processing is paused/);assert.doesNotMatch(e.w.document.querySelector('#modal').textContent,/Processing your evidence/);
 assert.equal(e.calls.filter(x=>x.opt.method==='POST').length,0);
 }finally{e.dom.window.close()}
});

test('Guided version conflict reloads evidence without resubmitting approval',async()=>{
 const c={...baseCase,mode:'ai',record_kind:'test',status:'SUPPLIER_REVIEW',intake:[],agent_tasks:[]};const e=await env({cases:[c],hash:'#case/'+c.id});try{
 const fetch=e.w.fetch;let submissions=0;e.w.fetch=async(url,opt)=>{if(url.includes('/actions/')){submissions++;c.version=2;return {ok:false,status:409,json:async()=>({detail:'Record changed; refresh before continuing'})};}return fetch(url,opt)};
 e.w.document.querySelector('[data-command="ai-journey"]').click();await until(()=>e.w.document.querySelector('.journey-dialog form'));
 const old=e.w.document.querySelector('#modal form');old.querySelector('[name="reason"]').value='My evidence review';old.dispatchEvent(new e.w.Event('submit',{bubbles:true,cancelable:true}));
 await until(()=>e.w.document.querySelector('#modal').textContent.includes('latest evidence is now loaded'));
 assert.equal(submissions,1);assert.notEqual(e.w.document.querySelector('#modal form'),old);assert.equal(e.w.document.querySelector('#modal [name="reason"]').value,'My evidence review');assert.equal(e.w.document.querySelector('#modal').open,true);
 }finally{e.dom.window.close()}
});
