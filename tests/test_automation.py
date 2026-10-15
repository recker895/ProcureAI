"""Isolated fixtures only; no real outbound messages, orders or payments."""
import sys,os,tempfile,unittest,json,copy,time,hmac,hashlib
from pathlib import Path
from unittest.mock import patch
from decimal import Decimal
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import storage as db,domain as d,ai_domain as business,optimization as opt,automation as engine,groq_provider as groq,document_intake,app,automation_api
from fastapi.testclient import TestClient

def fixture_assess(agent,snapshot,evidence,sources,executor=None,extraction=False):
    result=executor() if executor else {}
    return {'assessment':{'recommendation':'TEST FIXTURE assessment: '+groq.AGENTS[agent],'reasons':['Review the validated input snapshot and mandatory gates.'],'alternatives':['Escalate missing evidence.'],'uncertainties':['Isolated test data, not operational evidence.'],'source_ids':[snapshot['id']],'next_action':'Follow the server-validated workflow.'},'tools':[{'name':'calculate','arguments':{},'result':result}],'proposals':[],'document':None,'model':'TEST-FIXTURE-NO-LIVE-LLM','prompt_version':groq.PROMPT_VERSION,'input_tokens':10,'output_tokens':10,'elapsed_ms':1,'cost':None}

class OptimizationTests(unittest.TestCase):
    def setUp(self):
        self.case=d.create_case({'title':'TEST ONLY solver','material':'LOCK','quantity':'300','deadline':'2027-01-15'}, {'id':'BUYER'})
        self.case['quotes']=[d.quotation({'supplier':s,'quantity':qty,'unit_price':price,'quality':'95','reliability':'95','risk':'10','delivery_date':'2027-01-10'},self.case) for s,qty,price in [('A','200','25.50'),('B','300','26'),('C','300','29')]];self.case['optimization']={'max_suppliers':2}
    def test_split_capacity_and_exact_cost(self):
        r=opt.solve(self.case);self.assertTrue(r['optimal']);self.assertEqual(r['total'],'7700.00');self.assertEqual([(x['supplier'],x['quantity']) for x in r['allocations']],[('A','200'),('B','100')]);self.assertEqual(r['solver'],'HiGHS')
    def test_budget_infeasible(self):
        self.case['optimization']['budget']='7600';r=opt.solve(self.case);self.assertFalse(r['allocations']);self.assertIn('Infeasible',r['solver_status'])
    def test_moq_and_supplier_count(self):
        self.case['quotes'][0]['minimum_order']='250';self.case['optimization']['max_suppliers']=1;r=opt.solve(self.case);self.assertEqual(r['allocations'][0]['supplier'],'B');self.assertEqual(r['total'],'7800.00')
    def test_share_and_qualifications(self):
        self.case['optimization']['max_share']='50';r=opt.solve(self.case);self.assertEqual(sorted(x['quantity'] for x in r['allocations']),['150','150']);self.case['certifications']=['MISSING'];self.assertFalse(opt.solve(self.case)['allocations'])
    def test_freight_tax_and_penalties(self):
        self.case['quotes'][0].update(freight='100.00',discount='0.00',tax_rate='10');self.case['optimization']['objective']='cost_risk_quality';self.case['optimization']['risk_penalty']='1';r=opt.solve(self.case);self.assertEqual(r['allocations'][0]['supplier'],'B')
    def test_reproducible_snapshot(self):
        a=opt.solve(self.case);b=opt.solve(self.case);self.assertEqual(a['allocations'],b['allocations']);self.assertEqual(a['inputs'],b['inputs']);self.assertTrue(a['alternatives'])

class AutomationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.password=app.password_hash('isolated-test-password-2026')
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.old_data,self.old_db=db.DATA,db.DB;db.DATA=Path(self.temp.name);db.DB=db.DATA/'test.db';db.migrate();self.roles={r:{'id':'TEST-'+r,'role':r,'name':r} for r in ['buyer','procurement','warehouse','quality','finance','admin','viewer']}
        with db.transaction() as c:
            for r,u in self.roles.items():c.execute('INSERT INTO users(id,email,name,role,password) VALUES(?,?,?,?,?)',(u['id'],r+'@test.local',r,r,self.password))
            data=d.create_case({'title':'TEST ONLY isolated workflow','material':'LOCK','quantity':'300','deadline':'2027-01-15'},self.roles['buyer']);data.update(mode='manual',record_kind='test',mode_epoch=0);db.save(c,data);self.ident=data['id']
        self.mock=patch('groq_provider.assess',side_effect=fixture_assess);self.mock.start();self.gateway=patch('adapters.config',return_value=('https://test.invalid','TEST-TOKEN'));self.gateway.start();self.client=TestClient(app.app)
    def tearDown(self):self.mock.stop();self.gateway.stop();self.client.close();db.DATA,self.old_data=self.old_data,db.DATA;db.DB=self.old_db;self.temp.cleanup()
    def data(self):
        with db.connect() as c:return engine.load(c,self.ident)
    def act(self,a,p=None,role='buyer'):
        with db.transaction() as c:
            data=business.action(engine.load(c,self.ident),a,p or {},self.roles[role]);data['version']+=1;db.save(c,data);engine.schedule(c,data);return data
    def switch(self,mode):
        with db.transaction() as c:return engine.switch(c,engine.load(c,self.ident),mode,self.roles['buyer'],'TEST mode switch')
    def quotes(self):
        for supplier,price in [('A','25.50'),('B','26'),('C','29')]:self.act('quote',{'supplier':supplier,'quantity':'300','unit_price':price,'quality':'95','reliability':'95','risk':'10','delivery_date':'2027-01-10','payee_reference':'TEST-BENEFICIARY-'+supplier})
    def drain(self):
        for _ in range(50):
            if not engine.process_one():break
        else:self.fail('Automation did not reach a stable checkpoint')
    def approved(self):
        self.quotes();self.switch('ai');self.drain();self.assertEqual(self.data()['status'],'SUPPLIER_REVIEW');self.act('supplier_decision',{'decision':'approve','reason':'TEST human-role checkpoint'},'procurement');self.drain();self.assertEqual(self.data()['status'],'PO_REVIEW');self.act('po_decision',{'decision':'approve','reason':'TEST PO checkpoint'},'procurement');self.drain()
    def receive(self,n='300',reference='TEST-DELIVERY'):
        self.act('receipt',{'quantity':n,'reference':reference,'date':'2027-01-10'},'warehouse');self.drain();r=self.data()['receipts'][-1];self.act('quality',{'receipt_id':r['id'],'accepted':n,'reason':'TEST inspection confirmed by quality-role fixture'},'quality');self.drain()
    def invoice(self,n='300',total='7650',ref='TEST-INVOICE'):
        self.act('invoice',{'reference':ref,'quantity':n,'unit_price':'25.50','total':total,'date':'2027-01-11','due_date':'2027-02-10'});self.drain()
    def proof(self):
        ident=d.uid('DOC-');body=b'TEST ONLY: no money transferred';(db.DATA/'documents'/ident).write_bytes(body)
        with db.transaction() as c:c.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?)',(ident,self.ident,'TEST-finance','test.txt','text/plain',len(body),hashlib.sha256(body).hexdigest(),d.now()))
        return ident
    def test_complete_ai_cycle_with_mandatory_role_checkpoints(self):
        self.approved();self.receive();self.invoice();self.assertEqual(self.data()['status'],'FINANCE_REVIEW');self.act('finance_decision',{'decision':'approve','reason':'TEST finance checkpoint'},'finance');self.drain();self.assertEqual(self.data()['status'],'PAYMENT_PENDING');self.act('payment_record',{'amount':'7650','reference':'TEST-BANK','date':'2027-01-12','document_id':self.proof(),'reason':'TEST ONLY no real payment'},'finance');self.drain();self.assertEqual(self.data()['status'],'COMPLETED')
        with db.connect() as c:self.assertTrue(db.verify_audit(c));self.assertGreater(c.execute('SELECT COUNT(*) FROM agent_runs').fetchone()[0],5)
    def test_mode_switch_fences_queued_work(self):
        self.quotes();self.switch('ai');self.switch('manual');self.assertFalse(engine.process_one());self.assertEqual(self.data()['status'],'QUOTATIONS');self.assertEqual(self.data()['mode'],'manual');self.switch('ai');self.drain();self.assertEqual(self.data()['status'],'SUPPLIER_REVIEW')
    def test_agent_cannot_approve(self):
        with db.transaction() as c:
            with self.assertRaises(d.RuleError) as error:engine.do_action(c,self.data(),6,'supplier_decision',{'decision':'approve'})
            self.assertEqual(error.exception.status,403)
    def test_human_role_and_separation_checks(self):
        self.quotes();self.switch('ai');self.drain()
        with self.assertRaises(d.RuleError):self.act('supplier_decision',{'decision':'approve'},'buyer')
        user={'id':self.roles['buyer']['id'],'role':'procurement'}
        with self.assertRaises(d.RuleError):business.action(self.data(),'supplier_decision',{'decision':'approve'},user)
    def test_no_duplicate_agent_submission(self):
        self.quotes();self.switch('ai');self.drain();before=self.data()['version']
        for _ in range(3):
            with db.transaction() as c:engine.schedule(c,engine.load(c,self.ident))
            self.drain()
        self.assertEqual(before,self.data()['version']);self.assertEqual(len(self.data()['recommendations']),1)
    def test_changed_terms_invalidate_approvals(self):
        self.approved();data=self.act('commercial_change',{'quantity':'250','reason':'TEST revised demand'});self.assertIsNone(data['po']);self.assertIsNone(data['supplier_approval']);self.assertEqual(data['status'],'QUOTATIONS')
    def test_missing_connection_is_visible_exception(self):
        self.quotes();self.switch('ai')
        with patch.dict(os.environ,{'GROQ_API_KEY':''}):
            self.mock.stop();engine.process_one();self.mock.start()
        with db.connect() as c:self.assertIn('not connected',c.execute("SELECT error FROM agent_tasks WHERE status='BLOCKED'").fetchone()[0].lower())
    def test_test_record_cannot_queue_live_external_actions(self):
        with db.transaction() as c:
            with self.assertRaises(d.RuleError):engine.queue_gateway(c,engine.load(c,self.ident),'sap',{},['TEST'])
        with db.connect() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],0)
    def test_recovery_of_expired_lease(self):
        self.switch('ai')
        with db.transaction() as c:c.execute("UPDATE agent_tasks SET status='RUNNING',lease_until=0,lease_owner='dead',attempts=1 WHERE agent=1")
        self.drain()
        with db.connect() as c:self.assertEqual(c.execute('SELECT status FROM agent_tasks WHERE agent=1').fetchone()[0],'COMPLETED')
    def test_override_resolves_constraints_and_requires_reason(self):
        self.quotes();self.switch('ai');self.drain();other=self.data()['quotes'][1]['id']
        with self.assertRaises(d.RuleError):self.act('supplier_decision',{'decision':'override','override_quote':other},'procurement')
        result=self.act('supplier_decision',{'decision':'override','override_quote':other,'reason':'TEST supplier alternative'},'procurement');self.assertEqual(result['selected'],other);self.assertTrue(result['supplier_approval']['override_reason'])
    def test_invoice_exception_does_not_advance_to_finance(self):
        self.approved();self.receive();self.invoice(total='9000');self.assertEqual(self.data()['status'],'INVOICE_BLOCKED');self.assertFalse(self.data()['invoices'][-1].get('approval'))
    def test_partial_delivery_and_partial_invoice(self):
        self.approved();self.receive('100');self.invoice('100','2550');self.assertEqual(self.data()['status'],'FINANCE_REVIEW');self.assertEqual(d.summary(self.data())['accepted'],'100')
    def test_assessment_is_immutable_and_source_snapshots_are_saved(self):
        self.switch('ai');self.drain()
        with db.transaction() as c:
            row=c.execute('SELECT * FROM agent_runs LIMIT 1').fetchone();self.assertIn('case',json.loads(row['input']));self.assertTrue(json.loads(row['tools']))
            with self.assertRaises(Exception):c.execute("UPDATE agent_runs SET model='tampered'")
    def test_api_mode_idempotency_default_does_not_change_existing(self):
        auth=self.client.post('/api/login',json={'email':'buyer@test.local','password':'isolated-test-password-2026'}).json();headers={'X-CSRF-Token':auth['csrf'],'Idempotency-Key':'MODE-TEST'}
        body={'version':self.data()['version'],'payload':{'mode':'ai','reason':'TEST'}};url=f'/api/cases/{self.ident}/actions/mode';first=self.client.post(url,json=body,headers=headers);self.assertEqual(first.status_code,200,first.text);second=self.client.post(url,json=body,headers=headers);self.assertEqual(first.json(),second.json())
        self.client.post('/api/preferences',json={'default_mode':'manual'},headers=headers);self.assertEqual(self.data()['mode'],'ai')
    def test_signed_webhook_rejects_bad_signatures(self):
        raw=b'{}'
        with patch.dict(os.environ,{'WAREHOUSE_WEBHOOK_SECRET':'TEST-SECRET'}):r=self.client.post('/api/integration-events/warehouse',content=raw,headers={'X-Event-Timestamp':str(time.time()),'X-Event-Signature':'bad'})
        self.assertEqual(r.status_code,403)
    def test_extraction_requires_exact_supported_sources(self):
        good={'fields':{'quantity':'300'},'sources':{'quantity':'Quantity: 300'},'missing':[]};self.assertEqual(document_intake.validate_proposal(good,'Quantity: 300','quotation')['fields']['quantity'],'300')
        good['fields']['quantity']='900'
        with self.assertRaises(d.RuleError):document_intake.validate_proposal(good,'Quantity: 300','quotation')
    def test_image_requires_configured_ocr(self):
        with self.assertRaises(d.RuleError):document_intake.extract(b'fake image','image/png')

    def test_multi_supplier_orders_and_receipt_boundaries(self):
        self.quotes();q=self.data()['quotes'][0];self.act('quote',{**q,'quantity':'200'});self.act('optimization_config',{'reason':'TEST split allocation','configuration':{'max_suppliers':2}});self.switch('ai');self.drain();self.act('supplier_decision',{'decision':'approve'},'procurement');self.drain();data=self.data();self.assertEqual(len(data['po']['orders']),2);self.assertEqual(len({x['id'] for x in data['po']['orders']}),2);self.assertEqual(data['po']['total'],'7700.00');self.act('po_decision',{'decision':'approve'},'procurement')
        second=next(x for x in self.data()['po']['lines'] if x['supplier']=='B')
        with self.assertRaises(d.RuleError):self.act('receipt',{'quote_id':second['quote_id'],'quantity':'200','reference':'TEST-OVER','date':'2027-01-10'},'warehouse')
        self.act('receipt',{'quote_id':second['quote_id'],'quantity':'100','reference':'TEST-B-DELIVERY','date':'2027-01-10'},'warehouse');r=self.data()['receipts'][-1];self.act('quality',{'receipt_id':r['id'],'accepted':'100','reason':'TEST B inspection'},'quality')
        first=next(x for x in self.data()['po']['lines'] if x['supplier']=='A');self.act('invoice',{'quote_id':first['quote_id'],'quantity':'100','unit_price':'25.50','total':'2550','reference':'TEST-A-NO-RECEIPT','date':'2027-01-11','due_date':'2027-02-10'});self.drain();self.assertEqual(self.data()['status'],'INVOICE_BLOCKED')
        self.client.post('/api/login',json={'email':'buyer@test.local','password':'isolated-test-password-2026'});operational=self.client.get('/api/dashboard').json();self.assertEqual(operational['po_total'],'0.00');test_scope=self.client.get('/api/dashboard?record_kind=test').json();self.assertEqual(test_scope['po_total'],'7700.00');self.assertEqual({x['name']:x['amount'] for x in test_scope['suppliers']},{'A':'5100.00','B':'2600.00'})

    def enable_operational_fixture(self):
        with db.transaction() as c:c.execute("UPDATE case_controls SET record_kind='operational' WHERE case_id=?",(self.ident,))

    def test_gateway_acknowledgment_does_not_fabricate_payment(self):
        self.approved();self.receive();self.invoice();self.act('finance_decision',{'decision':'approve'},'finance');self.enable_operational_fixture();data=self.data();i=data['invoices'][-1]
        with db.transaction() as c:engine.queue_gateway(c,data,'payment',{'operation':'payment','invoice_id':i['id'],'amount':i['total'],'currency':data['currency'],'payee_reference':i['approval']['payee_reference']},['TEST-PAYMENT'])
        with patch('adapters.submit',return_value={'status':'confirmed','reference':'TEST-ACK'}):app.process_job()
        self.assertFalse(self.data()['payments']);self.assertEqual(self.data()['status'],'PAYMENT_PENDING')
        with db.connect() as c:self.assertEqual(c.execute('SELECT status FROM jobs LIMIT 1').fetchone()[0],'UNKNOWN')

    def test_gateway_settlement_matches_approved_invoice(self):
        self.approved();self.receive();self.invoice();self.act('finance_decision',{'decision':'approve'},'finance');self.enable_operational_fixture();data=self.data();i=data['invoices'][-1]
        with db.transaction() as c:engine.queue_gateway(c,data,'payment',{'operation':'payment','invoice_id':i['id'],'amount':i['total'],'currency':data['currency'],'payee_reference':i['approval']['payee_reference']},['TEST-PAYMENT'])
        result={'status':'confirmed','reference':'TEST-SETTLED','settled':True,'amount':i['total'],'currency':'USD','invoice_id':i['id'],'payee_reference':i['approval']['payee_reference'],'date':'2027-01-12'}
        with patch('adapters.submit',return_value=result):app.process_job()
        self.assertEqual(self.data()['status'],'COMPLETED');self.assertEqual(len(self.data()['payments']),1);self.assertEqual(self.data()['payments'][0]['status'],'Confirmed external settlement')

    def test_signed_event_replay_and_cross_case_document(self):
        self.approved();self.enable_operational_fixture();body={'event_id':'TEST-EVENT','workflow':self.ident,'operation':'receipt','confirmed':True,'fields':{'quantity':'100','reference':'TEST-EVENT-DELIVERY','date':'2027-01-10'}};digest=engine.fingerprint(body)
        first=automation_api.ingest_event('warehouse',body,digest);second=automation_api.ingest_event('warehouse',body,digest);self.assertEqual(first,second);self.assertEqual(len(self.data()['receipts']),1)
        with self.assertRaises(d.RuleError):automation_api.ingest_event('warehouse',body,'CHANGED')
        body.update(event_id='TEST-EVENT-2');body['fields']['document_id']='DOC-OTHER-CASE'
        with self.assertRaises(d.RuleError):automation_api.ingest_event('warehouse',body,engine.fingerprint(body))

    def test_pending_payment_blocks_duplicate_recording_and_unknown_requires_job(self):
        self.approved();self.receive();self.invoice();self.act('finance_decision',{'decision':'approve'},'finance');self.enable_operational_fixture();data=self.data();i=data['invoices'][-1]
        with db.transaction() as c:job=engine.queue_gateway(c,data,'payment',{'operation':'payment','invoice_id':i['id'],'amount':i['total'],'currency':'USD','payee_reference':i['approval']['payee_reference']},['TEST-PENDING'])
        auth=self.client.post('/api/login',json={'email':'finance@test.local','password':'isolated-test-password-2026'}).json();payload={'version':data['version'],'payload':{'amount':'7650','reference':'TEST-BANK','date':'2027-01-12','document_id':self.proof(),'reason':'TEST ONLY bank reconciliation'}};headers={'X-CSRF-Token':auth['csrf'],'Idempotency-Key':'PENDING-TEST'};url='/api/cases/'+self.ident+'/actions/payment_record';self.assertEqual(self.client.post(url,json=payload,headers=headers).status_code,409)
        with db.transaction() as c:c.execute("UPDATE jobs SET status='UNKNOWN' WHERE id=?",(job,))
        self.assertEqual(self.client.post(url,json=payload,headers=headers).status_code,409);payload['payload']['job_id']=job;response=self.client.post(url,json=payload,headers=headers);self.assertEqual(response.status_code,200,response.text)
        with db.connect() as c:self.assertEqual(c.execute('SELECT status FROM jobs WHERE id=?',(job,)).fetchone()[0],'RECONCILED')

