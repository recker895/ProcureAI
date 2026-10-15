"""Provider responses and fixtures only; never spend live tokens in unit tests."""
import json,time,unittest,hashlib
from unittest.mock import patch
import test_automation as fixtures
import groq_limits as limits,groq_provider as groq,automation as engine,storage as db,domain as d

class Response:
    def __init__(self,status=200,body=None,headers=None):self.status_code=status;self.body=body or {};self.headers=headers or {}
    def json(self):return self.body

def assessment_response():
    fields={'recommendation':'TEST verified recommendation','reasons':['TEST calculator evidence'],'source_ids':['TEST'],'next_action':'Human review'}
    return Response(body={'usage':{'prompt_tokens':10,'completion_tokens':10},'choices':[{'message':{'role':'assistant','tool_calls':[{'id':'TEST-CALL','function':{'name':'record_assessment','arguments':json.dumps(fields)}}]}}]})

class RateLimitTests(unittest.TestCase):
    def setUp(self):limits.buckets.clear()
    def test_exact_daily_limit_and_long_retry_are_preserved(self):
        r=Response(429,{'error':{'message':'Rate limit reached for model in organization org_SECRET on tokens per day (TPD): Limit 200000, Used 199950, Requested 2500. Please try again in 2h3m4s.'}})
        e=limits.diagnose(r,'MODEL');self.assertEqual(e.details['limit_type'],'TPD');self.assertEqual(e.retry_after,7384);self.assertEqual(e.details['limit'],200000);self.assertNotIn('org_SECRET',e.message)
    def test_minute_headers_and_unknown_type_do_not_invent_limits(self):
        e=limits.diagnose(Response(429,{'error':{'message':'tokens per minute (TPM): Limit 8000, Used 7000, Requested 2000'}},{'retry-after':'27'}),'MODEL');self.assertEqual(e.retry_after,27);self.assertEqual(e.details['requested'],2000)
        e=limits.diagnose(Response(429,headers={'x-ratelimit-reset-tokens':'1m2.5s'}),'MODEL');self.assertEqual(e.retry_after,62.5);self.assertEqual(e.details['limit_type'],'UNKNOWN')
    def test_model_cooldown_prevents_repeated_calls_but_other_model_can_run(self):
        class Client:
            calls=0
            def post(self,*args,**kwargs):
                self.calls+=1
                return Response(429,{'error':{'message':'tokens per day (TPD): Limit 200000'}},{'retry-after':'3600'}) if kwargs['json']['model']=='A' else Response()
        client=Client();payload={'model':'A','messages':[],'max_completion_tokens':10}
        for _ in range(2):
            with self.assertRaises(limits.ProviderBusy):limits.completion(client,'TEST',payload)
        self.assertEqual(client.calls,1);self.assertEqual(limits.completion(client,'TEST',{**payload,'model':'B'}).status_code,200);self.assertEqual(client.calls,2)
    def test_fallback_retains_verified_calculator_and_actual_model(self):
        with patch.dict('os.environ',{'GROQ_FALLBACK_MODEL':'SECOND'}),patch('groq_provider.config',return_value=('TEST','FIRST')),patch('groq_provider.completion',side_effect=[limits.ProviderBusy('FIRST TPM reached',20,{'model':'FIRST','limit_type':'TPM'}),assessment_response()]) as request:
            result=groq.assess(6,{'id':'TEST','quotes':[]},{'documents':[]},{'TEST'},lambda:{'solver':'HiGHS','total':'100.00'})
            self.assertEqual(result['model'],'SECOND');self.assertEqual(request.call_count,2);self.assertEqual(result['tools'][0]['name'],'calculate');self.assertEqual(result['tools'][-1]['name'],'model_routing')
    def test_all_models_limited_returns_real_earliest_retry(self):
        with patch.dict('os.environ',{'GROQ_FALLBACK_MODEL':'SECOND'}),patch('groq_provider.config',return_value=('TEST','FIRST')),patch('groq_provider.completion',side_effect=[limits.ProviderBusy('FIRST TPD reached',3600,{'model':'FIRST','limit_type':'TPD'}),limits.ProviderBusy('SECOND TPM reached',35,{'model':'SECOND','limit_type':'TPM'})]):
            with self.assertRaises(limits.ProviderBusy) as caught:groq.assess(6,{'id':'TEST','quotes':[]},{'documents':[]},{'TEST'},lambda:{})
            self.assertEqual(caught.exception.retry_after,35);self.assertEqual(len(caught.exception.details['routing_failures']),2)
    def test_extraction_checkpoint_resumes_explanation_without_reextracting(self):
        extraction=Response(body={'usage':{'prompt_tokens':10,'completion_tokens':10},'choices':[{'message':{'role':'assistant','tool_calls':[{'id':'EXTRACT','function':{'name':'extract_document','arguments':json.dumps({'fields':{'supplier':'TEST'},'sources':{'supplier':'TEST'},'missing':[]})}}]}}]})
        intake={'document_id':'DOC','kind':'quotation','source':'TEST'}
        evidence={'documents':[],'intake':intake}
        with patch.dict('os.environ',{'GROQ_INTAKE_MODEL':'FIRST','GROQ_FALLBACK_MODEL':'SECOND'}),patch('groq_provider.config',return_value=('TEST','FIRST')),patch('groq_provider.completion',side_effect=[extraction,limits.ProviderBusy('FIRST limited',20),limits.ProviderBusy('SECOND limited',30)]):
            with self.assertRaises(limits.ProviderBusy) as caught:groq.assess(3,{'id':'TEST','quotes':[]},evidence,{'TEST'},lambda:intake,True)
        with patch.dict('os.environ',{'GROQ_INTAKE_MODEL':'FIRST','GROQ_FALLBACK_MODEL':'SECOND'}),patch('groq_provider.config',return_value=('TEST','FIRST')),patch('groq_provider.completion',return_value=assessment_response()) as request:
            result=groq.assess(3,{'id':'TEST','quotes':[]},evidence,{'TEST'},lambda:intake,True,checkpoint=caught.exception.checkpoint)
            self.assertEqual(request.call_count,1);self.assertEqual(result['input_tokens'],20);self.assertEqual([x['name'] for x in result['tools']],['calculate','extract_document','record_assessment'])
    def test_empty_extraction_cannot_hide_visible_test_fields(self):
        def extracted(fields):return Response(body={'choices':[{'message':{'role':'assistant','tool_calls':[{'id':'EXTRACT','function':{'name':'extract_document','arguments':json.dumps({'fields':fields,'sources':{k:'Supplier: TEST' for k in fields},'missing':[]})}}]}}]})
        intake={'document_id':'DOC','kind':'quotation','source':'Supplier: TEST\nAvailable quantity: 300\nUnit price: 26.00\nCurrency: USD'}
        with patch.dict('os.environ',{'GROQ_INTAKE_MODEL':'FIRST'}),patch('groq_provider.config',return_value=('TEST','FIRST')),patch('groq_provider.completion',side_effect=[extracted({}),extracted({'supplier':'TEST'}),assessment_response()]) as request:
            result=groq.assess(3,{'id':'TEST','record_kind':'test','quotes':[]},{'documents':[],'intake':intake},{'TEST'},lambda:intake,True)
            self.assertEqual(request.call_count,3);self.assertEqual(result['document']['fields']['supplier'],'TEST');self.assertFalse(result['tools'][1]['result']['accepted'])

