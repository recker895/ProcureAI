"""Per-model pacing and safe, structured Groq limit diagnostics."""
import hashlib,json,math,re,threading,time
from email.utils import parsedate_to_datetime
import domain as d

lock=threading.Lock()
buckets={}

class ProviderBusy(d.RuleError):
    def __init__(self,message,delay=60,details=None):
        super().__init__(message,503)
        self.retry_after=max(1,float(delay));self.details=details or {}

def duration(value):
    try:return max(0,float(value))
    except (ValueError,TypeError):pass
    matches=re.findall(r'(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)',str(value))
    if matches:return sum(float(n)*{'ms':.001,'s':1,'m':60,'h':3600,'d':86400}[u] for n,u in matches)
    try:return max(0,parsedate_to_datetime(value).timestamp()-time.time())
    except (ValueError,TypeError,OverflowError):return 0

def diagnose(response,model):
    try:message=str(response.json().get('error',{}).get('message',''))
    except (ValueError,AttributeError):message=''
    metrics={'TPM':'tokens per minute','TPD':'tokens per day','RPM':'requests per minute','RPD':'requests per day','ITPM':'input tokens per minute','OTPM':'output tokens per minute'}
    metric=next((k for k in ['ITPM','OTPM','TPD','RPD','TPM','RPM'] if re.search(r'\b'+k+r'\b',message,re.I)),'UNKNOWN')
    if metric=='UNKNOWN':metric=next((k for k,v in metrics.items() if v in message.lower()),'UNKNOWN')
    details={'model':model,'limit_type':metric}
    for name in ['limit','used','requested']:
        match=re.search(r'\b'+name+r'\s*[:=]?\s*([\d,.]+)',message,re.I)
        if match:
            try:details[name]=float(match[1].replace(',',''))
            except ValueError:pass
    retry=duration(response.headers.get('retry-after'))
    if not retry:
        match=re.search(r'try again in\s+([\d.hms\s]+)',message,re.I)
        retry=duration(match[1]) if match else 0
    if not retry:retry=duration(response.headers.get('x-ratelimit-reset-requests' if metric in {'RPM','RPD'} else 'x-ratelimit-reset-tokens'))
    if not retry:retry=3600 if metric in {'TPD','RPD'} else 60
    details['retry_at']=time.time()+retry
    text=f'Groq {model}: {metrics.get(metric,"rate limit (provider did not identify its type)")} reached'
    values=', '.join(f'{name} {details[name]:g}' for name in ['limit','used','requested'] if name in details)
    if values:text+=' ('+values+')'
    return ProviderBusy(text+'. Automatic retry is scheduled.',retry,details)

def completion(client,key,payload):
    model=payload['model'];ident=(hashlib.sha256(key.encode()).hexdigest()[:12],model)
    # Serialize reservations and provider calls in this single-process deployment.
    with lock:
        bucket=buckets.setdefault(ident,{'limit':8000,'remaining':8000.,'updated':time.monotonic(),'cooldown':0,'details':{}})
        if bucket['cooldown']>time.time():
            raise ProviderBusy('Groq '+model+' is waiting for its scheduled capacity reset.',bucket['cooldown']-time.time(),bucket['details'])
        limit=bucket['limit'];available=min(limit,bucket['remaining']+(time.monotonic()-bucket['updated'])*limit/60)
        needed=math.ceil(len(json.dumps({k:v for k,v in payload.items() if k not in {'max_completion_tokens','model'}}))/3)+payload['max_completion_tokens']
        if needed>limit:d.fail('Agent request exceeds '+model+' token budget; split the document or increase account capacity.',422)
        delay=max(0,(needed-available)*60/limit)
        if delay>0:
            raise ProviderBusy('Groq '+model+' token budget is replenishing; automatic retry scheduled.',delay,{'model':model,'limit_type':'LOCAL_TPM_PACING','limit':limit,'requested':needed,'retry_at':time.time()+delay})
        response=client.post('https://api.groq.com/openai/v1/chat/completions',headers={'Authorization':'Bearer '+key},json=payload)
        try:
            bucket.update(limit=int(response.headers.get('x-ratelimit-limit-tokens',limit)),remaining=float(response.headers.get('x-ratelimit-remaining-tokens',max(0,available-needed))),updated=time.monotonic())
        except ValueError:pass
        if response.status_code==429:
            error=diagnose(response,model);bucket.update(cooldown=time.time()+error.retry_after,details=error.details);raise error
        bucket['cooldown']=0
        return response