class ProviderTests(unittest.TestCase):
    def test_provider_rejects_unoffered_tools(self):
        class Response:
            status_code=200
            def json(self):return {'choices':[{'message':{'role':'assistant','tool_calls':[{'id':'X','function':{'name':'finance_decision','arguments':'{}'}}]}}]}
        with patch('groq_provider.config',return_value=('TEST-KEY','TEST-MODEL')),patch('groq_provider.completion',return_value=Response()):
            with self.assertRaises(d.RuleError) as error:groq.assess(6,{'id':'TEST','quotes':[]},{'documents':[]},{'TEST'},lambda:{})
            self.assertEqual(error.exception.status,403)
    def test_provider_accepts_only_calculated_source_grounded_assessment(self):
        class Response:
            status_code=200
            def __init__(self,name,args):self.name,self.args=name,args
            def json(self):return {'usage':{'prompt_tokens':20,'completion_tokens':10},'choices':[{'message':{'role':'assistant','tool_calls':[{'id':'X','function':{'name':self.name,'arguments':json.dumps(self.args)}}]}}]}
        assessment={'recommendation':'TEST proceed','reasons':['TEST verified calculator result'],'alternatives':[],'uncertainties':['TEST data'],'source_ids':['TEST'],'next_action':'Human review'}
        with patch('groq_provider.config',return_value=('TEST-KEY','TEST-MODEL')),patch('groq_provider.completion',side_effect=[Response('record_assessment',assessment)]):
            result=groq.assess(6,{'id':'TEST','quotes':[]},{'documents':[]},{'TEST'},lambda:{'solver_status':'Optimal','allocations':[]})
            self.assertEqual(result['input_tokens'],20);self.assertEqual([x['name'] for x in result['tools']],['calculate','record_assessment'])

