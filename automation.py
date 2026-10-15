"""SQLite-backed durable coordinator. External I/O runs outside DB transactions.

Business transitions use the same server rules as humans. No agent can approve.
Deployment is one application process; independent read-only tasks run concurrently.
"""
import copy, json, os, time, hashlib, secrets
from datetime import date
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
import storage as db
import domain as d
import ai_domain as business
import optimization
import groq_provider as groq
import adapters
import document_intake

WORKER=secrets.token_hex(12)
MAX_ATTEMPTS=3

def load(c,ident):
    row=c.execute('SELECT data FROM cases WHERE id=?',(ident,)).fetchone()
    if not row:d.fail('Workflow not found',404)
    data=json.loads(row['data']);control=c.execute('SELECT * FROM case_controls WHERE case_id=?',(ident,)).fetchone()
    data['mode']=control['mode'] if control else 'manual';data['record_kind']=control['record_kind'] if control else 'operational';data['mode_epoch']=control['epoch'] if control else 0
    return data

def fingerprint(value):return hashlib.sha256(db.dumps(value).encode()).hexdigest()
def business_snapshot(data):return {k:v for k,v in data.items() if k not in {'version','updated','explanations','agent_tasks','agent_runs','summary','documents','jobs'}}
def actor(agent):return {'id':'agent:'+str(agent),'role':'buyer','name':groq.AGENTS[agent]}

def enqueue(c,data,agent,intent,extra=None):
    intent=fingerprint(intent);row=c.execute('SELECT * FROM agent_tasks WHERE case_id=? AND agent=? AND intent=? AND epoch=?',(data['id'],agent,intent,data['mode_epoch'])).fetchone()
    if row:
        if row['status']=='CANCELLED':c.execute("UPDATE agent_tasks SET status='QUEUED',available=?,error=NULL,updated=? WHERE id=?",(time.time(),d.now(),row['id']))
        return row['id']
    ident=d.uid('TASK-');created=d.now()
    c.execute('INSERT INTO agent_tasks(id,case_id,agent,intent,epoch,status,available,input,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)',(ident,data['id'],agent,intent,data['mode_epoch'],'QUEUED',time.time(),db.dumps(extra or {}),created,created));return ident

def completed(c,data,agent,intent):
    return bool(c.execute("SELECT 1 FROM agent_tasks WHERE case_id=? AND agent=? AND intent=? AND epoch=? AND status='COMPLETED'",(data['id'],agent,fingerprint(intent),data['mode_epoch'])).fetchone())

def deduplicate_intakes(c,data):
    seen={}
    for row in c.execute('SELECT i.*,doc.sha256 FROM document_intake i JOIN documents doc ON doc.id=i.document_id WHERE doc.case_id=? ORDER BY doc.created,doc.rowid',(data['id'],)).fetchall():
        key=(row['sha256'],row['kind'])
        if key not in seen:seen[key]=row['document_id'];continue
        if row['status'] in {'DUPLICATE','APPLIED'}:continue
        canonical=seen[key];note='Duplicate content; extraction belongs to '+canonical
        c.execute("UPDATE document_intake SET status='DUPLICATE',error=?,proposal=? WHERE document_id=?",(note,db.dumps({'duplicate_of':canonical}),row['document_id']))
        c.execute("UPDATE agent_tasks SET status='SUPERSEDED',lease_owner=NULL,lease_until=NULL,error=?,updated=? WHERE case_id=? AND agent=3 AND json_extract(input,'$.document_id')=? AND status IN ('QUEUED','RETRY','BLOCKED','RUNNING')",(note,d.now(),data['id'],row['document_id']))
        db.audit(c,data['id'],'document-intake','duplicate_extraction_skipped',{'document_id':row['document_id'],'duplicate_of':canonical},'system')

