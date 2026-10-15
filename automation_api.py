"""Authenticated automation workspaces and signed external event ingestion."""
import json, os, hmac, hashlib, time
from pathlib import Path
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
import storage as db
import domain as d
import ai_domain as business
import automation as engine
import groq_provider as groq
import document_intake
import poc_console

class IntakeInput(BaseModel):kind:str
class IntakeApply(BaseModel):version:int;document_id:str;fields:dict;reason:str
class ResumeInput(BaseModel):reason:str

def register(application,get_user):
    router=APIRouter()
    @router.get('/api/automation')
    def overview(request:Request):
        with db.connect() as c:
            u=get_user(request,c);result=engine.queues(c,u);row=c.execute('SELECT default_mode FROM preferences WHERE user_id=?',(u['id'],)).fetchone();result['default_mode']=row['default_mode'] if row else 'manual';return result

    @router.get('/api/agent-runs/{ident}')
    def run(ident:str,request:Request):
        with db.connect() as c:
            get_user(request,c);row=c.execute('SELECT * FROM agent_runs WHERE id=?',(ident,)).fetchone()
            if not row:raise HTTPException(404,'Assessment not found')
            result=dict(row)
            for key in ['input','tools','explanation']:result[key]=json.loads(result[key])
            return result

    @router.get('/api/agent-runs')
    def runs(request:Request,agent:int):
        if agent not in groq.AGENTS:d.fail('Unknown agent')
        with db.connect() as c:
            get_user(request,c)
            return [{**dict(row),'explanation':json.loads(row['explanation'])} for row in c.execute('SELECT id,case_id,agent,explanation,model,created FROM agent_runs WHERE agent=? ORDER BY created DESC LIMIT 100',(agent,))]

    @router.post('/api/integrations/groq/test')
    def connection(request:Request):
        with db.connect() as c:u=get_user(request,c);d.auth(u,{'admin'})
        result=groq.connection_test()
        with db.transaction() as c:
            c.execute('INSERT INTO service_health VALUES(?,?,?,?) ON CONFLICT(kind) DO UPDATE SET status=excluded.status,checked=excluded.checked,detail=excluded.detail',('groq',result['status'],result['checked'],db.dumps({'model':result['model']})));db.audit(c,None,u['id'],'groq_connection_checked',result)
        return result

    @router.post('/api/cases/{ident}/automation/resume')
    def resume(ident:str,p:ResumeInput,request:Request):
        with db.transaction() as c:
            u=get_user(request,c);d.auth(u,{'buyer','procurement','admin'});data=engine.load(c,ident)
            if data['mode']!='ai':d.fail('Switch this case to AI mode first',409)
            reason=d.required(p.reason,'Resume reason')
            c.execute("UPDATE agent_tasks SET status='QUEUED',attempts=0,available=?,error=NULL,updated=? WHERE case_id=? AND epoch=? AND status='BLOCKED'",(time.time(),d.now(),ident,data['mode_epoch']));engine.schedule(c,data);db.audit(c,ident,u['id'],'automation_resumed',{'reason':reason})
        return {'queued':True}

    @router.post('/api/documents/{ident}/intake')
    def intake(ident:str,p:IntakeInput,request:Request):
        if p.kind not in {'quotation','invoice','receipt','inspection'}:d.fail('Choose quotation, invoice, receipt or inspection')
        with db.connect() as c:
            u=get_user(request,c);d.auth(u,{'buyer','warehouse','quality'});row=c.execute('SELECT * FROM documents WHERE id=?',(ident,)).fetchone()
            if not row:d.fail('Document not found',404)
            metadata=dict(row);data=engine.load(c,row['case_id'])
            if data['mode']!='ai':d.fail('Document extraction is available in AI mode',409)
        body=(db.DATA/'documents'/ident).read_bytes()
        if hashlib.sha256(body).hexdigest()!=metadata['sha256']:d.fail('Source document integrity check failed',409)
        text=document_intake.extract(body,metadata['mime'])
        with db.transaction() as c:
            get_user(request,c);data=engine.load(c,data['id'])
            if data['mode']!='ai':d.fail('Operating mode changed; retry after review',409)
            existing=c.execute('SELECT kind,status FROM document_intake WHERE document_id=?',(ident,)).fetchone()
            if existing:
                if existing['kind']!=p.kind:d.fail('This file already has a different extraction type',409)
                return {'queued':existing['status']=='QUEUED','document_id':ident,'existing':True,'status':existing['status']}
            c.execute('INSERT INTO document_intake(document_id,kind,source) VALUES(?,?,?)',(ident,p.kind,text));engine.schedule(c,data);db.audit(c,data['id'],u['id'],'intake_requested',{'document_id':ident,'kind':p.kind})
        return {'queued':True,'document_id':ident}

    @router.post('/api/cases/{ident}/intake/apply')
    def apply_intake(ident:str,p:IntakeApply,request:Request):
        with db.transaction() as c:
            u=get_user(request,c);data=engine.load(c,ident)
            if data['version']!=p.version:d.fail('Record changed; refresh before applying',409)
            row=c.execute('SELECT i.* FROM document_intake i JOIN documents doc ON doc.id=i.document_id WHERE i.document_id=? AND doc.case_id=?',(p.document_id,ident)).fetchone()
            if not row or row['status']!='READY':d.fail('No unapplied extraction proposal found',409)
            reason=d.required(p.reason,'Review findings');action={'quotation':'quote','invoice':'invoice','receipt':'receipt','inspection':'quality'}[row['kind']]
            fields={**p.fields,'document_id':p.document_id,'reason':reason}
            # The normal business validator enforces the reviewer role and physical gates.
            poc=request.headers.get('X-POC-Console')=='true';reviewer=poc_console.reviewer(data,action,u,poc)
            data=business.action(data,action,fields,reviewer)
            if poc:poc_console.record(data,action,u)
            data['version']+=1;db.save(c,data);c.execute("UPDATE document_intake SET status='APPLIED' WHERE document_id=?",(p.document_id,));db.audit(c,ident,u['id'],'intake_applied',{'document_id':p.document_id,'fields':p.fields,'reason':reason,'poc_console':poc});engine.schedule(c,data)
        return data

    @router.post('/api/integration-events/{kind}')
    async def event(kind:str,request:Request):
        if kind not in {'supplier','warehouse','quality','payment'}:d.fail('Unsupported integration event',404)
        secret=os.getenv(kind.upper()+'_WEBHOOK_SECRET','')
        if not secret:d.fail('Event integration is not connected',503)
        raw=await request.body();timestamp=request.headers.get('X-Event-Timestamp','');signature=request.headers.get('X-Event-Signature','')
        try:within=abs(time.time()-float(timestamp))<=300
        except ValueError:within=False
        expected=hmac.new(secret.encode(),timestamp.encode()+b'.'+raw,hashlib.sha256).hexdigest()
        if not within or not hmac.compare_digest(signature,expected):d.fail('Invalid event signature',403)
        try:payload=json.loads(raw)
        except ValueError:d.fail('Invalid event JSON')
        return ingest_event(kind,payload,fingerprint=hashlib.sha256(raw).hexdigest())

    application.include_router(router)

