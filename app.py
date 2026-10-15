import os,json,secrets,time,hashlib,hmac,logging,asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime,timezone
from decimal import Decimal
from fastapi import FastAPI,Request,HTTPException,UploadFile,File
from fastapi.responses import JSONResponse,FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel,Field,ConfigDict
import storage as db
import domain as rules
import adapters
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent/'.env',override=False)
import ai_domain,automation,groq_provider,document_intake
import automation_api,poc_console

log=logging.getLogger('procureai');logging.basicConfig(level=logging.INFO)
PRODUCTION=os.environ.get('PROCURE_ENV')=='production'
if PRODUCTION and not os.getenv('PUBLIC_ORIGIN','').startswith('https://'):
    raise RuntimeError('Production requires an HTTPS PUBLIC_ORIGIN')
MAX_UPLOAD=10*1024*1024
def password_hash(value):
    salt=secrets.token_hex(16);digest=hashlib.scrypt(value.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex();return salt+':'+digest
def check_password(value,stored):
    salt,digest=stored.split(':');return hmac.compare_digest(hashlib.scrypt(value.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex(),digest)
def safe_user(u):return {k:u[k] for k in ['id','email','name','role','theme','active']}
def user(request,c):
    token=request.cookies.get('procure_session','');hashed=hashlib.sha256(token.encode()).hexdigest()
    row=c.execute('SELECT u.*,s.csrf,s.expires FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires>? AND u.active=1',(hashed,time.time())).fetchone()
    if not row:raise HTTPException(401,'Please sign in')
    if request.method not in {'GET','HEAD'} and not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),row['csrf']):raise HTTPException(403,'CSRF token missing or invalid')
    return dict(row)
def get_case(c,case_id):
    return automation.load(c,case_id)
def check_version(data,version):
    if version!=data['version']:raise HTTPException(409,'Record changed; refresh before continuing')
def validate_document(c,case_id,document_id):
    if document_id is not None and not isinstance(document_id,str):raise HTTPException(422,'Document ID must be text')
    if document_id and not c.execute('SELECT id FROM documents WHERE id=? AND case_id=?',(document_id,case_id)).fetchone():raise HTTPException(422,'Document must belong to this workflow')

async def worker():
    while True:
        try:
            await asyncio.to_thread(process_job)
            if os.getenv('AI_WORKER_ENABLED','true')=='true':await asyncio.to_thread(automation.tick)
        except Exception:log.exception('Background job failed')
        await asyncio.sleep(3)
