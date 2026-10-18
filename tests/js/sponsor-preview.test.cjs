const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function previewPanel({confirm = true, transport} = {}) {
  function control(extra={}) { return {listeners:{}, checked:false, disabled:false,
    addEventListener(name,fn){this.listeners[name]=fn;}, ...extra}; }
  const file=control({files:[]}), remove=control(), confirmation=control();
  const image={hidden:false,getAttribute(){return '/admin/sponsors/3/logo';}};
  const button=control({focus(){this.focused=true;}}), status={textContent:''};
  const fields={'[name="logo"]':file,'[name="remove_background"]':remove,'[name="confirm_background"]':confirmation,
    '[data-logo-preview]':image,'[data-preview-logo]':button,'[data-preview-status]':status};
  const form=control({dataset:{previewUrl:'/admin/sponsors/preview'},querySelector(selector){return fields[selector];}});
  const calls=[], revoked=[], created=[];
  const response={ok:true,redirected:false,headers:{get(){return 'image/webp';}},blob:async()=>({preview:true})};
  vm.runInNewContext(fs.readFileSync('static/sponsor-preview.js','utf8'),{
    document:{querySelectorAll(){return [form];},querySelector(){return {content:'csrf-test'};}},
    window:{confirm(){return confirm;}},
    URL:{createObjectURL(blob){created.push(blob);return 'blob:preview-'+created.length;},revokeObjectURL(url){revoked.push(url);}},
    FormData:class {constructor(){this.values={};} set(key,value){this.values[key]=value;}},
    fetch:async(...args)=>{calls.push(args);return transport ? transport() : response;}
  });
  return {file,remove,confirmation,image,button,status,form,calls,revoked,created};
}

test('background removal requires preview plus a separate save confirmation', async()=>{
  const p=previewPanel();p.remove.checked=true;
  let prevented=false;
  p.form.listeners.submit({preventDefault(){prevented=true;}});
  assert.equal(prevented,true);
  await p.button.listeners.click();
  assert.equal(p.calls[0][1].body.values.confirm_background,'1');
  assert.equal(p.calls[0][1].headers['X-CSRFToken'],'csrf-test');
  assert.equal(p.confirmation.disabled,false);
  assert.equal(p.confirmation.checked,false);
  prevented=false;p.confirmation.checked=true;
  p.form.listeners.submit({preventDefault(){prevented=true;}});
  assert.equal(prevented,false);
});

test('cancelled background preview never calls the server',async()=>{
  const p=previewPanel({confirm:false});p.remove.checked=true;
  await p.button.listeners.click();assert.equal(p.calls.length,0);
});

test('changing the file invalidates consent and frees old object URLs',async()=>{
  const p=previewPanel();p.remove.checked=true;await p.button.listeners.click();
  p.confirmation.checked=true;
  p.file.files=[{size:1024,type:'image/png'}];p.file.listeners.change();
  assert.equal(p.confirmation.checked,false);assert.equal(p.confirmation.disabled,true);
  assert.deepEqual(p.revoked,['blob:preview-1']);
  assert.equal(p.image.src,'blob:preview-2');
});

test('a stale preview cannot replace a newly selected image or enable confirmation',async()=>{
  let release;
  const p=previewPanel({transport:()=>new Promise(resolve=>{release=resolve;})});
  p.remove.checked=true;
  const pending=p.button.listeners.click();
  p.file.files=[{size:1000,type:'image/png'}];p.file.listeners.change();
  release({ok:true,redirected:false,headers:{get(){return 'image/webp';}},blob:async()=>({old:true})});
  await pending;
  assert.equal(p.created.length,1);assert.equal(p.created[0],p.file.files[0]);
  assert.equal(p.confirmation.disabled,true);
});

test('private preview rejects expired sessions and reports validation errors',async()=>{
  for(const response of [
    {ok:true,redirected:true,headers:{get(){return 'text/html';}}},
    {ok:false,headers:{get(){return 'application/json';}},json:async()=>({error:'Fundo não uniforme'})}
  ]) {
    const p=previewPanel({transport:async()=>response});await p.button.listeners.click();
    assert.equal(p.created.length,0);assert.equal(p.button.disabled,false);
    assert.equal(p.confirmation.disabled,true);assert.ok(p.status.textContent);
  }
});
