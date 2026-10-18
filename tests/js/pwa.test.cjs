const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
function worker(network = async () => new Response('network')) {
  const handlers = {}, stores = new Map(), deleted = [];
  let writes = 0, waiting = 0;
  const caches = {
    async keys() { return ['unrelated-cache','peladeiros-gpcta-v6','peladeiros-gpcta-v7']; },
    async delete(key) { deleted.push(key); },
    async open() { return {
      async addAll(urls) { for (const url of urls) stores.set(url, new Response('shell')); },
      async match(req) { return stores.get(typeof req === 'string' ? req : new URL(req.url).pathname); },
      async put(req, res) { await new Promise(resolve => setTimeout(resolve,5)); stores.set(new URL(req.url).pathname,res); writes++; }
    }; }
  };
  const self = {location: {origin:'https://gpcta.test'}, clients:{claim:async()=>{}}, skipWaiting(){waiting++;},
    addEventListener(name, fn){handlers[name]=fn;} };
  vm.runInNewContext(fs.readFileSync('static/service-worker.js','utf8'),{self,caches,fetch:network,URL,Response});
  async function event(name, extra={}) {
    let response, lifetime;
    handlers[name]({...extra,waitUntil(p){lifetime=p;}, respondWith(p){response=p;}});
    const result = response && await response;
    if (lifetime) await lifetime;
    return result;
  }
  return {event,stores,deleted,get writes(){return writes;},get waiting(){return waiting;}};
}
test('install waits for shell; activation deletes only owned caches; activation requires message', async()=>{
  const sw=worker(); await sw.event('install'); assert.ok(sw.stores.has('/offline')); assert.equal(sw.waiting,0);
  await sw.event('activate'); assert.deepEqual(sw.deleted,['peladeiros-gpcta-v6']);
  await sw.event('message',{data:{type:'SKIP_WAITING'}}); assert.equal(sw.waiting,1);
});
test('offline navigation uses informative fallback without caching private HTML',async()=>{
  const sw=worker(async()=>{throw Error('offline');}); await sw.event('install');
  const response=await sw.event('fetch',{request:{url:'https://gpcta.test/stock',method:'GET',mode:'navigate'}});
  assert.equal(await response.text(),'shell'); assert.equal(sw.writes,0);
});
test('online navigation preserves server errors; APIs, POST and non-allowlisted assets bypass cache',async()=>{
  const sw=worker(async()=>new Response('unauthorized',{status:401}));
  const response=await sw.event('fetch',{request:{url:'https://gpcta.test/stock',method:'GET',mode:'navigate'}});
  assert.equal(response.status,401); assert.equal(sw.writes,0);
  for(const [path,method] of [['/api/private','GET'],['/sale','POST'],['/static/private.json','GET']]) {
    assert.equal(await sw.event('fetch',{request:{url:'https://gpcta.test'+path,method,mode:'cors'}}),undefined);
  }
});
test('cache refresh lifetime awaits the actual cache write',async()=>{
  const sw=worker(); await sw.event('fetch',{request:{url:'https://gpcta.test/static/style.css',method:'GET',mode:'cors'}});
  assert.equal(sw.writes,1);
});
function ui(controlled = true) {
  const documentEvents={}, swEvents={}, regEvents={}, installEvents={};
  const button={disabled:false, addEventListener(name,fn){this[name]=fn;}};
  const nodes={'pwa-update':{},'pwa-update-button':button,'pwa-update-message':{}};
  let reloads=0, messages=0, resolveFetch;
  const navigator={serviceWorker:{controller:controlled ? {} : null,addEventListener(n,fn){swEvents[n]=fn;}}};
  const document={getElementById(id){return nodes[id];},addEventListener(n,fn){documentEvents[n]=fn;}};
  class XMLHttpRequest {
    constructor(){this.listeners={};}
    addEventListener(name,fn){this.listeners[name]=fn;}
    removeEventListener(name){delete this.listeners[name];}
    send(){if(this.fail) throw Error('send failed');}
    finish(){if(this.listeners.loadend)this.listeners.loadend();}
  }
  const window={XMLHttpRequest,location:{reload(){reloads++;}},fetch:()=>new Promise(resolve=>{resolveFetch=resolve;})};
  vm.runInNewContext(fs.readFileSync('static/pwa-update.js','utf8'),{document,navigator,window,XMLHttpRequest,Promise,Array,Set,queueMicrotask});
  documentEvents.DOMContentLoaded();
  const registration={waiting:{postMessage(){messages++;}},installing:{addEventListener(n,fn){installEvents[n]=fn;}},addEventListener(n,fn){regEvents[n]=fn;}};
  window.gpctaPwaUpdates.watch(registration);
  return {documentEvents,swEvents,regEvents,installEvents,button,nodes,window,registration,
    resolveFetch(){resolveFetch('ok');},get reloads(){return reloads;},get messages(){return messages;}};
}
test('waiting version requires user confirmation; clean page reloads only once',()=>{
  const app=ui(); assert.equal(app.nodes['pwa-update'].hidden,false); assert.equal(app.reloads,0);
  app.button.click(); assert.equal(app.messages,1); app.swEvents.controllerchange(); app.swEvents.controllerchange(); assert.equal(app.reloads,1);
});
test('dirty form and pending operation prevent update and reload even during version change',async()=>{
  const app=ui(); const field={type:'text',value:'edited',defaultValue:'original'}; const form={elements:[field]};
  field.closest=()=>form; app.documentEvents.input({target:field});
  assert.equal(app.button.disabled,true); app.button.click(); assert.equal(app.messages,0);
  app.swEvents.controllerchange(); assert.equal(app.reloads,0);
  field.value='original'; app.documentEvents.input({target:field}); assert.equal(app.button.disabled,false);
  const request=app.window.fetch('/sale'); await Promise.resolve(); assert.equal(app.button.disabled,true);
  app.button.click(); assert.equal(app.messages,0); app.resolveFetch(); await request;
  assert.equal(app.button.disabled,false); app.button.click(); app.swEvents.controllerchange(); assert.equal(app.reloads,1);
});
test('new worker detected after initial registration exposes update without automatic reload',()=>{
  const app=ui(); app.nodes['pwa-update'].hidden=true; app.regEvents.updatefound(); app.installEvents.statechange();
  assert.equal(app.nodes['pwa-update'].hidden,false); assert.equal(app.reloads,0);
});
test('native form submission prevents an update reload',async()=>{
  const app=ui(); app.documentEvents.submit({defaultPrevented:false}); await Promise.resolve();
  app.button.click(); app.swEvents.controllerchange(); assert.equal(app.reloads,0);
});
test('a request started after confirmation still prevents controller reload',async()=>{
  const app=ui(); app.button.click(); assert.equal(app.messages,1);
  const request=app.window.fetch('/mutation'); await Promise.resolve();
  app.swEvents.controllerchange(); assert.equal(app.reloads,0);
  app.resolveFetch(); await request; assert.equal(app.reloads,0);
  app.registration.waiting=null; app.button.click(); assert.equal(app.reloads,1);
});
test('initial installation never reloads an uncontrolled page',()=>{
  const events={}, nodes={'pwa-update':{},'pwa-update-button':{addEventListener(){}},'pwa-update-message':{}};
  let reloads=0;
  const navigator={serviceWorker:{controller:null,addEventListener(n,fn){events[n]=fn;}}};
  const document={getElementById:id=>nodes[id],addEventListener(){}};
  vm.runInNewContext(fs.readFileSync('static/pwa-update.js','utf8'),{navigator,document,window:{location:{reload(){reloads++;}}},Set,queueMicrotask});
  events.controllerchange(); assert.equal(reloads,0);
});

test('a later update in the first installation session reloads after explicit confirmation',()=>{
  const app=ui(false); app.swEvents.controllerchange(); assert.equal(app.reloads,0);
  app.button.click(); app.swEvents.controllerchange(); assert.equal(app.reloads,1);
});

test('XHR operations block updates and release after success or synchronous failure',()=>{
  const app=ui(); const xhr=new app.window.XMLHttpRequest(); xhr.send();
  assert.equal(app.button.disabled,true); app.button.click(); assert.equal(app.messages,0);
  xhr.finish(); assert.equal(app.button.disabled,false);
  const failed=new app.window.XMLHttpRequest(); failed.fail=true;
  assert.throws(()=>failed.send()); assert.equal(app.button.disabled,false);
});
