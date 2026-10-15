"""Explicit gateway contract. Credentials are loaded only from environment."""
import os, httpx
from urllib.parse import urlparse
from domain import RuleError
def config(kind):
    prefix={'sap':'SAP','email':'EMAIL','payment':'PAYMENT','warehouse':'WAREHOUSE','quality':'QUALITY','supplier':'SUPPLIER'}[kind]
    return os.getenv(prefix+'_GATEWAY_URL',''),os.getenv(prefix+'_GATEWAY_TOKEN','')
def status():
    result={}
    for kind in ['sap','email','payment','warehouse','quality','supplier']:
        gateway=all(config(kind));events=bool(os.getenv(kind.upper()+'_WEBHOOK_SECRET')) if kind in {'payment','warehouse','quality','supplier'} else False
        result[kind]={'status':'Configured; connection unverified' if gateway else 'Signed events configured; connection unverified' if events else 'Not connected','available':gateway,'signed_events_available':events}
    return result
def submit(kind,payload,key):
    endpoint,token=config(kind)
    if not endpoint or not token:raise RuleError('Integration is not connected',503)
    parsed=urlparse(endpoint)
    if parsed.scheme!='https' or parsed.username or parsed.password:raise RuleError('Gateway must be an HTTPS URL without embedded credentials',503)
    if kind=='email':
        recipients=payload.get('recipients',[])
        allowed={x.strip().lower() for x in os.getenv('EMAIL_ALLOWED_RECIPIENTS','').split(',') if x.strip()}
        if not recipients or any(x.lower() not in allowed for x in recipients):raise RuleError('Email recipient is outside the configured allowlist',403)
    # Configured gateways must implement this documented contract; not a guessed SAP API.
    with httpx.Client(timeout=20,follow_redirects=False) as client:
        response=client.post(endpoint.rstrip('/')+'/submit',json=payload,headers={'Authorization':'Bearer '+token,'Idempotency-Key':key})
        response.raise_for_status();body=response.json()
    if body.get('status')!='confirmed' or not isinstance(body.get('reference'),str) or not body['reference'].strip():raise RuleError('External outcome unconfirmed; reconcile using the existing idempotency key',502)
    return {'status':'confirmed','reference':body['reference'],**{k:body[k] for k in ['amount','currency','invoice_id','settled','date','events','orders','payee_reference'] if k in body}}

def poll(kind,case_id):
    endpoint,token=config(kind)
    if not endpoint or not token:raise RuleError('Integration is not connected',503)
    parsed=urlparse(endpoint)
    if parsed.scheme!='https' or parsed.username or parsed.password:raise RuleError('Gateway must be HTTPS',503)
    with httpx.Client(timeout=20,follow_redirects=False) as client:
        response=client.get(endpoint.rstrip('/')+'/events',params={'workflow':case_id},headers={'Authorization':'Bearer '+token});response.raise_for_status();body=response.json()
    if not isinstance(body.get('events'),list):raise RuleError('Invalid gateway events response',502)
    return body['events']
