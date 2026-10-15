"""Opt-in live Groq integration check using only a disposable TEST database.
Human gates are exercised by role fixtures; this is not actual human review.
No live emails, SAP orders or payments are sent.
"""
import sys,tempfile,json,hashlib,logging,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app,storage as db,domain as d,ai_domain as business,automation as engine,groq_provider as groq
logging.getLogger('httpx').setLevel(logging.WARNING);logging.getLogger('procureai').setLevel(logging.WARNING)

def run(target):
    with tempfile.TemporaryDirectory(prefix='procure-live-test-') as temp:
        db.DATA=Path(temp);db.DB=db.DATA/'isolated.db';db.migrate();roles={r:{'id':'TEST-'+r,'role':r} for r in ['buyer','procurement','warehouse','quality','finance']}
        data=d.create_case({'title':'TEST ONLY — LIVE AI INTEGRATION CHECK','material':'TEST-LOCK','quantity':'300','deadline':'2027-01-15'},roles['buyer']);data.update(mode='ai',mode_epoch=0,record_kind='test',supplier_contacts=[{'name':x,'email':x.lower()+'@example.invalid'} for x in ['Aster','Brookfield','Cedar']])
        with db.transaction() as c:
            db.save(c,data)
            for supplier,price in [('Brookfield','26.00'),('Cedar','29.00')]:
                data=business.action(data,'quote',{'supplier':supplier,'quantity':'300','unit_price':price,'quality':'95','reliability':'95','risk':'10','delivery_date':'2027-01-10','payment_terms':'Net 30'},roles['buyer']);data['version']+=1;db.save(c,data)
            source='TEST ONLY supplier quotation. Supplier: Aster\nQuantity: 300\nUnit price: 25.50\nFreight: 0\nDiscount: 0\nTax rate: 0\nMinimum order: 0\nCurrency: USD\nDelivery date: 2027-01-10\nQuality rating: 95\nReliability rating: 95\nRisk rating: 10\nPayment terms: Net 30\nAll values are isolated test inputs, not real supplier assertions.'
            doc=d.uid('DOC-');body=source.encode();(db.DATA/'documents'/doc).write_bytes(body);c.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?)',(doc,data['id'],'TEST-buyer','test-quotation.txt','text/plain',len(body),hashlib.sha256(body).hexdigest(),d.now()));c.execute('INSERT INTO document_intake(document_id,kind,source) VALUES(?,?,?)',(doc,'quotation',source));engine.schedule(c,data)
        ident=data['id'];checkpoints=[]
        def read():
            with db.connect() as c:return engine.load(c,ident)
        def act(action,p,role):
            with db.transaction() as c:
                result=business.action(engine.load(c,ident),action,p,roles[role]);result['version']+=1;db.save(c,result);db.audit(c,ident,roles[role]['id'],action,{'test_fixture':True});engine.schedule(c,result)
        def drain():
            for _ in range(150):
                if not engine.process_one():
                    with db.connect() as c:pending=c.execute("SELECT MIN(available) FROM agent_tasks WHERE status IN ('QUEUED','RETRY')").fetchone()[0]
                    if pending is None:return
                    time.sleep(min(2,max(.1,pending-time.time())))
            raise RuntimeError('Live check exceeded task budget')
        drain();data=read();checkpoints.append(data['status']);print('TEST checkpoint:',data['status'],flush=True)
        if data['status']!='SUPPLIER_REVIEW':
            with db.connect() as c:errors=[{'agent':r['agent'],'error':r['error']} for r in c.execute("SELECT agent,error FROM agent_tasks WHERE status='BLOCKED'")]
            raise RuntimeError('Supplier gate not reached: '+json.dumps(errors))
        act('supplier_decision',{'decision':'approve','reason':'TEST ROLE FIXTURE: independently reviewed test allocation'},'procurement');drain();checkpoints.append(read()['status']);print('TEST checkpoint:',read()['status'],flush=True)
        if read()['status']!='PO_REVIEW':raise RuntimeError('PO human gate not reached')
        act('po_decision',{'decision':'approve','reason':'TEST ROLE FIXTURE: approve test PO'},'procurement');drain()
        act('receipt',{'quantity':'300','reference':'TEST-DELIVERY','date':'2027-01-10'},'warehouse');drain();receipt=read()['receipts'][-1]
        act('quality',{'receipt_id':receipt['id'],'accepted':'300','reason':'TEST ROLE FIXTURE: simulated physical evidence; no real goods'},'quality');drain()
        act('invoice',{'reference':'TEST-INVOICE','quantity':'300','unit_price':'25.50','total':'7650','currency':'USD','date':'2027-01-11','due_date':'2027-02-10'},'buyer');drain();checkpoints.append(read()['status']);print('TEST checkpoint:',read()['status'],flush=True)
        if read()['status']!='FINANCE_REVIEW':raise RuntimeError('Finance human gate not reached')
        act('finance_decision',{'decision':'approve','reason':'TEST ROLE FIXTURE: approve test payment proposal'},'finance');drain();checkpoints.append(read()['status'])
        proof=d.uid('DOC-');body=b'TEST ONLY: no money transferred. Reference TEST-PAYMENT.';(db.DATA/'documents'/proof).write_bytes(body)
        with db.transaction() as c:c.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?)',(proof,ident,'TEST-finance','test-payment.txt','text/plain',len(body),hashlib.sha256(body).hexdigest(),d.now()))
        act('payment_record',{'amount':'7650','reference':'TEST-PAYMENT','date':'2027-01-12','document_id':proof,'reason':'TEST ROLE FIXTURE: no real payment'},'finance');drain();data=read()
        with db.connect() as c:
            rows=[dict(x) for x in c.execute('SELECT agent,model,input_tokens,output_tokens,elapsed_ms FROM agent_runs')];exceptions=[{'agent':x['agent'],'error':x['error']} for x in c.execute("SELECT agent,error FROM agent_tasks WHERE status='BLOCKED'")];audit=db.verify_audit(c);jobs=c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
        report={'scope':'Disposable TEST database, real Groq API and real HiGHS; role fixtures exercise human gates. No actual human review or external procurement execution.','connection':groq.connection_test(),'checkpoints':checkpoints,'final_status':data['status'],'summary':d.summary(data),'agents_exercised':sorted({x['agent'] for x in rows}),'assessments':rows,'expected_disconnected_exceptions':exceptions,'audit_integrity':audit,'external_jobs_created':jobs}
        Path(target).write_text(json.dumps(report,indent=2));print(json.dumps({k:report[k] for k in ['checkpoints','final_status','agents_exercised','audit_integrity','external_jobs_created']}))
if __name__=='__main__':
    if len(sys.argv)!=2:raise SystemExit('Usage: python ops/live_ai_check.py report.json (uses Groq tokens)')
    run(sys.argv[1])