def process_job():
    with db.transaction() as c:
        job=c.execute("SELECT * FROM jobs WHERE status='QUEUED' ORDER BY created LIMIT 1").fetchone()
        if not job:return
        job=dict(job);payload=json.loads(job['payload']);data=get_case(c,job['case_id'])
        try:
            automation.external_guard(data)
            if payload.get('_origin')=='ai' and (data['mode']!='ai' or payload['_epoch']!=data['mode_epoch']):
                c.execute("UPDATE jobs SET status='CANCELLED' WHERE id=?",(job['id'],));return
            if job['attempts']>=3:raise rules.RuleError('External attempt limit reached; reconcile with the service',409)
            if job['kind']=='sap':
                approval=data.get('po',{}).get('approval')
                if not approval or approval['version']!=data['commercial_version']:raise rules.RuleError('Current PO approval required',409)
                if payload['po']['id']!=data['po']['id']:raise rules.RuleError('Purchase order changed',409)
            if job['kind']=='payment':
                invoice=data['invoices'][-1];approval=invoice.get('approval',{})
                if data['status']!='PAYMENT_PENDING' or approval.get('version')!=data['commercial_version'] or invoice['id']!=payload['invoice_id']:raise rules.RuleError('Current finance approval required',409)
                remaining=rules.dec(invoice['total'])-sum((rules.dec(p['amount']) for p in data['payments'] if p['invoice']==invoice['id']),Decimal(0))
                if not payload.get('payee_reference') or rules.money(remaining)!=payload['amount'] or approval.get('payee_reference')!=payload['payee_reference']:raise rules.RuleError('Approved payment details changed or beneficiary reference is missing',409)
        except rules.RuleError as e:
            c.execute("UPDATE jobs SET status='BLOCKED',result=? WHERE id=?",(db.dumps({'error':e.message}),job['id']));db.audit(c,data['id'],'gateway','external_action_blocked',{'job':job['id'],'reason':e.message},'system');return
        c.execute("UPDATE jobs SET status='PROCESSING',attempts=attempts+1 WHERE id=?",(job['id'],))
    result=None;error=None
    try:result=adapters.submit(job['kind'],payload,job['id'])
    except Exception as e:error=type(e).__name__ # Do not leak tokens or remote bodies into logs/UI.
    with db.transaction() as c:
        if result:
            data=get_case(c,job['case_id'])
            if job['kind']=='payment':
                try:settlement_date=rules.iso(result.get('date'))
                except rules.RuleError:settlement_date=None
                valid=settlement_date is not None and result.get('settled') is True and result.get('invoice_id')==payload['invoice_id'] and result.get('currency')==payload['currency'] and str(result.get('amount'))==payload['amount'] and result.get('payee_reference')==payload['payee_reference']
                if not valid:
                    c.execute("UPDATE jobs SET status='UNKNOWN',result=? WHERE id=?",(db.dumps({'reference':result['reference'],'message':'Acknowledged; settlement details unconfirmed'}),job['id']));db.audit(c,data['id'],'gateway','settlement_unconfirmed',{'job':job['id']},'external system');return
                doc=rules.uid('DOC-');body=db.dumps(result).encode();(db.DATA/'documents'/doc).write_bytes(body)
                c.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?)',(doc,data['id'],'gateway:payment','confirmed-settlement.txt','text/plain',len(body),hashlib.sha256(body).hexdigest(),rules.now()))
                data=ai_domain.action(data,'payment_record',{'amount':result['amount'],'reference':result['reference'],'date':settlement_date,'document_id':doc,'reason':'Confirmed settlement from configured payment gateway'}, {'id':'gateway:payment','role':'finance'})
                data['payments'][-1]['status']='Confirmed external settlement'
            elif job['kind']=='sap':
                if len(data['po'].get('orders',[]))>1:
                    references={x.get('id'):x.get('reference') for x in result.get('orders',[]) if isinstance(x,dict)}
                    if any(not isinstance(references.get(x['id']),str) or not references[x['id']].strip() for x in data['po']['orders']):
                        c.execute("UPDATE jobs SET status='UNKNOWN',result=? WHERE id=?",(db.dumps({'reference':result['reference'],'message':'Every supplier order requires its own external acknowledgment'}),job['id']));return
                    for line in data['po']['lines']:line['external_reference']=references[line['order_id']];line['transmission']='Confirmed by external gateway'
                data['po']['transmission']='Confirmed by external gateway';data['po']['external_reference']=result['reference']
            c.execute("UPDATE jobs SET status='CONFIRMED',result=? WHERE id=?",(db.dumps(result),job['id']))
            data['version']+=1;db.save(c,data);automation.schedule(c,data)
            db.audit(c,data['id'],'gateway','payment_settled' if job['kind']=='payment' else 'order_transmitted' if job['kind']=='sap' else 'email_sent',result,'external system')
        else:
            c.execute("UPDATE jobs SET status='UNKNOWN',result=? WHERE id=?",(db.dumps({'error':error,'message':'Outcome unknown. Reconcile; retry uses the same key.'}),job['id']))
            db.audit(c,job['case_id'],'gateway','integration_unconfirmed',{'job':job['id'],'error':error},'external system')
@asynccontextmanager
async def lifespan(app):
    db.migrate()
    with db.transaction() as c:c.execute("UPDATE jobs SET status='UNKNOWN' WHERE status='PROCESSING'")
    if os.getenv('AI_WORKER_ENABLED','true')=='true' and groq_provider.status()['available']:
        try:
            connected=await asyncio.to_thread(groq_provider.connection_test)
            with db.transaction() as c:
                c.execute('INSERT INTO service_health VALUES(?,?,?,?) ON CONFLICT(kind) DO UPDATE SET status=excluded.status,checked=excluded.checked,detail=excluded.detail',('groq',connected['status'],connected['checked'],db.dumps({'model':connected['model']})))
                db.audit(c,None,'configuration','groq_connection_checked',connected,'system')
        except rules.RuleError:
            with db.transaction() as c:c.execute('INSERT INTO service_health VALUES(?,?,?,?) ON CONFLICT(kind) DO UPDATE SET status=excluded.status,checked=excluded.checked',('groq','Connection check failed',rules.now(),'Check server credentials/model/network'))
    task=asyncio.create_task(worker())
    yield
    task.cancel()

