import re
import subprocess
import unittest
from pathlib import Path


class PixModalLifecycleTest(unittest.TestCase):
    def test_close_restores_checkout_and_late_response_cannot_lock_it(self):
        source=Path('templates/sale.html').read_text()
        lifecycle=re.search(r"let pixModalVisible=false;.*?hidden.bs.modal.*?\n",source,re.S).group()
        script='''const assert=require('node:assert/strict');
const fields=[{disabled:false},{disabled:false}];
const pixButton={disabled:false};
const saleForm={querySelectorAll:()=>fields};
let visible=true,restoreCalls=0;
const handlers={};
const pixModalElement={classList:{contains:()=>visible},addEventListener:(name,fn)=>handlers[name]=fn};
function toggleSaleTarget(){restoreCalls++;}
'''+lifecycle+'''
for(const flow of ['cash','bar2x','sports3x']){
 visible=true;handlers['show.bs.modal']();lockCheckout(true);assert(fields.every(f=>f.disabled));assert(pixButton.disabled);
 const financial={sale:'pending',attempt:'pending',stock:76};
 const before=JSON.stringify(financial);
 visible=false;handlers['hide.bs.modal']();handlers['hidden.bs.modal']();
 assert(fields.every(f=>!f.disabled));assert.equal(pixButton.disabled,false);
 lockCheckout(true);assert(fields.every(f=>!f.disabled));
 assert.equal(JSON.stringify(financial),before);
 visible=true;handlers['show.bs.modal']();lockCheckout(true);assert(pixButton.disabled);
 visible=false;handlers['hide.bs.modal']();handlers['hidden.bs.modal']();
}
assert.equal(restoreCalls,6);
'''
        result=subprocess.run(['node','-e',script],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn('fetch(',lifecycle)
        self.assertNotIn('remove()',lifecycle)
        self.assertIn('data-bs-dismiss="modal"',source)

    def test_installment_flows_share_bootstrap_instance_without_financial_close(self):
        for name in ('bar_installments.js','sports_installments.js'):
            source=Path('static',name).read_text()
            self.assertIn("bootstrap.Modal.getOrCreateInstance(document.querySelector('#pix-modal'))",source)
            handler=re.search(r"addEventListener\('hidden.bs.modal'.*",source).group()
            self.assertIn('clearTimeout(timer)',handler)
            self.assertNotIn('fetch',handler)
            self.assertNotIn('cancel',handler)