def schedule(c,data):
    if data['mode']!='ai':return
    deduplicate_intakes(c,data)
    if data['status'] in {'COMPLETED','CANCELLED'}:
        c.execute("UPDATE agent_tasks SET status='SUPERSEDED',updated=? WHERE case_id=? AND status='BLOCKED'",(d.now(),data['id']))
        c.execute("UPDATE agent_tasks SET status='CANCELLED',updated=? WHERE case_id=? AND status IN ('QUEUED','RETRY') AND agent NOT IN (1,2,13)",(d.now(),data['id']))
    if data['status']=='CANCELLED':
        c.execute("UPDATE agent_tasks SET status='CANCELLED',updated=? WHERE case_id=? AND status IN ('QUEUED','RETRY')",(d.now(),data['id']));return
    cv=data['commercial_version'];state=data['status'];quotes=[(q['id'],q['version']) for q in data['quotes']]
    phase=[cv,state,quotes,data['po']['id'] if data['po'] else None,[(r['id'],r['inspected']) for r in data['receipts']],[(i['id'],i['status']) for i in data['invoices']]]
    enqueue(c,data,1,['workflow-stage',state])
    requirements={k:data[k] for k in ['material','quantity','unit','currency','deadline','min_quality','certifications']}
    enqueue(c,data,2,['requirements',requirements])
    for row in c.execute("SELECT * FROM document_intake WHERE document_id IN (SELECT id FROM documents WHERE case_id=?) AND status='QUEUED'",(data['id'],)):
        enqueue(c,data,3,['document',row['document_id']],{'document_id':row['document_id']})
    if state=='QUOTATIONS':
        if data.get('supplier_contacts'):
            interval=max(1,int(os.getenv('EMAIL_REMINDER_DAYS','2')));age=max(0,(date.today()-date.fromisoformat(data['created'][:10])).days);bucket=min(age//interval,int(os.getenv('EMAIL_MAX_REMINDERS','2')))
            req=[data[k] for k in ['material','quantity','currency','deadline','certifications']];enqueue(c,data,4,['rfq',req,data['supplier_contacts'],bucket])
        if len(data['quotes'])>=3:enqueue(c,data,5,['risk',cv,quotes])
        if 3<=len(quotes)<=4 and completed(c,data,5,['risk',cv,quotes]):enqueue(c,data,6,['allocation',cv,quotes,data.get('optimization',{})])
    if state=='SUPPLIER_APPROVED':enqueue(c,data,7,['po',cv,data['selected']])
    if state in {'PO_APPROVED','RECEIVING','QUALITY','INVOICE'}:
        enqueue(c,data,8,['delivery',cv,date.today().isoformat(),data['po'].get('external_reference')])
        if data['receipts']:enqueue(c,data,9,['receipt',[(r['id'],r['quantity']) for r in data['receipts']]])
        if any(not r['inspected'] for r in data['receipts']):enqueue(c,data,10,['quality',[r['id'] for r in data['receipts'] if not r['inspected']]])
    if state in {'INVOICE_MATCHING','INVOICE_BLOCKED'}:
        i=data['invoices'][-1];enqueue(c,data,11,['match',cv,i['id'],i['quantity'],i['unit_price'],i['total'],i['currency']])
    if state=='MATCHED':enqueue(c,data,12,['finance',cv,data['invoices'][-1]['id']])
    if state in {'PAYMENT_PENDING','COMPLETED'}:enqueue(c,data,13,['settlement',cv,data['invoices'][-1]['id'],d.summary(data)['paid']])
    # Expire exceptions when the human has moved the workflow past that phase.
    phase_roles={6:{'QUOTATIONS'},7:{'SUPPLIER_APPROVED'},10:{'QUALITY'},11:{'INVOICE_MATCHING','INVOICE_BLOCKED'},12:{'MATCHED'},13:{'PAYMENT_PENDING','COMPLETED'}}
    for agent,states in phase_roles.items():
        if state not in states:c.execute("UPDATE agent_tasks SET status='SUPERSEDED',updated=? WHERE case_id=? AND agent=? AND status='BLOCKED'",(d.now(),data['id'],agent))

def switch(c,data,mode,u,reason):
    d.auth(u,{'buyer','procurement','admin'})
    if mode not in {'manual','ai'}:d.fail('Choose Manual or AI mode')
    if mode==data['mode']:return data
    d.required(reason,'Mode change reason');data=copy.deepcopy(data)
    # Fence in-progress tasks; queued external actions may be stopped, confirmed outcomes remain.
    epoch=data['mode_epoch']+1;c.execute('UPDATE case_controls SET mode=?,epoch=? WHERE case_id=?',(mode,epoch,data['id']))
    c.execute("UPDATE agent_tasks SET status='CANCELLED',updated=? WHERE case_id=? AND status IN ('QUEUED','RETRY','BLOCKED')",(d.now(),data['id']))
    for row in c.execute("SELECT id,payload FROM jobs WHERE case_id=? AND status='QUEUED'",(data['id'],)).fetchall():
        if json.loads(row['payload']).get('_origin')=='ai':c.execute("UPDATE jobs SET status='CANCELLED' WHERE id=?",(row['id'],))
    previous=data['mode'];data['mode']=mode;data['mode_epoch']=epoch;data['version']+=1;db.save(c,data)
    db.audit(c,data['id'],u['id'],'mode_changed',{'from':previous,'to':mode,'reason':reason,'epoch':epoch,'external_in_flight':'Any processing/unknown action requires reconciliation'})
    schedule(c,data);return data

def external_guard(data):
    if data['record_kind']!='operational':d.fail('Test and unclassified legacy records cannot reach live external services',409)

def queue_gateway(c,data,kind,payload,intent):
    external_guard(data)
    if not adapters.status()[kind]['available']:d.fail(kind.title()+' integration is not connected',503)
    key='EXT-'+fingerprint([data['id'],kind,intent])[:32]
    old=c.execute('SELECT * FROM jobs WHERE id=?',(key,)).fetchone()
    if old:
        if old['status']=='CANCELLED':c.execute("UPDATE jobs SET status='QUEUED' WHERE id=?",(key,))
        return key
    payload={**payload,'workflow':data['id'],'_origin':'ai','_epoch':data['mode_epoch'],'_commercial_version':data['commercial_version']}
    c.execute('INSERT INTO jobs(id,case_id,kind,payload,status,created) VALUES(?,?,?,?,?,?)',(key,data['id'],kind,db.dumps(payload),'QUEUED',d.now()));db.audit(c,data['id'],'agent:1','external_action_queued',{'kind':kind,'idempotency_key':key},'agent');return key

def snapshot_evidence(c,data,task):
    documents=[dict(x) for x in c.execute('SELECT id,name,mime,sha256,created FROM documents WHERE case_id=?',(data['id'],))]
    ids={data['id'],*[q['id'] for q in data['quotes']],*[r['id'] for r in data['receipts']],*[i['id'] for i in data['invoices']],*[x['id'] for x in documents]}
    if data['po']:ids.add(data['po']['id'])
    evidence={'documents':documents,'summary':d.summary(data),'integrations':adapters.status(),'phase':data['status'],'human_gates':['Supplier approval','PO issuance approval','Finance approval'],'record_kind':data['record_kind']}
    if task['agent']==3:
        meta=json.loads(task['input']);row=c.execute('SELECT * FROM document_intake WHERE document_id=?',(meta['document_id'],)).fetchone()
        if not row:d.fail('Intake document unavailable')
        evidence['intake']={'document_id':row['document_id'],'kind':row['kind'],'source':row['source'],'requirements':'Return extract_document with fields, sources (exact excerpts), and missing fields. Never fill absent ratings or physical confirmations.'}
    return evidence,ids

def tool_result(agent,data,evidence):
    if agent==6:return optimization.solve(data)
    if agent==5:
        return {'checks':[{'quotation':q['id'],'document_id':q.get('document_id'),'quality':q['quality'],'reliability':q['reliability'],'risk':q['risk'],'certifications':q['certifications'],'source_status':'Source-linked extracted interpretation' if q.get('source_origin')=='Groq extraction' else 'Authenticated supplier event; reviewer must verify evidence' if q.get('source_origin')=='Authenticated supplier event' else 'Human-entered ratings; reviewer must verify evidence'} for q in data['quotes']],'rule':'No invented ratings or probabilistic confidence. Supplier approval remains human.'}
    if agent==11:
        if data['status'] not in {'INVOICE_MATCHING','INVOICE_BLOCKED'}:return {'waiting':'Invoice input required'}
        result=business.action(data,'match',{},actor(agent));return {'match':result['invoices'][-1]['match'],'outcome':result['status']}
    if agent==12:
        invoice=data['invoices'][-1];line=next((x for x in data['po'].get('lines',[]) if x['quote_id']==invoice.get('quote_id')),None);q=line['quote'] if line else data['po']['quote']
        return {'invoice':invoice,'balance':d.summary(data)['balance'],'payment_candidate':{'amount':invoice['total'],'currency':invoice['currency'],'payee_reference':q.get('payee_reference') or 'Not configured; manual external reconciliation required','due_date':invoice['due_date'],'source_order':line['order_id'] if line else data['po']['id']},'requires':'Independent finance approval of the exact invoice and beneficiary reference'}
    if agent==9:return {'receipts':data['receipts'],'accepted':d.summary(data)['accepted'],'physical_source':'Warehouse confirmations; this agent cannot create receipt facts'}
    if agent==10:return {'pending_inspection':[r for r in data['receipts'] if not r['inspected']],'requires':'Quality reviewer confirmation or authenticated inspection event'}
    if agent==13:return {'payments':data['payments'],'balance':d.summary(data)['balance'],'invoice':data['invoices'][-1],'external_confirmation_required':True}
    return {'requirements':{k:data[k] for k in ['material','quantity','unit','currency','deadline','min_quality','certifications']},'status':data['status'],'documents':evidence['documents']}

def store_run(c,task,data,evidence,response,input_snapshot=None):
    ident=d.uid('RUN-');assessment=response['assessment']
    c.execute('INSERT INTO agent_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(ident,task['id'],data['id'],task['agent'],db.dumps({'case':business_snapshot(input_snapshot or data),'evidence':evidence,'model_request_context':response.get('request_context')}),db.dumps(response['tools']),db.dumps(assessment),response['model'],response['prompt_version'],response['input_tokens'],response['output_tokens'],response['elapsed_ms'],response['cost'],d.now()))
    data['explanations'].append({'id':ident,'agent':groq.AGENTS[task['agent']],'agent_id':task['agent'],'created':d.now(),'version':data['version']+1,'recommendation':assessment['recommendation'],'reasons':assessment['reasons'],'alternatives':assessment['alternatives'],'uncertainties':assessment['uncertainties'],'source_ids':assessment['source_ids'],'next_action':assessment['next_action'],'evidence':evidence,'rules':'Backend validators and immutable tool results govern execution. Groq narrative is an interpretation.','uncertainty':'; '.join(assessment['uncertainties']),'algorithm':response['prompt_version'],'model':response['model'],'run_id':ident})
    db.audit(c,data['id'],'agent:'+str(task['agent']),'agent_assessment',{'run_id':ident,'task':task['id'],'source_ids':assessment['source_ids']},'agent')
    return ident

def do_action(c,data,agent,action,payload):
    if action not in groq.SCOPES[agent]:d.fail('Agent cannot perform this action',403)
    # Approvals are intentionally absent from every scope.
    if payload.get('document_id') and not c.execute('SELECT 1 FROM documents WHERE id=? AND case_id=?',(payload['document_id'],data['id'])).fetchone():d.fail('Evidence must belong to the case')
    result=business.action(data,action,payload,actor(agent))
    db.audit(c,data['id'],'agent:'+str(agent),action,{'reason':payload.get('reason',''),'commercial_version':data['commercial_version']},'agent')
    return result

def advance(c,data,task,response,calculation):
    agent=task['agent'];blocked=None
    if agent==3:
        intake=response['document'];source=calculation['source'];kind=calculation['kind'];proposal=document_intake.validate_proposal(intake,source,kind)
        doc_id=calculation['document_id'];c.execute("UPDATE document_intake SET proposal=?,status='READY',error=NULL WHERE document_id=?",(db.dumps(proposal),doc_id))
        if kind in {'quotation','invoice'}:
            payload={**proposal['fields'],'document_id':doc_id,'field_sources':proposal['sources']}
            required={'quotation':{'supplier','quantity','unit_price','freight','discount','tax_rate','minimum_order','quality','reliability','risk','delivery_date','currency','payment_terms'},'invoice':{'reference','quantity','unit_price','total','currency','date','due_date'}}[kind]
            if required.issubset(payload) and not required.intersection(proposal['missing']):
                try:
                    data=do_action(c,data,3,'quote' if kind=='quotation' else 'invoice',payload);c.execute("UPDATE document_intake SET status='APPLIED' WHERE document_id=?",(doc_id,))
                except d.RuleError as error:blocked=error.message+'; extracted fields remain available for review.'
            else:blocked='Source document lacks required fields: '+', '.join(sorted((required-set(payload))|required.intersection(proposal['missing'])))+'. Review the extraction and supply missing evidence.'
        elif kind in {'receipt','inspection'}:blocked='Physical receipt/inspection proposal requires an accountable warehouse or quality confirmation.'
        else:blocked='Review missing document fields before applying this proposal.'
    elif agent==4:
        contacts=[x for x in data.get('supplier_contacts',[]) if not any(q['supplier'].casefold()==x['name'].casefold() or q.get('supplier_email','').casefold()==x['email'].casefold() for q in data['quotes'])];recipients=[x['email'] for x in contacts]
        if os.getenv('EMAIL_AUTOMATION_ENABLED')!='true':blocked='Automatic supplier communication is disabled. Review RFQ contacts and communication policy.'
        elif not recipients:pass
        else:
            payload={'operation':'rfq','recipients':recipients,'subject':'Quotation request: '+data['material'],'body':f"Please quote {data['quantity']} {data['unit']} of {data['material']} in {data['currency']}, delivery by {data['deadline']}. Include capacity, MOQ, freight, discounts, tax, payment terms and qualification evidence."}
            interval=max(1,int(os.getenv('EMAIL_REMINDER_DAYS','2')));age=max(0,(date.today()-date.fromisoformat(data['created'][:10])).days);bucket=min(age//interval,int(os.getenv('EMAIL_MAX_REMINDERS','2')));requirements=[data[k] for k in ['material','quantity','currency','deadline','certifications']]
            for recipient in recipients:queue_gateway(c,data,'email',{**payload,'recipients':[recipient],'reminder':bucket>0},['rfq',requirements,recipient,bucket])
    elif agent==6:
        if not calculation.get('allocations'):blocked=calculation['explanation']
        elif not calculation['optimal'] and not data.get('allow_feasible_plan',False):blocked='Feasible allocation found, but optimality is not proven. Review the solver result.'
        else:
            # Store the exact tool calculation rather than re-solving an LLM-authored plan.
            data['recommendations'].append(calculation);data['ai_plan']=calculation
            data=do_action(c,data,6,'supplier_submit',{'reason':calculation['explanation']})
    elif agent==7:data=do_action(c,data,7,'po_submit',{})
    elif agent==8:
        if not data['po'].get('external_reference'):
            if data.get('po',{}).get('approval',{}).get('version')!=data['commercial_version']:d.fail('PO approval is stale',409)
            queue_gateway(c,data,'sap',{'operation':data['po'].get('kind','purchase_order'),'po':data['po']},['po',data['po']['id'],data['commercial_version']]);data['po']['transmission']='Queued; not confirmed'
        blocked='Delivery tracking awaits connected supplier/warehouse events or a warehouse confirmation.'
    elif agent==10:blocked='Inspection evidence is ready for the quality reviewer. Agent analysis does not confirm physical quality.'
    elif agent==11:
        data=do_action(c,data,11,'match',{})
        if data['status']=='INVOICE_BLOCKED':blocked='; '.join(data['invoices'][-1]['match']['exceptions'])
    elif agent==12:data=do_action(c,data,12,'finance_submit',{})
    elif agent==13 and data['status']=='PAYMENT_PENDING':
        invoice=data['invoices'][-1];approval=invoice.get('approval',{})
        if approval.get('version')!=data['commercial_version']:d.fail('Current finance approval required',409)
        payee=approval.get('payee_reference','')
        if not payee:blocked='Finance approved the invoice, but no approved payee reference is configured. Reconcile an external payment or obtain approved beneficiary details.'
        else:
            remaining=d.dec(invoice['total'])-sum((d.dec(p['amount']) for p in data['payments'] if p['invoice']==invoice['id']),Decimal(0))
            queue_gateway(c,data,'payment',{'operation':'payment','invoice_id':invoice['id'],'amount':d.money(remaining),'currency':data['currency'],'payee_reference':payee,'finance_approval':approval},['payment',invoice['id'],approval,d.money(remaining)])
    return data,blocked

def process_one():
    with db.transaction() as c:
        c.execute("UPDATE agent_tasks SET status=CASE WHEN attempts>=? THEN 'BLOCKED' ELSE 'RETRY' END,error='Worker interrupted; execution will be revalidated',lease_owner=NULL,lease_until=NULL,available=?,updated=? WHERE status='RUNNING' AND lease_until<?",(MAX_ATTEMPTS,time.time(),d.now(),time.time()))
        row=c.execute("SELECT t.* FROM agent_tasks t JOIN case_controls cc ON cc.case_id=t.case_id WHERE t.status IN ('QUEUED','RETRY') AND t.available<=? AND cc.mode='ai' AND cc.epoch=t.epoch ORDER BY t.created,t.agent LIMIT 1",(time.time(),)).fetchone()
        if not row:return False
        task=dict(row);data=load(c,task['case_id'])
        if task['agent']==3:
            doc_id=json.loads(task['input'])['document_id'];intake=c.execute('SELECT status FROM document_intake WHERE document_id=?',(doc_id,)).fetchone()
            if not intake or intake['status']!='QUEUED':
                c.execute("UPDATE agent_tasks SET status='SUPERSEDED',updated=? WHERE id=?",(d.now(),task['id']));return True
        active={4:{'QUOTATIONS'},5:{'QUOTATIONS'},6:{'QUOTATIONS'},7:{'SUPPLIER_APPROVED'},8:{'PO_APPROVED','RECEIVING','QUALITY','INVOICE'},10:{'QUALITY'},11:{'INVOICE_MATCHING','INVOICE_BLOCKED'},12:{'MATCHED'},13:{'PAYMENT_PENDING','COMPLETED'}}
        if data['status']=='CANCELLED' or (task['agent'] in active and data['status'] not in active[task['agent']]):
            c.execute("UPDATE agent_tasks SET status='SUPERSEDED',updated=? WHERE id=?",(d.now(),task['id']));return True
        evidence,sources=snapshot_evidence(c,data,task);snapshot=copy.deepcopy(data);original=fingerprint(business_snapshot(data))
        c.execute("UPDATE agent_tasks SET status='RUNNING',attempts=attempts+1,lease_owner=?,lease_until=?,updated=? WHERE id=?",(WORKER,time.time()+300,d.now(),task['id']))
    response=None
    try:
        calculation=None
        if task['agent']==3:calculation=evidence['intake']
        def calculate():
            nonlocal calculation
            if calculation is None:calculation=tool_result(task['agent'],snapshot,evidence)
            return calculation
        checkpoint=json.loads(task['result']).get('checkpoint') if task.get('result') else None
        options={'checkpoint':checkpoint} if checkpoint and task['agent']==3 else {}
        response=groq.assess(task['agent'],business_snapshot(snapshot),evidence,sources,calculate,task['agent']==3,**options)
        if calculation is None:calculation=calculate()
        with db.transaction() as c:
            current=load(c,data['id']);lease=c.execute('SELECT * FROM agent_tasks WHERE id=?',(task['id'],)).fetchone()
            if current['mode']!='ai' or current['status']=='CANCELLED' or current['mode_epoch']!=task['epoch'] or lease['lease_owner']!=WORKER:
                c.execute("UPDATE agent_tasks SET status='CANCELLED',updated=? WHERE id=?",(d.now(),task['id']));return True
            if task['agent']!=3 and fingerprint(business_snapshot(current))!=original:
                state='BLOCKED' if task['attempts']+1>=MAX_ATTEMPTS else 'RETRY'
                c.execute("UPDATE agent_tasks SET status=?,available=?,lease_owner=NULL,lease_until=NULL,error='Input changed; review before retry if the attempt limit is reached',updated=? WHERE id=?",(state,time.time()+1,d.now(),task['id']));return True
            run=store_run(c,task,current,evidence,response,snapshot)
            # A failing business action must not erase the assessment; isolate it with a savepoint.
            c.execute('SAVEPOINT advance')
            before_advance=copy.deepcopy(current)
            try:current,blocked=advance(c,current,task,response,calculation);c.execute('RELEASE advance')
            except d.RuleError as error:c.execute('ROLLBACK TO advance');c.execute('RELEASE advance');current=before_advance;blocked=error.message
            current['version']+=1;db.save(c,current)
            status='BLOCKED' if blocked else 'COMPLETED'
            c.execute('UPDATE agent_tasks SET status=?,result=?,error=?,lease_until=NULL,lease_owner=NULL,updated=? WHERE id=?',(status,db.dumps({'run_id':run,'calculation':calculation}),blocked,d.now(),task['id']))
            if blocked:db.notify(c,current['id'],'buyer',groq.AGENTS[task['agent']]+': '+blocked)
            if current['status'] in {'SUPPLIER_REVIEW','PO_REVIEW','FINANCE_REVIEW'}:db.notify(c,current['id'],'finance' if current['status']=='FINANCE_REVIEW' else 'procurement',current['title']+' Â· human approval required')
            schedule(c,current)
    except Exception as error:
        message=error.message if isinstance(error,d.RuleError) else 'Agent failed validation: '+type(error).__name__
        capacity=isinstance(error,groq.ProviderBusy) and getattr(error,'details',{}).get('limit_type') not in {'SERVICE_UNAVAILABLE','MODEL_UNAVAILABLE'}
        retry=isinstance(error,d.RuleError) and error.status==503 and (capacity or task['attempts']+1<MAX_ATTEMPTS) and groq.status()['available']
        with db.transaction() as c:
            delay=getattr(error,'retry_after',min(60,10*2**task['attempts']))
            available=time.time()+delay;details=getattr(error,'details',{})
            checkpoint=getattr(error,'checkpoint',None) or checkpoint
            c.execute('UPDATE agent_tasks SET status=?,error=?,available=?,result=?,attempts=attempts-?,lease_owner=NULL,lease_until=NULL,updated=? WHERE id=? AND lease_owner=?',('RETRY' if retry else 'BLOCKED',message,available,db.dumps({'provider_limit':details,'retry_at':available,'checkpoint':checkpoint}) if capacity else None,1 if capacity else 0,d.now(),task['id'],WORKER))
            db.audit(c,data['id'],'agent:'+str(task['agent']),'agent_exception',{'task':task['id'],'error':message,'retry':retry,'provider_limit':details,'retry_at':available if retry else None},'agent')
    return True

def tick():
    with db.transaction() as c:
        for row in c.execute("SELECT case_id FROM case_controls WHERE mode='ai'").fetchall():schedule(c,load(c,row['case_id']))
    with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(lambda _:process_one(),range(2)))
    poll_events()

def poll_events():
    """Bounded periodic ingestion from configured adapters; event IDs prevent replay."""
    import automation_api
    due=[]
    with db.transaction() as c:
        for row in c.execute("SELECT case_id FROM case_controls WHERE mode='ai' AND record_kind='operational'").fetchall():
            data=load(c,row['case_id'])
            if data['status'] in {'COMPLETED','CANCELLED'}:continue
            for kind in ['supplier','warehouse','quality','payment']:
                if not adapters.status()[kind]['available']:continue
                key='poll:'+kind+':'+data['id'];previous=c.execute('SELECT checked FROM service_health WHERE kind=?',(key,)).fetchone()
                if previous and time.time()-float(previous['checked'])<60:continue
                c.execute('INSERT INTO service_health VALUES(?,?,?,?) ON CONFLICT(kind) DO UPDATE SET checked=excluded.checked',(key,'Scheduled',str(time.time()),'Polling configured gateway'));due.append((kind,data['id']))
                if len(due)>=2:break
            if len(due)>=2:break
    for kind,ident in due:
        try:
            for event in adapters.poll(kind,ident)[:20]:automation_api.ingest_event(kind,event,fingerprint(event))
        except Exception as error:
            message=error.message if isinstance(error,d.RuleError) else 'Gateway polling failed: '+type(error).__name__
            with db.transaction() as c:db.audit(c,ident,'gateway:'+kind,'event_poll_failed',{'error':message},'external system')

def queues(c,u):
    tasks=[dict(r) for r in c.execute('SELECT t.*,json_extract(c.data,\'$.title\') title FROM agent_tasks t JOIN cases c ON c.id=t.case_id ORDER BY t.updated DESC LIMIT 200')]
    working=[{k:r[k] for k in ['id','case_id','agent','status','title','updated','attempts','available','error']} for r in tasks if r['status'] in {'QUEUED','RUNNING','RETRY'}]
    exceptions=[{k:r[k] for k in ['id','case_id','agent','status','title','updated','error']} for r in tasks if r['status']=='BLOCKED']
    decisions=[]
    for row in c.execute('SELECT id FROM cases'):
        data=load(c,row['id']);owner={'SUPPLIER_REVIEW':'procurement','PO_REVIEW':'procurement','FINANCE_REVIEW':'finance','QUALITY':'quality','PO_APPROVED':'warehouse','RECEIVING':'warehouse'}.get(data['status'])
        poc=data.get('mode')=='ai' and data.get('record_kind')=='test' and data['creator']==u['id'] and u['role']=='buyer'
        if owner==u['role'] or (poc and (owner or data['status'] in {'INVOICE','PAYMENT_PENDING'})):decisions.append({'case_id':data['id'],'title':data['title'],'status':data['status'],'owner':owner,'updated':data['updated'],'next_action':'Process next Â· single-user POC' if poc else 'Confirm inspection' if owner=='quality' else 'Confirm receipt' if owner=='warehouse' else 'Review approval'})
    metrics=c.execute('SELECT COUNT(*),COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0),COALESCE(SUM(elapsed_ms),0),SUM(CAST(cost AS REAL)) FROM agent_runs').fetchone()
    return {'working':working,'decisions':decisions,'exceptions':exceptions,'roles':groq.AGENTS,'metrics':{'runs':metrics[0],'input_tokens':metrics[1],'output_tokens':metrics[2],'elapsed_ms':metrics[3],'estimated_cost':metrics[4],'cost_note':'Unknown until configured token prices are supplied; this is not billing data'}}