app=FastAPI(title='ProcureAI',version='2.0.0',lifespan=lifespan,docs_url=None if PRODUCTION else '/api/docs',openapi_url='/api/openapi.json')
app.add_middleware(TrustedHostMiddleware,allowed_hosts=os.getenv('ALLOWED_HOSTS','127.0.0.1,localhost,testserver').split(','))
@app.exception_handler(rules.RuleError)
async def rule_error(request,e):return JSONResponse({'detail':e.message},status_code=e.status)
@app.middleware('http')
async def safeguards(request,call_next):
    start=time.monotonic()
    if request.method not in {'GET','HEAD','OPTIONS'}:
        origin=request.headers.get('origin')
        expected=os.getenv('PUBLIC_ORIGIN',str(request.base_url).rstrip('/'))
        if origin and origin!=expected:return JSONResponse({'detail':'Origin not allowed'},403)
        try:length=int(request.headers.get('content-length','0'))
        except ValueError:return JSONResponse({'detail':'Invalid request length'},400)
        if length>MAX_UPLOAD+65536:return JSONResponse({'detail':'Request too large'},413)
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff';response.headers['Referrer-Policy']='same-origin'
    response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'"
    response.headers['X-Frame-Options']='DENY'
    if request.url.path.startswith('/api'):response.headers['Cache-Control']='no-store'
    if PRODUCTION:response.headers['Strict-Transport-Security']='max-age=31536000; includeSubDomains'
    log.info('%s %s %s %.0fms',request.method,request.url.path,response.status_code,(time.monotonic()-start)*1000)
    return response
class Credentials(BaseModel):
    email:str=Field(min_length=3,max_length=254)
    password:str=Field(min_length=1,max_length=200)
class Bootstrap(Credentials):name:str=Field(min_length=1,max_length=100);token:str=''
class NewUser(Bootstrap):role:str
class CaseInput(BaseModel):
    title:str;material:str;quantity:str;currency:str='USD';unit:str='EA';deadline:str;min_quality:str='80';certifications:list[str]=[]
    mode:str|None=None;record_kind:str='operational'
class ActionInput(BaseModel):
    version:int=Field(ge=1)
    payload:dict=Field(default_factory=dict)
class Preference(BaseModel):theme:str|None=None;default_mode:str|None=None
class PasswordChange(BaseModel):current_password:str;new_password:str=Field(min_length=12,max_length=200)
class UserAccess(BaseModel):active:bool
@app.get('/api/setup')
def setup():
    with db.connect() as c:return {'required':c.execute('SELECT COUNT(*) FROM users').fetchone()[0]==0,'environment':'production' if PRODUCTION else 'local','bootstrap_token_required':PRODUCTION}
@app.post('/api/setup')
def bootstrap(p:Bootstrap,request:Request):
    if len(p.password)<12:raise HTTPException(422,'Password needs at least 12 characters')
    if PRODUCTION:
        token=os.getenv('BOOTSTRAP_TOKEN','')
        if not token or not hmac.compare_digest(token,p.token):raise HTTPException(403,'Bootstrap token required')
    elif request.client.host not in {'127.0.0.1','::1','testclient'}:raise HTTPException(403,'Initial setup is restricted to localhost')
    with db.transaction() as c:
        if c.execute('SELECT COUNT(*) FROM users').fetchone()[0]:raise HTTPException(409,'Setup already complete')
        ident=rules.uid('U-');c.execute('INSERT INTO users(id,email,name,role,password) VALUES(?,?,?,?,?)',(ident,p.email.lower().strip(),p.name,'admin',password_hash(p.password)));db.audit(c,None,ident,'administrator_created',{'email':p.email})
    return {'created':True}