def ingest_event(kind,payload,fingerprint):
    ident=d.required(payload.get('event_id'),'Event ID');case_id=d.required(payload.get('workflow'),'Workflow ID')
    with db.transaction() as c:
        previous=c.execute('SELECT * FROM integration_events WHERE id=?',(ident,)).fetchone()
        if previous:
            if previous['digest']!=fingerprint:d.fail('Event ID reused with a different payload',409)
            return json.loads(previous['result'])
        data=engine.load(c,case_id);engine.external_guard(data)
        if data['mode']!='ai' and kind!='payment':d.fail('Live event processing requires AI mode; use accountable manual entry',409)
        actor={'id':'gateway:'+kind,'role':{'supplier':'buyer','warehouse':'warehouse','quality':'quality','payment':'finance'}[kind]}
        fields=payload.get('fields',{})
        if not isinstance(fields,dict):d.fail('Event fields must be an object')
        action={'supplier':{'quotation':'quote','invoice':'invoice'},'warehouse':{'receipt':'receipt'},'quality':{'inspection':'quality'},'payment':{'settlement':'payment_record'}}[kind].get(payload.get('operation'))
        if not action:d.fail('Operation is outside the integration scope',403)
        if kind in {'warehouse','quality'} and payload.get('confirmed') is not True:d.fail('Accountable source confirmation required')
        if kind=='payment':
            if payload.get('confirmed') is not True or payload.get('settled') is not True:d.fail('Confirmed settlement required')
            invoice=data['invoices'][-1]
            if payload.get('invoice_id')!=invoice['id'] or fields.get('currency')!=data['currency']:d.fail('Settlement must match the approved invoice and currency')
            job=c.execute("SELECT * FROM jobs WHERE case_id=? AND kind='payment' AND id=?",(case_id,payload.get('job_id'))).fetchone()
            if not job:d.fail('Settlement must reference a recorded payment request')
            submitted=json.loads(job['payload'])
            if submitted['invoice_id']!=invoice['id'] or submitted['amount']!=d.money(fields.get('amount')) or submitted['payee_reference']!=fields.get('payee_reference'):d.fail('Settlement differs from the approved payment request')
            doc=d.uid('DOC-');body=db.dumps(payload).encode();path=db.DATA/'documents'/doc;path.write_bytes(body)
            c.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?)',(doc,case_id,actor['id'],'settlement-'+ident+'.txt','text/plain',len(body),hashlib.sha256(body).hexdigest(),d.now()));fields={**fields,'document_id':doc,'reason':'Verified signed payment gateway settlement '+ident}
        if fields.get('document_id') and not c.execute('SELECT 1 FROM documents WHERE id=? AND case_id=?',(fields['document_id'],case_id)).fetchone():d.fail('Event document must belong to the case')
        data=business.action(data,action,fields,actor)
        if kind=='payment':
            data['payments'][-1]['status']='Confirmed external settlement';c.execute("UPDATE jobs SET status='CONFIRMED',result=? WHERE id=?",(db.dumps({'reference':fields['reference'],'settled':True}),payload['job_id']))
        data['version']+=1;db.save(c,data);db.audit(c,case_id,actor['id'],'integration_event_applied',{'id':ident,'operation':action,'source_confirmation':payload.get('confirmed',False)},'external system');result={'applied':True,'workflow':case_id,'version':data['version'],'source_event':payload};c.execute('INSERT INTO integration_events VALUES(?,?,?,?,?,?)',(ident,kind,case_id,fingerprint,db.dumps(result),d.now()));engine.schedule(c,data)
    return result
