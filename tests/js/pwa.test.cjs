const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
function worker(network = async () => new Response('network')) {
  const handlers = {}, stores = new Map(), deleted = [];
  let writes = 0, waiting = 0;
  const caches = {
    async keys() { return ['unrelated-cache','peladeiros-gpcta-v6','peladeiros-gpcta-v8']; },
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

// Minimal transactional IndexedDB fake: tests execute the real worker helpers.
function fakeIndexedDB() {
  const seen=new Map();
  const database={close(){}, createObjectStore(){}, transaction(){
    const tx={objectStore(){return {
      add(value){ const request={}; queueMicrotask(()=>{
        if(seen.has(value.id)) { request.error={name:'ConstraintError'}; request.onerror({preventDefault(){},stopPropagation(){}}); }
        else seen.set(value.id,value);
        if(tx.oncomplete)tx.oncomplete();
      }); return request; },
      delete(id){seen.delete(id);queueMicrotask(()=>tx.oncomplete && tx.oncomplete());}
    };}};return tx;
  }};
  return {open(){const request={result:database};queueMicrotask(()=>request.onsuccess());return request;}};
}
test('persistent intelligent push identity claims once and can retry failed presentation',async()=>{
  const context={self:{addEventListener(){}},indexedDB:fakeIndexedDB()};
  vm.createContext(context);vm.runInContext(fs.readFileSync('static/service-worker.js','utf8'),context);
  assert.equal(await context.claimPurchaseAlert('123'),true);
  assert.equal(await context.claimPurchaseAlert('123'),false);
  await context.forgetPurchaseAlert('123');
  assert.equal(await context.claimPurchaseAlert('123'),true);
});

test('manager inbox counter uses its own endpoint and accessible label',async()=>{
  const calls=[];
  const bell={classList:{toggle(){}},setAttribute(name,value){this[name]=value;}};
  const badge={hidden:true,textContent:''};
  const menuBadge={hidden:true,textContent:''};
  const nodes={'#notification-bell':bell,'#notification-bell-count':badge,'#manager-message-menu-count':menuBadge};
  const documentEvents={};
  const context={
    document:{body:{dataset:{pushRole:'manager'}},hidden:false,
      querySelector:selector=>nodes[selector] || null,
      addEventListener(name,handler){documentEvents[name]=handler;}},
    window:{},navigator:{setAppBadge:async()=>{}},
    fetch:async url=>{calls.push(url);return {ok:true,json:async()=>({count:3})};},
    console,
  };
  const source=fs.readFileSync('static/pwa.js','utf8');
  const prefix=source.slice(0,source.indexOf('  const updatePendingDelivery'))+'})();';
  vm.runInNewContext(prefix,context);
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(calls,['/mensagens/nao-lidas']);
  assert.equal(bell['aria-label'],'Mensagens: 3 não lido(s)');
  assert.equal(badge.textContent,'3');
  assert.equal(menuBadge.textContent,'3');
  assert.equal(typeof documentEvents.visibilitychange,'function');
});
function pushUI(role, permission='granted') {
  const calls=[],documentEvents={};
  let unsubscribed=0,subscribed=0;
  const makeSubscription=()=>({endpoint:'https://fcm.googleapis.com/test',toJSON:()=>({endpoint:'https://fcm.googleapis.com/test',keys:{p256dh:'public',auth:'key'}}),unsubscribe:async()=>{unsubscribed++;}});
  const subscription=makeSubscription();
  const context={registrationPromise:Promise.resolve({pushManager:{getSubscription:async()=>subscription,subscribe:async()=>{subscribed++;return makeSubscription();}}}),
    document:{body:{dataset:{pushRole:role}},addEventListener(name,fn){documentEvents[name]=fn;},querySelector:()=>({content:'csrf-test'}),getElementById:()=>null},
    window:{PushManager:{}},Notification:{permission,requestPermission:async()=>'granted'},
    fetch:async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>({publicKey:'AA',forceRenew:role==='manager'})};},
    atob:s=>Buffer.from(s,'base64').toString('binary'),Uint8Array,console};
  context.window.Notification=context.Notification;
  const source=fs.readFileSync('static/pwa.js','utf8');
  vm.runInNewContext(source.slice(source.indexOf('  const publicKeyBytes'),source.indexOf('  const onboarding')),context);
  return {context,calls,documentEvents,get unsubscribed(){return unsubscribed;},get subscribed(){return subscribed;}};
}
test('manager permission alone never silently grants consent; explicit enrollment includes CSRF',async()=>{
  const app=pushUI('manager');await new Promise(resolve=>setImmediate(resolve));assert.equal(app.calls.length,0);
  await app.context.window.enablePushNotifications();
  const request=app.calls.find(call=>call.url.endsWith('/subscribe'));
  assert.equal(request.url,'/notifications/manager-push/subscribe');
  assert.equal(request.options.headers['X-CSRFToken'],'csrf-test');
  assert.equal(JSON.parse(request.options.body).consent,true);
  assert.equal(app.unsubscribed,1);
  assert.equal(app.subscribed,1);
});
test('client subscription renewal stays on legacy endpoints',async()=>{
  const app=pushUI('client');await new Promise(resolve=>setImmediate(resolve));
  assert.ok(app.calls.some(call=>call.url==='/notifications/push/subscribe'));
  assert.ok(app.calls.every(call=>!call.url.includes('manager-push')));
});
test('denied browser permission does not revoke manager association on page load',async()=>{
  const app=pushUI('manager','denied');await new Promise(resolve=>setImmediate(resolve));
  assert.equal(app.calls.length,0);
});