class IntakeRecoveryTests(unittest.TestCase):
    setUpClass=classmethod(fixtures.AutomationTests.setUpClass.__func__)
    setUp=fixtures.AutomationTests.setUp
    tearDown=fixtures.AutomationTests.tearDown
    data=fixtures.AutomationTests.data
    switch=fixtures.AutomationTests.switch
    act=fixtures.AutomationTests.act
    quotes=fixtures.AutomationTests.quotes
    def login(self):return self.client.post('/api/login',json={'email':'buyer@test.local','password':'isolated-test-password-2026'}).json()['csrf']
    def test_identical_upload_and_intake_are_idempotent(self):
        csrf=self.login();self.switch('ai');url='/api/cases/'+self.ident+'/documents';headers={'X-CSRF-Token':csrf}
        first=self.client.post(url,files={'file':('first.txt',b'TEST quotation source','text/plain')},headers=headers).json()
        second=self.client.post(url,files={'file':('renamed.txt',b'TEST quotation source','text/plain')},headers=headers).json();self.assertEqual(first['id'],second['id']);self.assertTrue(second['duplicate'])
        url='/api/documents/'+first['id']+'/intake'
        for _ in range(2):self.assertEqual(self.client.post(url,json={'kind':'quotation'},headers=headers).status_code,200)
        with db.connect() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM documents').fetchone()[0],1);self.assertEqual(c.execute('SELECT COUNT(*) FROM document_intake').fetchone()[0],1)
    def test_preexisting_duplicate_preserves_source_and_skips_extra_task(self):
        self.switch('ai')
        with db.transaction() as c:
            for n in range(2):
                ident='DOC-'+str(n);body=b'TEST same source';(db.DATA/'documents'/ident).write_bytes(body)
                c.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?)',(ident,self.ident,'TEST-buyer',ident+'.txt','text/plain',len(body),hashlib.sha256(body).hexdigest(),d.now()))
                c.execute('INSERT INTO document_intake(document_id,kind,source) VALUES(?,?,?)',(ident,'quotation','TEST same source'))
            engine.schedule(c,self.data())
            self.assertEqual(c.execute("SELECT COUNT(*) FROM document_intake WHERE status='DUPLICATE'").fetchone()[0],1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM documents').fetchone()[0],2)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM agent_tasks WHERE agent=3 AND status='QUEUED'").fetchone()[0],1)
    def test_provider_capacity_wait_does_not_exhaust_validation_attempts(self):
        self.switch('ai')
        with patch('groq_provider.assess',side_effect=limits.ProviderBusy('TEST TPD reached',4000,{'model':'MODEL','limit_type':'TPD'})):self.assertTrue(engine.process_one())
        with db.connect() as c:
            row=c.execute("SELECT * FROM agent_tasks WHERE status='RETRY'").fetchone();self.assertEqual(row['attempts'],0);self.assertGreater(row['available'],time.time()+3900);self.assertEqual(json.loads(row['result'])['provider_limit']['limit_type'],'TPD')

if __name__=='__main__':unittest.main()

class CitationRecoveryTests(unittest.TestCase):
    def test_invalid_citation_is_rejected_then_corrected(self):
        bad=assessment_response();call=bad.body['choices'][0]['message']['tool_calls'][0];args=json.loads(call['function']['arguments']);args['source_ids']=['INVENTED'];call['function']['arguments']=json.dumps(args)
        with patch('groq_provider.config',return_value=('TEST','FIRST')),patch('groq_provider.completion',side_effect=[bad,assessment_response()]) as request:
            result=groq.assess(6,{'id':'TEST','quotes':[]},{'documents':[]},{'TEST'},lambda:{'total':'100'})
        self.assertEqual(request.call_count,2)
        self.assertEqual(result['assessment']['source_ids'],['TEST'])
        self.assertFalse(result['tools'][1]['result']['accepted'])
    def test_repeated_invalid_citations_remain_blocked(self):
        bad=assessment_response();call=bad.body['choices'][0]['message']['tool_calls'][0];args=json.loads(call['function']['arguments']);args['source_ids']=['INVENTED'];call['function']['arguments']=json.dumps(args)
        with patch('groq_provider.config',return_value=('TEST','FIRST')),patch('groq_provider.completion',return_value=bad) as request:
            with self.assertRaises(d.RuleError):groq.assess(6,{'id':'TEST','quotes':[]},{'documents':[]},{'TEST'},lambda:{})
        self.assertEqual(request.call_count,5)

class NullExtractionTests(unittest.TestCase):
    def test_all_null_fields_are_not_successful_extraction(self):
        def extraction(fields):return Response(body={'choices':[{'message':{'role':'assistant','tool_calls':[{'id':'EXTRACT','function':{'name':'extract_document','arguments':json.dumps({'fields':fields,'sources':{},'missing':[]})}}]}}]})
        intake={'document_id':'DOC','kind':'quotation','source':'Supplier: TEST\nAvailable quantity: 300\nUnit price: 26.00\nCurrency: USD'}
        with patch('groq_provider.config',return_value=('TEST','FIRST')),patch('groq_provider.completion',side_effect=[extraction({'supplier':None,'quantity':None}),extraction({'supplier':'TEST'}),assessment_response()]) as request:
            result=groq.assess(3,{'id':'TEST','quotes':[]},{'documents':[],'intake':intake},{'TEST'},lambda:intake,True)
        self.assertFalse(result['tools'][1]['result']['accepted'])
        self.assertEqual(result['document']['fields'],{'supplier':'TEST'})
        self.assertEqual(request.call_count,3)