@app.post('/api/login')
def login(p:Credentials,request:Request):
    key=hashlib.sha256((request.client.host+'|'+p.email.lower()).encode()).hexdigest()
    with db.transaction() as c:
        attempt=c.execute('SELECT * FROM login_attempts WHERE key=?',(key,)).fetchone()
        if attempt and attempt['count']>=8 and time.time()-attempt['last']<900:raise HTTPException(429,'Too many attempts; wait 15 minutes')
        u=c.execute('SELECT * FROM users WHERE email=? AND active=1',(p.email.lower().strip(),)).fetchone()
        valid=check_password(p.password,u['password']) if u else check_password(p.password,password_hash('timing-dummy'))
        if not u or not valid:
            count=(attempt['count'] if attempt and time.time()-attempt['last']<900 else 0)+1
            c.execute('INSERT INTO login_attempts VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET count=excluded.count,last=excluded.last',(key,count,time.time()))
            response=JSONResponse({'detail':'Invalid email or password'},401)
        else:
            c.execute('DELETE FROM login_attempts WHERE key=?',(key,));token=secrets.token_urlsafe(48);csrf=secrets.token_urlsafe(32)
            c.execute('INSERT INTO sessions VALUES(?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),u['id'],csrf,time.time()+8*3600))
            db.audit(c,None,u['id'],'signed_in',{})
            response=JSONResponse({'user':safe_user(u),'csrf':csrf});response.set_cookie('procure_session',token,httponly=True,secure=PRODUCTION,samesite='strict',max_age=8*3600)
    return response
@app.get('/api/me')
def me(request:Request):
    with db.connect() as c:u=user(request,c);return {'user':safe_user(u),'csrf':u['csrf']}
@app.post('/api/logout')
def logout(request:Request):
    with db.transaction() as c:
        u=user(request,c);c.execute('DELETE FROM sessions WHERE token=?',(hashlib.sha256(request.cookies.get('procure_session','').encode()).hexdigest(),));db.audit(c,None,u['id'],'signed_out',{})
    response=JSONResponse({'ok':True});response.delete_cookie('procure_session');return response
@app.get('/api/users')
def users(request:Request):
    with db.connect() as c:
        u=user(request,c);rules.auth(u,{'admin'});return [safe_user(r) for r in c.execute('SELECT * FROM users ORDER BY name')]
@app.post('/api/users')
def add_user(p:NewUser,request:Request):
    if p.role not in rules.ROLES or len(p.password)<12:raise HTTPException(422,'Valid role and password of 12+ characters required')
    with db.transaction() as c:
        u=user(request,c);rules.auth(u,{'admin'});ident=rules.uid('U-')
        if c.execute('SELECT id FROM users WHERE email=?',(p.email.lower().strip(),)).fetchone():raise HTTPException(409,'Email already registered')
        c.execute('INSERT INTO users(id,email,name,role,password) VALUES(?,?,?,?,?)',(ident,p.email.lower().strip(),p.name,p.role,password_hash(p.password)));db.audit(c,None,u['id'],'user_created',{'id':ident,'role':p.role})
    return {'id':ident}
@app.post('/api/preferences')
def preference(p:Preference,request:Request):
    if p.theme is not None and p.theme not in {'light','dark','system'}:raise HTTPException(422,'Unknown theme')
    if p.default_mode is not None and p.default_mode not in {'manual','ai'}:raise HTTPException(422,'Unknown operating mode')
    with db.transaction() as c:
        u=user(request,c)
        if p.theme is not None:c.execute('UPDATE users SET theme=? WHERE id=?',(p.theme,u['id']))
        if p.default_mode is not None:
            c.execute('INSERT INTO preferences VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET default_mode=excluded.default_mode',(u['id'],p.default_mode));db.audit(c,None,u['id'],'default_mode_changed',{'default_mode':p.default_mode,'scope':'new cases only'})
    return p.model_dump(exclude_none=True)
@app.post('/api/password')
def change_password(p:PasswordChange,request:Request):
    with db.transaction() as c:
        u=user(request,c)
        if not check_password(p.current_password,u['password']):raise HTTPException(403,'Current password is incorrect')
        c.execute('UPDATE users SET password=? WHERE id=?',(password_hash(p.new_password),u['id']))
        c.execute('DELETE FROM sessions WHERE user_id=?',(u['id'],));db.audit(c,None,u['id'],'password_changed',{})
    response=JSONResponse({'ok':True,'sign_in_required':True});response.delete_cookie('procure_session');return response
@app.post('/api/users/{user_id}/access')
def user_access(user_id:str,p:UserAccess,request:Request):
    with db.transaction() as c:
        u=user(request,c);rules.auth(u,{'admin'})
        if user_id==u['id']:raise HTTPException(409,'Cannot deactivate your own administrator account')
        target=c.execute('SELECT id FROM users WHERE id=?',(user_id,)).fetchone()
        if not target:raise HTTPException(404,'User not found')
        c.execute('UPDATE users SET active=? WHERE id=?',(int(p.active),user_id))
        if not p.active:c.execute('DELETE FROM sessions WHERE user_id=?',(user_id,))
        db.audit(c,None,u['id'],'user_access_changed',{'user':user_id,'active':p.active})
    return {'ok':True}
@app.get('/api/cases')
def cases(request:Request):
    with db.connect() as c:user(request,c);return [get_case(c,r['id']) for r in c.execute('SELECT id FROM cases ORDER BY updated DESC')]
@app.post('/api/cases')
def new_case(p:CaseInput,request:Request):
    key=request.headers.get('Idempotency-Key','')
    if not key or len(key)>100:raise HTTPException(422,'Idempotency-Key is required')
    digest=hashlib.sha256(db.dumps(['create_case',p.model_dump()]).encode()).hexdigest()
    with db.transaction() as c:
        u=user(request,c);rules.auth(u,{'buyer'})
        existing=c.execute('SELECT * FROM idempotency WHERE user_id=? AND key=?',(u['id'],key)).fetchone()
        if existing:
            if existing['digest']!=digest:raise HTTPException(409,'Idempotency key reused with different request')
            return json.loads(existing['result'])
        preference=c.execute('SELECT default_mode FROM preferences WHERE user_id=?',(u['id'],)).fetchone();mode=p.mode or (preference['default_mode'] if preference else 'manual')
        if mode not in {'manual','ai'} or p.record_kind not in {'test','operational'}:raise HTTPException(422,'Valid operating mode and record kind required')
        data=rules.create_case(p.model_dump(),u);data.update(mode=mode,record_kind=p.record_kind,mode_epoch=0);db.save(c,data);db.audit(c,data['id'],u['id'],'case_created',{'version':1,'status':data['status'],'mode':mode,'record_kind':p.record_kind});automation.schedule(c,data)
        c.execute('INSERT INTO idempotency VALUES(?,?,?,?)',(u['id'],key,digest,db.dumps(data)))
    return data
@app.get('/api/cases/{case_id}')
def case(case_id:str,request:Request):
    with db.connect() as c:
        user(request,c);data=get_case(c,case_id);data['summary']=rules.summary(data);data['documents']=[dict(r) for r in c.execute('SELECT * FROM documents WHERE case_id=?',(case_id,))];data['jobs']=[dict(r) for r in c.execute('SELECT id,kind,status,attempts,result,created FROM jobs WHERE case_id=?',(case_id,))];data['agent_tasks']=[{**dict(r),'name':groq_provider.AGENTS[r['agent']]} for r in c.execute('SELECT id,agent,status,attempts,error,available,input,created,updated,result FROM agent_tasks WHERE case_id=? ORDER BY created DESC',(case_id,))];data['intake']=[dict(r) for r in c.execute('SELECT * FROM document_intake WHERE document_id IN (SELECT id FROM documents WHERE case_id=?)',(case_id,))];return data
@app.get('/api/cases/{case_id}/versions')
def versions(case_id:str,request:Request):
    with db.connect() as c:user(request,c);get_case(c,case_id);return [json.loads(r['data']) for r in c.execute('SELECT data FROM versions WHERE case_id=? ORDER BY version DESC',(case_id,))]
@app.post('/api/cases/{case_id}/actions/{action}')
def perform(case_id:str,action:str,p:ActionInput,request:Request):
    key=request.headers.get('Idempotency-Key','')
    if not key or len(key)>100:raise HTTPException(422,'Idempotency-Key is required (maximum 100 characters)')
    poc=request.headers.get('X-POC-Console')=='true'
    digest=hashlib.sha256(db.dumps([case_id,action,p.model_dump()]+(['poc_console'] if poc else [])).encode()).hexdigest()
    with db.transaction() as c:
        u=user(request,c);previous=c.execute('SELECT * FROM idempotency WHERE user_id=? AND key=?',(u['id'],key)).fetchone()
        if previous:
            if previous['digest']!=digest:raise HTTPException(409,'Idempotency key reused with different request')
            return json.loads(previous['result'])
        data=get_case(c,case_id);check_version(data,p.version);validate_document(c,case_id,p.payload.get('document_id'))
        reviewer=poc_console.reviewer(data,action,u,poc)
        if poc and action in {'supplier_decision','po_decision','finance_decision'}:rules.required(p.payload.get('reason'),'POC review reason')
        pending_payment=c.execute("SELECT * FROM jobs WHERE case_id=? AND kind='payment' AND status IN ('QUEUED','PROCESSING','UNKNOWN')",(case_id,)).fetchone() if action=='payment_record' else None
        if pending_payment:
            original=json.loads(pending_payment['payload'])
            if pending_payment['status']!='UNKNOWN':raise HTTPException(409,'A payment request is queued or processing; wait for its outcome before recording another payment')
            if p.payload.get('job_id')!=pending_payment['id'] or rules.money(p.payload.get('amount'))!=original['amount'] or data['invoices'][-1]['id']!=original['invoice_id']:raise HTTPException(409,'Reconcile the unknown request explicitly using its job ID and exact amount')
        if action in {'commercial_change','optimization_config','cancel'} and c.execute("SELECT id FROM jobs WHERE case_id=? AND kind IN ('sap','payment') AND status IN ('QUEUED','PROCESSING','UNKNOWN')",(case_id,)).fetchone():
            raise HTTPException(409,'An external submission is pending or unknown; reconcile it before changing the order')
        if action=='mode':
            data=automation.switch(c,data,p.payload.get('mode'),u,p.payload.get('reason',''))
            c.execute('INSERT INTO idempotency VALUES(?,?,?,?)',(u['id'],key,digest,db.dumps(data)));return data
        if action=='classify_record':
            rules.auth(u,{'admin'});kind=p.payload.get('record_kind')
            if kind=='operational' and data.get('poc_reviews'):raise HTTPException(409,'POC-reviewed cases must remain Test records; create a new operational case')
            if kind not in {'test','operational'} or not p.payload.get('reason'):raise HTTPException(422,'Classification and reason required')
            if c.execute("SELECT 1 FROM jobs WHERE case_id=? AND status IN ('PROCESSING','UNKNOWN')",(case_id,)).fetchone():raise HTTPException(409,'Reconcile external actions before reclassification')
            c.execute('UPDATE case_controls SET record_kind=? WHERE case_id=?',(kind,case_id));data['record_kind']=kind
        elif action=='supplier_contacts':
            rules.auth(u,{'buyer'});rules.state(data,{'QUOTATIONS'});contacts=p.payload.get('contacts',[])
            if not isinstance(contacts,list) or not 3<=len(contacts)<=4:raise HTTPException(422,'Provide 3â€“4 authorized supplier contacts')
            import re
            if any(not isinstance(x,dict) or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',str(x.get('email',''))) or not x.get('name') for x in contacts):raise HTTPException(422,'Valid supplier names and emails required')
            data['supplier_contacts']=contacts
        elif action=='transmit_order':
            rules.auth(u,{'buyer'});rules.state(data,{'PO_APPROVED','RECEIVING','QUALITY','INVOICE','INVOICE_MATCHING','MATCHED','FINANCE_REVIEW','PAYMENT_PENDING','COMPLETED'})
            automation.external_guard(data)
            if data['po']['approval']['version']!=data['commercial_version']:raise HTTPException(409,'Current PO approval required')
            if not adapters.status()['sap']['available']:raise HTTPException(503,'SAP integration is not connected')
            if data['po']['external_reference']:raise HTTPException(409,'Order already externally confirmed')
            if c.execute("SELECT id FROM jobs WHERE case_id=? AND kind='sap'",(case_id,)).fetchone():raise HTTPException(409,'Submission exists; reconcile or retry the existing job')
            job=rules.uid('JOB-');c.execute('INSERT INTO jobs(id,case_id,kind,payload,status,created) VALUES(?,?,?,?,?,?)',(job,case_id,'sap',db.dumps({'operation':data['po'].get('kind','purchase_order'),'workflow':case_id,'po':data['po']}),'QUEUED',rules.now()));data['po']['transmission']='Queued; not confirmed'
        elif action=='test_payment':
            if not poc:raise HTTPException(403,'Use the AI Test POC console')
            rules.state(data,{'PAYMENT_PENDING'})
            reason=rules.required(p.payload.get('reason'),'Test completion reason')
            document=rules.uid('DOC-');body=b'POC TEST PAYMENT ONLY. No money transferred. Single-user demonstration.'
            (db.DATA/'documents'/document).write_bytes(body)
            c.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?)',(document,case_id,u['id'],'POC_Test_Payment.txt','text/plain',len(body),hashlib.sha256(body).hexdigest(),rules.now()))
            invoice=data['invoices'][-1]
            balance=Decimal(invoice['total'])-sum((Decimal(x['amount']) for x in data['payments'] if x['invoice']==invoice['id']),Decimal(0))
            data=ai_domain.action(data,'payment_record',{'amount':str(balance),'reference':rules.uid('TEST-PAY-'),'date':datetime.now(timezone.utc).date().isoformat(),'document_id':document,'reason':'POC simulation: '+reason},reviewer)
            data['payments'][-1]['status']='Simulated test payment; no money transferred'
        else:data=ai_domain.action(data,action,p.payload,reviewer)
        if poc:poc_console.record(data,action,u)
        if pending_payment:
            c.execute("UPDATE jobs SET status='RECONCILED',result=? WHERE id=?",(db.dumps({'reference':p.payload['reference'],'document_id':p.payload['document_id'],'actor':u['id'],'attribution':'Human reconciled external bank evidence; not a gateway execution acknowledgment'}),pending_payment['id']))
        data['version']+=1;db.save(c,data);db.audit(c,case_id,u['id'],action,{'version':data['version'],'status':data['status'],'reason':p.payload.get('reason',''),'payload':p.payload,'poc_console':poc,'attribution':'Single-user POC review' if poc else 'Authenticated role action'})
        if action in {'rank','po_submit','receipt','quality','match','finance_submit'}:db.audit(c,case_id,'rules-engine',action+'_recommendation',data['explanations'][-1] if data['explanations'] else {},'agent')
        roles={'SUPPLIER_REVIEW':'procurement','PO_REVIEW':'procurement','PO_APPROVED':'warehouse','RECEIVING':'warehouse','QUALITY':'quality','INVOICE':'buyer','FINANCE_REVIEW':'finance','INVOICE_BLOCKED':'buyer','PAYMENT_PENDING':'finance'}
        if data['status'] in roles:db.notify(c,case_id,roles[data['status']],data['title']+' Â· '+data['status'].replace('_',' ').title())
        c.execute('INSERT INTO idempotency VALUES(?,?,?,?)',(u['id'],key,digest,db.dumps(data)))
        automation.schedule(c,data)
    return data
@app.post('/api/jobs/{job_id}/retry')
def retry_job(job_id:str,request:Request):
    with db.transaction() as c:
        u=user(request,c);rules.auth(u,{'buyer'});job=c.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if not job or job['status']!='UNKNOWN':raise HTTPException(409,'Only unconfirmed jobs can be retried')
        c.execute("UPDATE jobs SET status='QUEUED' WHERE id=?",(job_id,));db.audit(c,job['case_id'],u['id'],'integration_retry',{'job':job_id,'idempotency_key':job_id})
    return {'queued':True}
@app.post('/api/cases/{case_id}/documents')
async def upload(case_id:str,request:Request,file:UploadFile=File(...)):
    with db.connect() as c:u=user(request,c);rules.auth(u,{'buyer','warehouse','quality','finance'});get_case(c,case_id)
    body=await file.read(MAX_UPLOAD+1)
    if len(body)>MAX_UPLOAD:raise HTTPException(413,'Maximum attachment size is 10 MB')
    signatures={'application/pdf':body.startswith(b'%PDF-'),'image/png':body.startswith(b'\x89PNG\r\n\x1a\n'),'image/jpeg':body.startswith(b'\xff\xd8\xff'),'text/plain':b'\x00' not in body,'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet':body.startswith(b'PK\x03\x04')}
    if not signatures.get(file.content_type,False):raise HTTPException(422,'Upload PDF, PNG, JPEG or plain text with a matching file signature')
    digest=hashlib.sha256(body).hexdigest()
    with db.connect() as c:
        existing=c.execute('SELECT id,name FROM documents WHERE case_id=? AND sha256=?',(case_id,digest)).fetchone()
        if existing:return {**dict(existing),'duplicate':True}
    ident=rules.uid('DOC-');path=db.DATA/'documents'/ident;path.write_bytes(body)
    try:
        with db.transaction() as c:
            u=user(request,c)
            existing=c.execute('SELECT id,name FROM documents WHERE case_id=? AND sha256=?',(case_id,digest)).fetchone()
            if existing:path.unlink(missing_ok=True);return {**dict(existing),'duplicate':True}
            c.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?)',(ident,case_id,u['id'],Path(file.filename or 'document').name,file.content_type,len(body),digest,rules.now()))
            db.audit(c,case_id,u['id'],'document_uploaded',{'document':ident,'sha256':hashlib.sha256(body).hexdigest()})
    except BaseException:path.unlink(missing_ok=True);raise
    return {'id':ident,'name':file.filename}