test('logout cancels a manager consent flow already awaiting preparation',async()=>{
  const app=pushUI('manager');let release;
  app.context.fetch=()=>new Promise(resolve=>{release=resolve;});
  const flow=app.context.window.enablePushNotifications();
  await new Promise(resolve=>setImmediate(resolve));
  app.documentEvents.submit({target:{getAttribute:()=>'/logout'}});
  release({ok:true,json:async()=>({publicKey:'AA'})});
  await assert.rejects(flow,/logout/);
});

function managerControls({active=false, permission='default', statusFailure=false, subscribeFailure=false}={}) {
  const calls=[];
  const element=extra=>Object.assign({hidden:false,disabled:false,checked:false,textContent:'',listeners:{},
    addEventListener(name,fn){this.listeners[name]=fn;}},extra);
  const nodes={
    'manager-push-status':element(),
    'manager-enable-push':element({disabled:true}),
    'manager-disable-push':element({hidden:true}),
    'manager-push-consent':element({disabled:true}),
    'manager-push-consent-group':element(),
  };
  let localSubscription=active ? {endpoint:'https://fcm.googleapis.com/active',toJSON(){return {endpoint:this.endpoint,keys:{p256dh:'public',auth:'key'}};},unsubscribe:async()=>{localSubscription=null;}} : null;
  const registration={pushManager:{
    getSubscription:async()=>localSubscription,
    subscribe:async()=>{localSubscription={endpoint:'https://fcm.googleapis.com/new',toJSON(){return {endpoint:this.endpoint,keys:{p256dh:'public',auth:'key'}};},unsubscribe:async()=>{localSubscription=null;}};return localSubscription;}
  }};
  const context={registrationPromise:Promise.resolve(registration),
    document:{body:{dataset:{pushRole:'manager'}},addEventListener(){},querySelector:()=>({content:'csrf-test'}),getElementById:id=>nodes[id] || null},
    window:{PushManager:{}},Notification:{permission,requestPermission:async()=>permission === 'denied' ? 'denied' : 'granted'},
    fetch:async(url,options)=>{
      calls.push({url,options});
      if (url.endsWith('/status')) {
        if (statusFailure) throw new TypeError('network');
        return {ok:true,redirected:false,json:async()=>({active})};
      }
      if (url.endsWith('/prepare')) return {ok:true,redirected:false,json:async()=>({publicKey:'AA',forceRenew:false})};
      if (url.endsWith('/subscribe') && subscribeFailure) throw new TypeError('network');
      return {ok:true,redirected:false,json:async()=>({ok:true})};
    },
    atob:s=>Buffer.from(s,'base64').toString('binary'),Uint8Array,console};
  context.window.Notification=context.Notification;
  const source=fs.readFileSync('static/pwa.js','utf8');
  vm.runInNewContext(source.slice(source.indexOf('  const publicKeyBytes'),source.indexOf('  const onboarding')),context);
  return {context,calls,nodes,settle:()=>new Promise(resolve=>setImmediate(resolve))};
}

test('manager consent checkbox gates activation and server success changes the UI',async()=>{
  const app=managerControls();await app.settle();
  const consent=app.nodes['manager-push-consent'];const enable=app.nodes['manager-enable-push'];
  assert.equal(consent.checked,false);assert.equal(consent.disabled,false);assert.equal(enable.disabled,true);
  consent.checked=true;consent.listeners.change();assert.equal(enable.disabled,false);
  await enable.listeners.click();
  assert.ok(app.calls.some(call=>call.url.endsWith('/prepare')));
  assert.ok(app.calls.some(call=>call.url.endsWith('/subscribe')));
  assert.equal(app.nodes['manager-push-status'].textContent,'Notificações ativas neste dispositivo');
  assert.equal(enable.hidden,true);assert.equal(app.nodes['manager-disable-push'].hidden,false);
});

test('an already authorized manager device is recognized without prepare or renewal',async()=>{
  const app=managerControls({active:true,permission:'granted'});await app.settle();
  assert.deepEqual(app.calls.map(call=>call.url),['/notifications/manager-push/status']);
  assert.equal(app.nodes['manager-enable-push'].hidden,true);
  assert.equal(app.nodes['manager-disable-push'].hidden,false);
  assert.equal(app.nodes['manager-push-status'].textContent,'Notificações ativas neste dispositivo');
});

test('denied permission and status network failure never revoke or enroll automatically',async()=>{
  const denied=managerControls({permission:'denied'});await denied.settle();
  assert.equal(denied.calls.length,0);
  assert.match(denied.nodes['manager-push-status'].textContent,/bloqueadas/);
  const failed=managerControls({active:true,statusFailure:true,permission:'granted'});await failed.settle();
  assert.deepEqual(failed.calls.map(call=>call.url),['/notifications/manager-push/status']);
  assert.match(failed.nodes['manager-push-status'].textContent,/verificar/);
});

test('activation network failure does not show success or silently disable consent',async()=>{
  const app=managerControls({subscribeFailure:true});await app.settle();
  const consent=app.nodes['manager-push-consent'];const enable=app.nodes['manager-enable-push'];
  consent.checked=true;consent.listeners.change();await enable.listeners.click();
  assert.equal(app.nodes['manager-push-status'].textContent,'Não foi possível concluir a operação. Verifique a conexão e tente novamente.');
  assert.equal(consent.checked,true);assert.equal(enable.hidden,false);assert.equal(enable.disabled,false);
});
