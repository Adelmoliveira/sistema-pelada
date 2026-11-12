"""Execute the actual submit handler without claiming a rendered browser test."""
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


class CancellationFeedbackTest(unittest.TestCase):
    def test_confirmed_value_failures_and_retry_idempotency(self):
        source=Path('templates/orders.html').read_text()
        handler=re.search(r"cancelForm.addEventListener\('submit',(async event=>\{.*?)\);\npendingBox.addEventListener\('input'",source,re.S).group(1)
        money=re.search(r'^function money\(cents\).*$',source,re.M).group(0)
        javascript='const handler='+handler+';\n'+money+'\n'+r'''
const assert=require('node:assert/strict');
let mode='success',bodies=[],refreshes=0,closed=0;
const message={textContent:''},error={textContent:''},submit={disabled:false};
const cancelSuccess={hidden:true,scrollIntoView(){}};
const cancelDraft={saleId:10,itemId:20,key:'stable-operation-key'};
const cancelQuantity={value:'1'};
const cancelForm={reportValidity:()=>true,querySelector:()=>submit};
const cancelDialog={close(){closed++}};
const cancelItemBase='/orders/0/cancel-item',csrfToken='synthetic';
const document={querySelector(selector){return selector==='#cancel-item-success-message'?message:selector==='#cancel-item-error'?error:{value:'Justificativa sintética'}}};
const refresh=async()=>{refreshes++};
const fetch=async(url,options)=>{
 bodies.push(JSON.parse(options.body));
 if(mode==='network')throw new Error('Conexão interrompida');
 return {ok:!['server-error','rejected'].includes(mode),status:mode==='server-error'?500:mode==='rejected'?409:200,
  json:async()=>{if(mode==='invalid-json')throw new Error('Resposta incompleta');
   return {ok:mode!=='unconfirmed',error:'Não confirmado',operation:{sale_id:10,sale_item_id:20,
    idempotency_key:mode==='wrong-operation'?'other-key':'stable-operation-key',amount_cents:1375}};}};
};
const event={preventDefault(){}};
(async()=>{
 for(mode of ['network','server-error','invalid-json','unconfirmed','wrong-operation']){
  cancelSuccess.hidden=true;message.textContent='';
  await handler(event);
  assert.equal(cancelSuccess.hidden,true,mode);
  assert.equal(message.textContent,'',mode);
  assert.equal(closed,0,mode);
  assert.equal(submit.disabled,false,mode);
  assert.equal(cancelDraft.body.idempotency_key,'stable-operation-key',mode);
 }
 mode='success';await handler(event);
 assert.equal(cancelSuccess.hidden,false);
 assert.equal(message.textContent.replace(/\s/g,' '),'Item cancelado com sucesso. R$ 13,75 adicionados aos créditos do peladeiro. Estoque atualizado.');
 assert.equal(closed,1);assert.equal(refreshes,1);
 assert.ok(bodies.every(body=>body.idempotency_key==='stable-operation-key'));
 assert.ok(bodies.every(body=>JSON.stringify(body)===JSON.stringify(bodies[0])));
 // Replaying an uncertain request shows its confirmed outcome with the same key.
 await handler(event);assert.equal(cancelSuccess.hidden,false);
 assert.equal(bodies.at(-1).idempotency_key,'stable-operation-key');
 mode='rejected';cancelSuccess.hidden=true;await handler(event);
 assert.equal(cancelSuccess.hidden,true);
 console.log('Feedback confirmado, valor do servidor, falhas e repetição: OK');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        with tempfile.TemporaryDirectory() as directory:
            script=Path(directory)/'feedback.js';script.write_text(javascript)
            result=subprocess.run(['node',str(script)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('id="cancel-item-success"',source)
        self.assertIn('aria-live="polite"',source)
