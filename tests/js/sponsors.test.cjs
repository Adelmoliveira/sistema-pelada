const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function panel(response = {ok:true, redirected:false, json:async()=>({ok:true})}) {
  const handlers = {}, saveHandlers = {}, calls = [];
  let reloads = 0;
  const list = {dataset:{revision:'4', url:'/admin/sponsors/order'}, children:[],
    addEventListener(name, fn) { handlers[name] = fn; },
    insertBefore(item, before) {
      this.children.splice(this.children.indexOf(item), 1);
      const at = before ? this.children.indexOf(before) : this.children.length;
      this.children.splice(at, 0, item);
    }};
  for (const id of [10, 11]) {
    const item = {dataset:{id:String(id)}, offsetHeight:10,
      closest() { return this; }, getBoundingClientRect() { return {top:0}; }};
    Object.defineProperty(item, 'previousElementSibling', {get:()=>list.children[list.children.indexOf(item)-1]});
    Object.defineProperty(item, 'nextElementSibling', {get:()=>list.children[list.children.indexOf(item)+1]});
    Object.defineProperty(item, 'nextSibling', {get:()=>item.nextElementSibling});
    list.children.push(item);
  }
  const status = {textContent:''}, save = {disabled:false, addEventListener(name, fn) { saveHandlers[name] = fn; }};
  const document = {getElementById(id) { return {'sponsor-order':list, 'sponsor-save-order':save, 'sponsor-order-status':status}[id]; },
    querySelector() { return {content:'csrf-test'}; }};
  vm.runInNewContext(fs.readFileSync('static/sponsors.js','utf8'), {document,
    window:{location:{reload(){reloads++;}}},
    fetch:async(...args)=>{calls.push(args); return response;},
    setInterval(){throw Error('polling forbidden');}});
  return {handlers, saveHandlers, list, status, save, calls, get reloads(){return reloads;}};
}

test('keyboard buttons reorder without dragging and preserve focus', () => {
  const p = panel(), item = p.list.children[1];
  const button = {dataset:{move:'up'}, closest(){return item;}, focus(){this.focused=true;}};
  p.handlers.click({target:{closest(){return button;}}});
  assert.deepEqual(p.list.children.map(x=>x.dataset.id), ['11','10']);
  assert.equal(button.focused, true);
});

test('drag and drop changes the common sponsor order', () => {
  const p = panel(), [first, second] = p.list.children;
  let transfer;
  p.handlers.dragstart({target:first,dataTransfer:{setData(type,value){transfer=[type,value];}}});
  p.handlers.drop({target:second,clientY:9,preventDefault(){}});
  assert.deepEqual(transfer,['text/plain','10']);
  assert.deepEqual(p.list.children.map(x=>x.dataset.id),['11','10']);
});

test('save sends current order and revision with CSRF and reloads after confirmation', async () => {
  const p = panel(); await p.saveHandlers.click();
  assert.equal(p.calls.length,1);
  const [url, options] = p.calls[0];
  assert.equal(url,'/admin/sponsors/order');
  assert.equal(options.headers['X-CSRFToken'],'csrf-test');
  assert.equal(options.credentials,'same-origin');
  assert.deepEqual(JSON.parse(options.body),{revision:4,ids:[10,11]});
  assert.equal(p.reloads,1);
});

test('conflict or expired session preserves page and displays error', async () => {
  for (const response of [{ok:false,status:409},{ok:true,redirected:true}]) {
    const p = panel(response); await p.saveHandlers.click();
    assert.equal(p.reloads,0);
    assert.equal(p.save.disabled,false);
    assert.match(p.status.textContent,/Recarregue/);
  }
});