@app.get('/api/documents/{document_id}')
def download(document_id:str,request:Request):
    with db.connect() as c:
        user(request,c);r=c.execute('SELECT * FROM documents WHERE id=?',(document_id,)).fetchone()
        if not r:raise HTTPException(404,'Document not found')
        path=db.DATA/'documents'/document_id
        if not path.is_file():raise HTTPException(410,'Stored file is unavailable')
        return FileResponse(path,media_type='application/octet-stream',filename=r['name'])
@app.get('/api/audit')
def audit_list(request:Request,case_id:str|None=None):
    with db.connect() as c:
        user(request,c)
        rows=c.execute('SELECT a.*,u.name actor_name FROM audit a LEFT JOIN users u ON a.actor=u.id '+('WHERE case_id=? ' if case_id else '')+'ORDER BY seq DESC LIMIT 500',(case_id,) if case_id else ()).fetchall()
        return {'integrity':db.verify_audit(c),'events':[dict(r) for r in rows]}
@app.get('/api/notifications')
def notifications(request:Request):
    with db.connect() as c:u=user(request,c);return [dict(r) for r in c.execute('SELECT * FROM notifications WHERE user_id=? ORDER BY created DESC LIMIT 100',(u['id'],))]
@app.post('/api/notifications/read')
def read_notifications(request:Request):
    with db.transaction() as c:u=user(request,c);c.execute('UPDATE notifications SET read=1 WHERE user_id=?',(u['id'],))
    return {'ok':True}