class POCConsoleTests(AutomationTests):
    def login_buyer(self):
        return self.client.post('/api/login',json={'email':'buyer@test.local','password':'isolated-test-password-2026'}).json()['csrf']
    def console(self,action,payload=None,poc=True):
        headers={'X-CSRF-Token':self.csrf,'Idempotency-Key':d.uid('TEST-')}
        if poc:headers['X-POC-Console']='true'
        return self.client.post('/api/cases/'+self.ident+'/actions/'+action,json={'version':self.data()['version'],'payload':payload or {}},headers=headers)
    def test_single_login_full_cycle_and_attribution(self):
        self.csrf=self.login_buyer();self.quotes();self.switch('ai');self.drain()
        for action in ['supplier_decision','po_decision']:
            response=self.console(action,{'decision':'approve','reason':'POC operator review'});self.assertEqual(response.status_code,200,response.text);self.drain()
        response=self.console('receipt',{'quantity':'300','reference':'POC-RECEIPT','date':'2027-01-10'});self.assertEqual(response.status_code,200,response.text);self.drain()
        receipt=self.data()['receipts'][-1]
        response=self.console('quality',{'receipt_id':receipt['id'],'accepted':'300','reason':'POC test inspection'});self.assertEqual(response.status_code,200,response.text);self.drain()
        self.invoice();response=self.console('finance_decision',{'decision':'approve','reason':'POC finance review'});self.assertEqual(response.status_code,200,response.text);self.drain()
        self.assertEqual(self.console('test_payment',{'reason':'POC test only'}).status_code,200)
        data=self.data();self.assertEqual(data['status'],'COMPLETED');self.assertEqual(data['payments'][-1]['status'],'Simulated test payment; no money transferred')
        self.assertTrue(all(x['performed_by']=='TEST-buyer' for x in data['poc_reviews']))
        with db.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],0);self.assertTrue(db.verify_audit(c))
    def test_poc_cannot_bypass_manual_operational_or_other_owner(self):
        self.csrf=self.login_buyer();self.quotes();self.act('rank');self.act('supplier_submit')
        self.assertEqual(self.console('supplier_decision',{'decision':'approve'}).status_code,403)
        self.assertEqual(self.console('supplier_decision',{'decision':'approve'},poc=False).status_code,403)
        self.switch('ai')
        with db.transaction() as c:c.execute("UPDATE case_controls SET record_kind='operational' WHERE case_id=?",(self.ident,))
        self.assertEqual(self.console('supplier_decision',{'decision':'approve'}).status_code,403)
        with db.transaction() as c:
            c.execute("UPDATE case_controls SET record_kind='test' WHERE case_id=?",(self.ident,));data=engine.load(c,self.ident);data['creator']='OTHER';data['version']+=1;db.save(c,data)
        self.assertEqual(self.console('supplier_decision',{'decision':'approve'}).status_code,403)
    def test_poc_gate_version_replay_and_permanent_test_classification(self):
        self.csrf=self.login_buyer();self.switch('ai')
        self.assertEqual(self.console('test_payment',{'reason':'Cannot skip finance'}).status_code,409)
        self.quotes();self.drain();headers={'X-CSRF-Token':self.csrf,'X-POC-Console':'true','Idempotency-Key':'POC-REPLAY'};body={'version':self.data()['version'],'payload':{'decision':'approve','reason':'POC review'}};url='/api/cases/'+self.ident+'/actions/supplier_decision'
        first=self.client.post(url,json=body,headers=headers);self.assertEqual(first.status_code,200,first.text);self.assertEqual(self.client.post(url,json=body,headers=headers).json(),first.json())
        headers['Idempotency-Key']='STALE';self.assertEqual(self.client.post(url,json=body,headers=headers).status_code,409)
        self.csrf=self.client.post('/api/login',json={'email':'admin@test.local','password':'isolated-test-password-2026'}).json()['csrf']
        self.assertEqual(self.console('classify_record',{'record_kind':'operational','reason':'Must not promote'},poc=False).status_code,409)

if __name__=='__main__':unittest.main()
