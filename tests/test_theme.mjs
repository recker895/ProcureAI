import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const script=fs.readFileSync(path.join(root,'static/theme.js'),'utf8');
function environment(initial='system',dark=false){
  let listener;const values=new Map([['procure-theme',initial]]);const document={documentElement:{dataset:{}}};
  const media={matches:dark,addEventListener:(_,fn)=>listener=fn};const context={window:{},document,matchMedia:()=>media,localStorage:{getItem:k=>values.get(k),setItem:(k,v)=>values.set(k,v)}};
  vm.runInNewContext(script,context);return {context,values,media,change:()=>listener()};
}
test('Light and Dark apply immediately and persist locally',()=>{
  const e=environment();e.context.window.setTheme('dark');assert.equal(e.context.document.documentElement.dataset.theme,'dark');assert.equal(e.values.get('procure-theme'),'dark');
  e.context.window.setTheme('light');assert.equal(e.context.document.documentElement.dataset.theme,'light');
});
test('System follows operating system changes',()=>{
  const e=environment('system',false);assert.equal(e.context.document.documentElement.dataset.theme,'light');e.media.matches=true;e.change();assert.equal(e.context.document.documentElement.dataset.theme,'dark');
});
test('Explicit preference is not overwritten by OS changes',()=>{
  const e=environment('light',true);e.change();assert.equal(e.context.document.documentElement.dataset.theme,'light');
});
test('Preloaded preference prevents wrong-theme initialization',()=>{
  const e=environment('dark',false);assert.equal(e.context.document.documentElement.dataset.theme,'dark');
});
test('Semantic tokens cover both themes',()=>{
  const css=fs.readFileSync(path.join(root,'static/tokens.css'),'utf8');const [light,dark]=css.split(':root[data-theme=dark]');
  const tokens=[...light.matchAll(/--([\w-]+):/g)].map(x=>x[1]);for(const token of tokens)assert.ok(dark.includes('--'+token+':'),token);
});
test('Theme still works when browser storage is unavailable',()=>{
  const document={documentElement:{dataset:{}}};const context={window:{},document,matchMedia:()=>({matches:true,addEventListener(){}}),localStorage:{getItem(){throw Error('Storage blocked')},setItem(){throw Error('Storage blocked')}}};
  vm.runInNewContext(script,context);assert.equal(document.documentElement.dataset.theme,'dark');context.window.setTheme('light');assert.equal(document.documentElement.dataset.theme,'light');
});