@app.get('/api/integrations')
def integrations(request:Request):
    with db.connect() as c:
        user(request,c);statuses={**adapters.status(),'groq':groq_provider.status(),'optimization':{'available':True,'status':'HiGHS available locally'}}
        for r in c.execute('SELECT * FROM service_health'):
            if r['kind'] in statuses:statuses[r['kind']].update(status=r['status'],checked=r['checked'])
        return statuses
@app.get('/api/dashboard')
def dashboard(request:Request,currency:str='USD',record_kind:str='operational'):
    if record_kind not in {'operational','test','all'}:raise HTTPException(422,'Unknown reporting environment')
    with db.connect() as c:
        u=user(request,c);data=[get_case(c,r['id']) for r in c.execute('SELECT id FROM cases')]
    if record_kind!='all':data=[x for x in data if x['record_kind']==record_kind]
    scoped=[x for x in data if x['currency']==currency];suppliers={};trend={}
    for x in scoped:
        if x['po'] and x['po']['approval']:
            if x['po'].get('lines'):
                for line in x['po']['lines']:suppliers[line['supplier']]=suppliers.get(line['supplier'],Decimal(0))+rules.dec(line['total'])
            else:
                q=x['po']['quote'];suppliers[q['supplier']]=suppliers.get(q['supplier'],Decimal(0))+rules.dec(x['po']['total'])
            month=x['po']['created'][:7];trend.setdefault(month,{'po':Decimal(0),'invoice':Decimal(0)})['po']+=rules.dec(x['po']['total'])
        for i in x['invoices']:
            if i['status']!='VOID':trend.setdefault(i['date'][:7],{'po':Decimal(0),'invoice':Decimal(0)})['invoice']+=rules.dec(i['total'])
    return {'currency':currency,'record_kind':record_kind,'scope':record_kind.title()+' records; all time. Approved PO value and recorded invoice value by calendar month. Test and unclassified legacy records are excluded from operational analytics.','currencies':sorted({x['currency'] for x in data}|{'USD'}),'active':sum(x['status'] not in {'COMPLETED','CANCELLED'} for x in data),'pending':sum(x['status'] in {'SUPPLIER_REVIEW','PO_REVIEW','FINANCE_REVIEW'} for x in data),'po_total':rules.money(sum(suppliers.values(),Decimal(0))),'verification':sum(i['status'] in {'UNMATCHED','BLOCKED'} for x in data for i in x['invoices']),'suppliers':[{'name':k,'amount':rules.money(v)} for k,v in sorted(suppliers.items(),key=lambda x:-x[1])],'trend':[{'month':k,**{n:rules.money(v) for n,v in vals.items()}} for k,vals in sorted(trend.items())]}
@app.get('/health/live')
def live():return {'status':'ok'}
@app.get('/health/ready')
def ready():
    try:
        with db.connect() as c:c.execute('SELECT version FROM migrations LIMIT 1').fetchone()
        return {'status':'ready'}
    except Exception:raise HTTPException(503,'Database unavailable')
automation_api.register(app,user)
app.mount('/assets',StaticFiles(directory=Path(__file__).parent/'static'),name='assets')
@app.get('/')
def index():return FileResponse(Path(__file__).parent/'static/index.html')
