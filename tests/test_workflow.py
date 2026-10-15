"""Isolated test fixtures; never seed operational data."""
import sys,tempfile,unittest,copy,json,sqlite3,hashlib
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
import storage as db
import domain as rules
import app,logging
logging.getLogger('procureai').setLevel(logging.WARNING)
logging.getLogger('httpx').setLevel(logging.WARNING)

PASSWORD='test-only-password-2026'
class ProcurementTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();db.DATA=Path(self.temp.name);db.DB=db.DATA/'test.db';db.migrate()
        self.client=TestClient(app.app);self.client.__enter__();self.user_ids={}
        r=self.client.post('/api/setup',json={'email':'admin@test.local','name':'Administrator','password':PASSWORD});self.assertEqual(r.status_code,200)
        self.sign('admin')
        for role in ['buyer','procurement','warehouse','quality','finance','viewer']:
            r=self.post('/api/users',{'email':role+'@test.local','name':role.title(),'password':PASSWORD,'role':role});self.assertEqual(r.status_code,200,r.text);self.user_ids[role]=r.json()['id']
        self.sign('buyer');r=self.post('/api/cases',{'title':'TEST ONLY: security lock procurement','material':'TEST-LOCK','quantity':'300','deadline':'2027-01-15','min_quality':'80','certifications':['ISO'],'currency':'USD'});self.assertEqual(r.status_code,200,r.text);self.case=r.json()
    def tearDown(self):self.client.__exit__(None,None,None);self.temp.cleanup()
    def sign(self,role):
        r=self.client.post('/api/login',json={'email':role+'@test.local','password':PASSWORD});self.assertEqual(r.status_code,200,r.text);self.csrf=r.json()['csrf']
    def post(self,path,body,headers=None):return self.client.post(path,json=body,headers={'X-CSRF-Token':self.csrf,'Idempotency-Key':rules.uid(),**(headers or {})})
    def act(self,a,p=None,expected=200,key=None,version=None):
        r=self.post(f"/api/cases/{self.case['id']}/actions/{a}",{'version':version or self.case['version'],'payload':p or {}},{'Idempotency-Key':key or rules.uid()});self.assertEqual(r.status_code,expected,r.text)
        if r.status_code==200:self.case=r.json()
        return r
    def quotes(self):
        for supplier,price,quality in [('Aster Components','25.50','95'),('Brookfield Supply','26.00','90'),('Cedar Industrial','29.00','85')]:
            self.act('quote',{'supplier':supplier,'quantity':'300','unit_price':price,'quality':quality,'reliability':'95','risk':'10','delivery_date':'2027-01-10','certifications':['ISO'],'payment_terms':'Net 30'})
        self.act('rank')
    def approved(self):
        self.quotes();self.act('supplier_submit');self.sign('procurement');self.act('supplier_decision',{'decision':'approve'});self.sign('buyer');self.act('po_submit');self.sign('procurement');self.act('po_decision',{'decision':'approve'})
    def receive(self,n='300',ref='DELIVERY-1',accepted=None):
        self.sign('warehouse');self.act('receipt',{'quantity':n,'reference':ref,'date':'2027-01-10'});self.sign('quality');self.act('quality',{'receipt_id':self.case['receipts'][-1]['id'],'accepted':accepted or n,'reason':'Count and inspection confirmed from test fixture'})
    def invoice(self,n='300',total='7650.00',ref='BILL-1'):
        self.sign('buyer');self.act('invoice',{'reference':ref,'quantity':n,'unit_price':'25.50','total':total,'date':'2027-01-11','due_date':'2027-02-10'});self.act('match')
    def finance(self):
        self.sign('buyer');self.act('finance_submit');self.sign('finance');self.act('finance_decision',{'decision':'approve'})
    def evidence(self):
        r=self.client.post(f"/api/cases/{self.case['id']}/documents",files={'file':('bank.txt',b'TEST ONLY externally reconciled payment evidence','text/plain')},headers={'X-CSRF-Token':self.csrf});self.assertEqual(r.status_code,200,r.text);return r.json()['id']
    def pay(self,n='7650',ref='BANK-1'):
        self.act('payment_record',{'amount':n,'reference':ref,'date':'2027-01-12','document_id':self.evidence(),'reason':'Verified external bank record in test fixture'})
    def test_complete_cycle(self):
        self.approved();self.receive();self.invoice();self.finance();self.pay();self.assertEqual(self.case['status'],'COMPLETED');self.assertEqual(rules.summary(self.case)['balance'],'0.00')
        with db.connect() as c:self.assertTrue(db.verify_audit(c));self.assertGreater(c.execute('SELECT COUNT(*) FROM versions').fetchone()[0],10)
    def test_ranking_reproducible(self):
        self.quotes();r=self.case['recommendations'][-1];self.assertEqual(r['rows'][0]['supplier'],'Aster Components');self.assertEqual(r['rows'][0]['landed_cost'],'7650.00');self.assertEqual(rules.rank(self.case)['rows'],r['rows']);self.assertEqual(r['algorithm'],rules.ALGORITHM)
    def test_no_eligible_suppliers(self):
        self.quotes();c=copy.deepcopy(self.case);c['min_quality']='99';r=rules.rank(c);self.assertIsNone(r['winner']);self.assertTrue(all(not r['eligible'] for r in r['rows']))
    def test_currency_constraint(self):
        self.quotes();c=copy.deepcopy(self.case);c['quotes'][0]['currency']='EUR';r=rules.rank(c);row=next(x for x in r['rows'] if x['quote_id']==c['quotes'][0]['id']);self.assertFalse(row['eligible'])
    def test_bad_weight_sum(self):
        self.quotes();self.act('rank',{'weights':{'cost':99}},422)
    def test_nonfinite_amount(self):
        self.act('quote',{'supplier':'NaN supplier','quantity':'300','unit_price':'NaN','quality':'95','reliability':'95','risk':'10','delivery_date':'2027-01-10'},422)
    def test_mandatory_supplier_count(self):self.act('rank',{},422)
    def test_supplier_override_requires_reason(self):
        self.quotes();other=self.case['recommendations'][-1]['rows'][1]['quote_id'];self.act('supplier_submit',{'quote_id':other},422);self.act('supplier_submit',{'quote_id':other,'reason':'Reviewed sourcing concentration risk'});self.assertEqual(self.case['selected'],other)
    def test_approval_gate(self):self.act('po_submit',{},409)
    def test_buyer_cannot_approve(self):
        self.quotes();self.act('supplier_submit');self.act('supplier_decision',{'decision':'approve'},403)
    def test_self_approval_after_role_change(self):
        self.quotes();self.act('supplier_submit')
        with db.transaction() as c:c.execute("UPDATE users SET role='procurement' WHERE id=?",(self.user_ids['buyer'],))
        self.act('supplier_decision',{'decision':'approve'},403)
    def test_viewer_cannot_modify(self):self.sign('viewer');self.act('rank',{},403)
    def test_csrf_required(self):
        r=self.client.post('/api/preferences',json={'theme':'dark'});self.assertEqual(r.status_code,403)
    def test_origin_rejected(self):
        r=self.post('/api/preferences',{'theme':'dark'},{'Origin':'https://attacker.invalid'});self.assertEqual(r.status_code,403)
    def test_unauthenticated_api(self):
        self.post('/api/logout',{});self.assertEqual(self.client.get('/api/cases').status_code,401)
    def test_theme_persistence(self):
        self.post('/api/preferences',{'theme':'dark'});self.sign('buyer');self.assertEqual(self.client.get('/api/me').json()['user']['theme'],'dark')
    def test_idempotency_and_changed_payload(self):
        self.quotes();v=self.case['version'];key=rules.uid();self.act('supplier_submit',{},key=key);after=self.case['version'];self.act('supplier_submit',{},key=key,version=v);self.assertEqual(self.case['version'],after);self.act('supplier_submit',{'reason':'different'},409,key=key,version=v)
    def test_stale_write(self):
        self.quotes();self.act('supplier_submit',{},409,version=1)
    def test_concurrent_writes_one_wins(self):
        self.quotes();body={'version':self.case['version'],'payload':{}}
        cookies=dict(self.client.cookies)
        def send(_):
            with TestClient(app.app) as client:
                client.cookies.update(cookies);return client.post(f"/api/cases/{self.case['id']}/actions/supplier_submit",json=body,headers={'X-CSRF-Token':self.csrf,'Idempotency-Key':rules.uid()}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:statuses=list(pool.map(send,range(2)))
        self.assertEqual(sorted(statuses),[200,409])
    def test_approval_invalidation(self):
        self.approved();self.sign('buyer');self.act('commercial_change',{'quantity':'320','reason':'Updated requirements'});self.assertIsNone(self.case['po']);self.assertIsNone(self.case['supplier_approval']);self.assertEqual(self.case['status'],'QUOTATIONS');self.act('supplier_submit',{},409)
    def test_posted_record_change_blocked(self):
        self.approved();self.receive();self.sign('buyer');self.act('commercial_change',{'quantity':'310','reason':'Cannot overwrite receipt'},409)
    def test_partial_delivery_then_completion(self):
        self.approved();self.receive('100');self.assertEqual(self.case['status'],'RECEIVING');self.receive('200','DELIVERY-2');self.assertEqual(self.case['status'],'INVOICE');self.assertEqual(rules.summary(self.case)['accepted'],'300')
    def test_quality_rejection_and_replacement(self):
        self.approved();self.receive('300',accepted='280');self.assertEqual(self.case['status'],'RECEIVING');self.receive('20','REPLACEMENT');self.assertEqual(rules.summary(self.case)['accepted'],'300')
    def test_over_receipt(self):
        self.approved();self.sign('warehouse');self.act('receipt',{'quantity':'301','reference':'OVER','date':'2027-01-10'},422)
    def test_duplicate_receipt(self):
        self.approved();self.receive('100');self.sign('warehouse');self.act('receipt',{'quantity':'100','reference':'DELIVERY-1','date':'2027-01-10'},409)
    def test_mismatch_blocks_finance(self):
        self.approved();self.receive();self.invoice(total='7800');self.assertEqual(self.case['status'],'INVOICE_BLOCKED');self.act('finance_submit',{},409);self.act('invoice_correct',{'total':'7650','reason':'Supplier corrected billing error'});self.act('match');self.assertEqual(self.case['status'],'MATCHED')
    def test_invoice_above_accepted_quantity(self):
        self.approved();self.receive('100');self.invoice();self.assertEqual(self.case['status'],'INVOICE_BLOCKED')
    def test_duplicate_invoice(self):
        self.approved();self.receive('100');self.invoice('100','2550');self.finance();self.pay('2550');self.sign('buyer');self.act('invoice',{'reference':'BILL-1','quantity':'100','unit_price':'25.5','total':'2550','date':'2027-01-11','due_date':'2027-02-10'},409)
    def test_partial_invoice_payment_and_completion(self):
        self.approved();self.receive('100');self.invoice('100','2550');self.finance();self.pay('2550');self.assertEqual(self.case['status'],'RECEIVING');self.receive('200','DELIVERY-2');self.invoice('200','5100','BILL-2');self.finance();self.pay('5100','BANK-2');self.assertEqual(self.case['status'],'COMPLETED')
    def test_payment_requires_evidence(self):
        self.approved();self.receive();self.invoice();self.finance();self.act('payment_record',{'amount':'7650','reference':'BANK','date':'2027-01-12','reason':'Missing attachment'},422)
    def test_overpayment(self):
        self.approved();self.receive();self.invoice();self.finance();self.act('payment_record',{'amount':'7651','reference':'BANK','date':'2027-01-12','document_id':self.evidence(),'reason':'Test'},422)
    def test_partial_payment(self):
        self.approved();self.receive();self.invoice();self.finance();self.pay('2000');self.assertEqual(self.case['status'],'PAYMENT_PENDING');self.pay('5650','BANK-2');self.assertEqual(self.case['status'],'COMPLETED')
    def test_bad_document_type(self):
        r=self.client.post(f"/api/cases/{self.case['id']}/documents",files={'file':('fake.pdf',b'<script>alert(1)</script>','application/pdf')},headers={'X-CSRF-Token':self.csrf});self.assertEqual(r.status_code,422)
    def test_document_download_auth_and_hash(self):
        ident=self.evidence();r=self.client.get('/api/documents/'+ident);self.assertEqual(r.status_code,200)
        with db.connect() as c:row=c.execute('SELECT sha256 FROM documents WHERE id=?',(ident,)).fetchone();self.assertEqual(row['sha256'],hashlib.sha256(r.content).hexdigest())
        self.post('/api/logout',{});self.assertEqual(self.client.get('/api/documents/'+ident).status_code,401)
    def test_append_only_audit(self):
        with db.transaction() as c:
            with self.assertRaises(sqlite3.IntegrityError):c.execute('DELETE FROM audit')
    def test_integration_disconnected(self):
        self.approved();self.sign('buyer');self.act('transmit_order',{},503);self.assertEqual(self.client.get('/api/integrations').json()['sap']['status'],'Not connected')
    def test_gateway_timeout_retry_same_key(self):
        self.approved();self.sign('buyer')
        with patch('adapters.status',return_value={'sap':{'available':True}}),patch('adapters.submit',side_effect=TimeoutError):
            self.act('transmit_order');app.process_job()
        with db.connect() as c:j=dict(c.execute('SELECT * FROM jobs').fetchone())
        self.assertEqual(j['status'],'UNKNOWN');self.assertIsNone(self.case['po']['external_reference']);self.assertEqual(self.post('/api/jobs/'+j['id']+'/retry',{}).status_code,200)
        with patch('adapters.submit',return_value={'status':'confirmed','reference':'EXT-PO-44'}) as submit:app.process_job();self.assertEqual(submit.call_args.args[2],j['id'])
        current=self.client.get('/api/cases/'+self.case['id']).json();self.assertEqual(current['po']['external_reference'],'EXT-PO-44')
    def test_deactivate_revokes_sessions(self):
        self.sign('admin');self.post('/api/users/'+self.user_ids['buyer']+'/access',{'active':False});r=self.client.post('/api/login',json={'email':'buyer@test.local','password':PASSWORD});self.assertEqual(r.status_code,401)
    def test_password_change_revokes_sessions(self):
        self.post('/api/password',{'current_password':PASSWORD,'new_password':'changed-test-password-2026'});self.assertEqual(self.client.get('/api/me').status_code,401)
    def test_login_lockout(self):
        for _ in range(8):self.assertEqual(self.client.post('/api/login',json={'email':'viewer@test.local','password':'wrong'}).status_code,401)
        self.assertEqual(self.client.post('/api/login',json={'email':'viewer@test.local','password':PASSWORD}).status_code,429)
    def test_dashboard_no_invented_stats(self):
        self.assertEqual(self.client.get('/api/dashboard').json()['po_total'],'0.00');self.approved();dash=self.client.get('/api/dashboard').json();self.assertEqual(dash['po_total'],'7650.00');self.assertEqual(dash['suppliers'][0]['amount'],'7650.00')
    def test_unknown_submission_blocks_amendment(self):
        self.approved();self.sign('buyer')
        with patch('adapters.status',return_value={'sap':{'available':True}}):self.act('transmit_order')
        self.act('commercial_change',{'quantity':'310','reason':'Pending external order'},409)
    def test_sensitivity_snapshots(self):
        self.quotes();r=self.case['recommendations'][-1];self.assertEqual(len(r['sensitivity']),8)
        for s in r['sensitivity']:self.assertEqual(sum(rules.dec(v) for v in s['weights'].values()),100)
    def test_malformed_payload_rejected(self):
        self.act('quote',{'supplier':{},'quantity':'300'},422);self.act('cancel',{'reason':[]},422)
    def test_creation_idempotency(self):
        p={'title':'Duplicate protection','material':'T-2','quantity':'2','deadline':'2027-01-15'};key=rules.uid()
        r1=self.post('/api/cases',p,{'Idempotency-Key':key});r2=self.post('/api/cases',p,{'Idempotency-Key':key});self.assertEqual(r1.json()['id'],r2.json()['id'])
    def test_backup_and_restore(self):
        self.evidence();from ops.backup import backup
        target=Path(self.temp.name)/'backup.zip';backup(target)
        import zipfile
        with zipfile.ZipFile(target) as z:self.assertIsNone(z.testzip());self.assertEqual(len([x for x in z.namelist() if x.startswith('documents/')]),1)
        from ops.restore import restore
        destination=Path(self.temp.name)/'restored';restore(target,destination);self.assertTrue((destination/'procure.db').is_file());self.assertEqual(len(list((destination/'documents').iterdir())),1)
    def test_invoice_tolerance_revision_requires_reapproval(self):
        self.approved();self.sign('buyer');self.act('commercial_change',{'amount_tolerance':'0.05','reason':'Reviewed rounding policy'});self.assertEqual(self.case['amount_tolerance'],'0.05');self.assertIsNone(self.case['po'])
if __name__=='__main__':unittest.main(verbosity=2)
