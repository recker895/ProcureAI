"""Pure, reproducible procurement rules. No network or language model decisions."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import date, datetime, timezone
import copy
import uuid

ALGORITHM = 'constrained-weighted-ranking/1.0'
ROLES = {'buyer','procurement','warehouse','quality','finance','admin','viewer'}
class RuleError(Exception):
    def __init__(self,message,status=422): self.message,self.status=message,status
def fail(message,status=422): raise RuleError(message,status)
def now(): return datetime.now(timezone.utc).isoformat()
def uid(prefix=''): return prefix+uuid.uuid4().hex[:16]
def dec(value,minimum='0'):
    if len(str(value))>40:fail('Decimal value is too long')
    try: x=Decimal(str(value))
    except (InvalidOperation,ValueError): fail('Invalid decimal value')
    if not x.is_finite() or x < Decimal(minimum) or x > Decimal('1000000000000'): fail('Decimal value out of range')
    return x
def money(x): return str(dec(x).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))
def quantity(x):
    n=dec(x,'0.001')
    if n.as_tuple().exponent < -3: fail('Quantity supports at most 3 decimals')
    return str(n)
def percent(x):
    n=dec(x)
    if n>100: fail('Percentage must be between 0 and 100')
    return str(n)
def iso(x):
    try: return date.fromisoformat(str(x)).isoformat()
    except (ValueError,TypeError): fail('Date must be YYYY-MM-DD')
def required(x,label):
    if not isinstance(x,str):fail(label+' must be text')
    s=str(x or '').strip()
    if not s or len(s)>500: fail(label+' is required (maximum 500 characters)')
    return s
def certifications(value):
    if not isinstance(value,list) or len(value)>30 or any(not isinstance(x,str) or len(x)>100 for x in value):fail('Certifications must be a list of at most 30 short text labels')
    return sorted({x.strip() for x in value if x.strip()})
def summary(c):
    accepted=sum((dec(r['accepted']) for r in c['receipts']),Decimal(0))
    pending=sum((dec(r['quantity']) for r in c['receipts'] if not r['inspected']),Decimal(0))
    billed=sum((dec(i['quantity']) for i in c['invoices'] if i['status']!='VOID'),Decimal(0))
    total=sum((dec(i['total']) for i in c['invoices'] if i['status']!='VOID'),Decimal(0))
    paid=sum((dec(p['amount']) for p in c['payments']),Decimal(0))
    return {'accepted':str(accepted),'pending':str(pending),'invoiced':str(billed),'invoice_total':money(total),'paid':money(paid),'balance':money(total-paid)}
def create_case(p,u):
    currency=required(p.get('currency','USD'),'Currency').upper()
    if len(currency)!=3 or not currency.isalpha(): fail('Use a three-letter currency code')
    return {'id':uid('PR-'),'title':required(p.get('title'),'Title'),'material':required(p.get('material'),'Material'),'quantity':quantity(p.get('quantity')),'unit':required(p.get('unit','EA'),'Unit'),'currency':currency,'deadline':iso(p.get('deadline')),'min_quality':percent(p.get('min_quality',80)),'certifications':certifications(p.get('certifications',[])),'amount_tolerance':'0.01','creator':u['id'],'created':now(),'updated':now(),'version':1,'commercial_version':1,'status':'QUOTATIONS','quotes':[],'weights':{'cost':40,'quality':20,'delivery':15,'reliability':15,'risk':10},'recommendations':[],'selected':None,'supplier_approval':None,'po':None,'receipts':[],'invoices':[],'payments':[],'explanations':[]}
def quotation(p,c):
    currency=required(p.get('currency',c['currency']),'Currency').upper()
    return {'id':required(p.get('id'),'Quotation ID') if p.get('id') else uid('Q-'),'supplier':required(p.get('supplier'),'Supplier'),'quantity':quantity(p.get('quantity')),'unit_price':money(p.get('unit_price')),'freight':money(p.get('freight',0)),'discount':money(p.get('discount',0)),'tax_rate':percent(p.get('tax_rate',0)),'currency':currency,'delivery_date':iso(p.get('delivery_date')),'quality':percent(p.get('quality')),'reliability':percent(p.get('reliability')),'risk':percent(p.get('risk')),'certifications':certifications(p.get('certifications',[])),'payment_terms':required(p.get('payment_terms','Net 30'),'Payment terms'),'document_id':p.get('document_id'),'version':1}
def landed(q,c):
    subtotal=dec(c['quantity'])*dec(q['unit_price'])+dec(q['freight'])-dec(q['discount'])
    if subtotal<0: fail('Discount exceeds quotation value')
    return dec(money(subtotal*(1+dec(q['tax_rate'])/100)))
def rank(c,sensitivity=True):
    if len(c['quotes'])<3 or len(c['quotes'])>4: fail('Provide quotations from 3–4 distinct suppliers')
    if len({q['supplier'].casefold() for q in c['quotes']})!=len(c['quotes']): fail('Suppliers must be distinct')
    if not isinstance(c['weights'],dict):fail('Weights must be an object')
    weights={k:dec(c['weights'].get(k,0)) for k in ['cost','quality','delivery','reliability','risk']}
    if sum(weights.values())!=100: fail('Ranking weights must sum to 100')
    rows=[]
    for q in c['quotes']:
        reasons=[]
        if dec(q['quantity'])<dec(c['quantity']): reasons.append('Available quantity below requirement')
        if q['delivery_date']>c['deadline']: reasons.append('Delivery exceeds deadline')
        if dec(q['quality'])<dec(c['min_quality']): reasons.append('Quality below minimum')
        if not set(c['certifications']).issubset(set(q['certifications'])): reasons.append('Required certification missing')
        if q['currency']!=c['currency']: reasons.append('Currency differs; reviewed conversion is required')
        rows.append({'quote_id':q['id'],'supplier':q['supplier'],'eligible':not reasons,'excluded_reasons':reasons,'landed_cost':money(landed(q,c)),'metrics':{'cost':str(landed(q,c)),'quality':q['quality'],'delivery':str(date.fromisoformat(q['delivery_date']).toordinal()),'reliability':q['reliability'],'risk':q['risk']}})
    eligible=[r for r in rows if r['eligible']]
    for r in eligible:
        normalized={}; contributions={}
        for k in weights:
            vals=[dec(e['metrics'][k]) for e in eligible]; lo,hi=min(vals),max(vals)
            n=Decimal(1) if hi==lo else (dec(r['metrics'][k])-lo)/(hi-lo)
            if k in ['cost','delivery','risk'] and hi!=lo: n=1-n
            normalized[k]=str(n.quantize(Decimal('.000001'))); contributions[k]=str((n*weights[k]).quantize(Decimal('.000001')))
        r.update(normalized=normalized,contributions=contributions,score=str(sum(dec(v) for v in contributions.values()).quantize(Decimal('.000001'))))
    eligible.sort(key=lambda r:(-dec(r['score']),dec(r['landed_cost']),r['supplier'].casefold(),r['quote_id']))
    for n,r in enumerate(eligible,1): r['rank']=n
    result={'id':uid('REC-'),'algorithm':ALGORITHM,'created':now(),'commercial_version':c['commercial_version'],'inputs':copy.deepcopy({'quantity':c['quantity'],'currency':c['currency'],'deadline':c['deadline'],'min_quality':c['min_quality'],'certifications':c['certifications'],'quotes':c['quotes'],'weights':c['weights']}),'rows':eligible+[r for r in rows if not r['eligible']],'winner':eligible[0]['quote_id'] if eligible else None,'rules':'Eligibility first. Min-max scores within eligible suppliers; equal metrics receive 1. Cost, delivery and risk favor lower values. Weights sum to 100. Ties: landed cost, supplier name, quotation ID. No cross-currency comparison without reviewed conversion.','uncertainties':'Quality, reliability and risk are reviewer-supplied evidence ratings; validate their source documents. No probability confidence is implied.'}
    result['explanation']=('Recommend '+eligible[0]['supplier']+' with score '+eligible[0]['score']+' and landed cost '+eligible[0]['landed_cost']+' '+c['currency']+'. All mandatory constraints passed.' if eligible else 'No eligible supplier. Correct quotations or obtain a reviewed policy change before continuing.')
    if sensitivity:
        result['sensitivity']=[]
        # Transfer 10 weight points between cost and each other factor. Eligibility unchanged.
        for k in ['quality','delivery','reliability','risk']:
            for direction in [-1,1]:
                variant=copy.deepcopy(c);w=copy.deepcopy(weights);delta=Decimal(10)*direction
                if w['cost']-delta<0 or w[k]+delta<0:continue
                w['cost']-=delta;w[k]+=delta;variant['weights']={k:str(v) for k,v in w.items()};v=rank(variant,False)
                result['sensitivity'].append({'scenario':('Increase ' if direction>0 else 'Decrease ')+k+' weight by 10; offset cost','weights':variant['weights'],'winner':v['winner'],'changed':v['winner']!=result['winner']})
    return result
def auth(u,roles):
    if u['role'] not in roles: fail('Your role is not authorized for this action',403)
def state(c,allowed):
    if c['status'] not in allowed: fail('Action is unavailable in state '+c['status'],409)
def explain(c,agent,recommendation,evidence,rules,uncertainty=''):
    c['explanations'].append({'id':uid('EXP-'),'agent':agent,'created':now(),'version':c['version']+1,'recommendation':recommendation,'evidence':evidence,'rules':rules,'uncertainty':uncertainty,'algorithm':'procurement-rules/1.0'})
def action(c,a,p,u):
    c=copy.deepcopy(c)
    if not isinstance(p.get('reason',''),str) or len(p.get('reason',''))>4000:fail('Reason must be text of at most 4000 characters')
    if 'decision' in p and not isinstance(p['decision'],str):fail('Decision must be text')
    reason=p.get('reason','').strip()
    if a=='quote':
        auth(u,{'buyer'});state(c,{'QUOTATIONS'})
        q=quotation(p,c);existing=next((x for x in c['quotes'] if x['id']==q['id']),None)
        if existing:q['version']=existing['version']+1;c['quotes'].remove(existing)
        elif len(c['quotes'])>=4:fail('Maximum four quotations')
        if any(x['supplier'].casefold()==q['supplier'].casefold() for x in c['quotes']):fail('Supplier already has a quotation')
        c['quotes'].append(q);c['commercial_version']+=1
    elif a=='rank':
        auth(u,{'buyer'});state(c,{'QUOTATIONS'})
        c['weights']=p.get('weights',c['weights']);r=rank(c);c['recommendations'].append(r)
        explain(c,'Supplier selection',r['explanation'],r['inputs'],r['rules'],r['uncertainties'])
    elif a=='supplier_submit':
        auth(u,{'buyer'});state(c,{'QUOTATIONS'})
        if not c['recommendations']:fail('Run supplier ranking first')
        r=c['recommendations'][-1]
        if r['commercial_version']!=c['commercial_version']:fail('Ranking is stale; recalculate',409)
        selected=p.get('quote_id') or r['winner']
        if not any(x['quote_id']==selected and x['eligible'] for x in r['rows']):fail('Choose an eligible quotation')
        if selected!=r['winner'] and not reason:fail('Supplier override requires a reason')
        c['selected']=selected;c['supplier_submission']={'actor':u['id'],'version':c['commercial_version'],'reason':reason};c['status']='SUPPLIER_REVIEW'
    elif a=='supplier_decision':
        auth(u,{'procurement'});state(c,{'SUPPLIER_REVIEW'})
        if u['id'] in {c['creator'],c['supplier_submission']['actor']}:fail('Cannot approve your own submission',403)
        decision=p.get('decision')
        if decision not in {'approve','reject','changes'}:fail('Invalid decision')
        if decision!='approve' and not reason:fail('Provide a decision reason')
        if decision=='approve':c['supplier_approval']={'actor':u['id'],'version':c['commercial_version'],'created':now(),'reason':reason};c['status']='SUPPLIER_APPROVED'
        else:c['selected']=None;c['status']='QUOTATIONS'
    elif a=='po_submit':
        auth(u,{'buyer'});state(c,{'SUPPLIER_APPROVED'})
        q=next(x for x in c['quotes'] if x['id']==c['selected'])
        c['po']={'id':uid('PO-'),'quote':copy.deepcopy(q),'quantity':c['quantity'],'total':money(landed(q,c)),'currency':c['currency'],'commercial_version':c['commercial_version'],'submitter':u['id'],'created':now(),'approval':None,'transmission':'Not connected','external_reference':None}
        c['status']='PO_REVIEW';explain(c,'PO preparation','Review draft '+c['po']['id'],c['po'],'Draft copies the approved quotation. Total = (quantity × unit price + freight − discount) × (1 + tax rate / 100).')
    elif a=='po_decision':
        auth(u,{'procurement'});state(c,{'PO_REVIEW'})
        if u['id']==c['po']['submitter']:fail('Cannot approve your own PO',403)
        decision=p.get('decision')
        if decision not in {'approve','reject','changes'}:fail('Invalid decision')
        if decision!='approve' and not reason:fail('Provide a decision reason')
        if decision=='approve':c['po']['approval']={'actor':u['id'],'created':now(),'version':c['commercial_version']};c['status']='PO_APPROVED'
        else:c['po']=None;c['status']='SUPPLIER_APPROVED'
    elif a=='receipt':
        auth(u,{'warehouse'});state(c,{'PO_APPROVED','RECEIVING','QUALITY','INVOICE'})
        if not c['po']['approval']:fail('PO approval required')
        n=quantity(p.get('quantity'));s=summary(c)
        if dec(s['accepted'])+dec(s['pending'])+dec(n)>dec(c['quantity']):fail('Receipt exceeds remaining quantity')
        ref=required(p.get('reference'),'Delivery reference')
        if any(r['reference']==ref for r in c['receipts']):fail('Duplicate delivery reference',409)
        r={'id':uid('GR-'),'quantity':n,'accepted':'0','rejected':'0','inspected':False,'reference':ref,'date':iso(p.get('date')),'actor':u['id'],'document_id':p.get('document_id')};c['receipts'].append(r);c['status']='QUALITY'
        late=r['date']>c['po']['quote']['delivery_date'];r['late']=late
        explain(c,'Receipt review','Inspect receipt '+r['id']+(' · late against supplier commitment' if late else ''),r,'Received quantity must not exceed ordered quantity less accepted and pending receipts. Rejected quantities can be replaced. Receipt date is compared with the approved quotation delivery commitment.','Physical delivery is confirmed by the warehouse user, not inferred by the agent.')
    elif a=='quality':
        auth(u,{'quality'});state(c,{'QUALITY'})
        r=next((r for r in c['receipts'] if r['id']==p.get('receipt_id') and not r['inspected']),None)
        if not r:fail('Select an uninspected receipt')
        if r['actor']==u['id']:fail('Receiver cannot inspect own receipt',403)
        accepted=dec(p.get('accepted'));rejected=dec(r['quantity'])-accepted
        if rejected<0:fail('Accepted quantity exceeds receipt')
        if not reason:fail('Inspection findings are required')
        r.update(accepted=str(accepted),rejected=str(rejected),inspected=True,inspector=u['id'],findings=reason)
        s=summary(c);c['status']='QUALITY' if dec(s['pending']) else ('INVOICE' if dec(s['accepted'])==dec(c['quantity']) else 'RECEIVING')
        explain(c,'Quality review','Release '+str(accepted)+' '+c['unit']+'; reject '+str(rejected),r,'Only reviewer-confirmed accepted quantity is available for invoice matching. Rejected goods do not satisfy the purchase requirement.')
    elif a=='invoice':
        auth(u,{'buyer'});state(c,{'INVOICE','RECEIVING'})
        ref=required(p.get('reference'),'Supplier invoice reference')
        if any(i['reference'].casefold()==ref.casefold() for i in c['invoices']):fail('Duplicate supplier invoice reference',409)
        i={'id':uid('INV-'),'reference':ref,'quantity':quantity(p.get('quantity')),'unit_price':money(p.get('unit_price')),'total':money(p.get('total')),'currency':p.get('currency',c['currency']),'date':iso(p.get('date')),'due_date':iso(p.get('due_date')),'status':'UNMATCHED','submitter':u['id'],'document_id':p.get('document_id')};c['invoices'].append(i);c['status']='INVOICE_MATCHING'
    elif a=='match':
        auth(u,{'buyer','finance'});state(c,{'INVOICE_MATCHING','INVOICE_BLOCKED'})
        i=c['invoices'][-1];q=c['po']['quote'];s=summary(c);exceptions=[]
        # Allocate agreed freight/discount proportionally across partial invoices.
        proportion=dec(i['quantity'])/dec(c['quantity'])
        expected=money((dec(i['quantity'])*dec(q['unit_price'])+(dec(q['freight'])-dec(q['discount']))*proportion)*(1+dec(q['tax_rate'])/100))
        if dec(s['invoiced'])>dec(s['accepted']):exceptions.append('Invoiced quantity exceeds quality-accepted receipts')
        if dec(i['unit_price'])!=dec(q['unit_price']):exceptions.append('Unit price differs from approved PO')
        if i['currency']!=c['currency']:exceptions.append('Invoice currency differs from PO')
        tolerance=c.get('amount_tolerance','0.01')
        if abs(dec(i['total'])-dec(expected))>dec(tolerance):exceptions.append('Total differs from expected '+expected+' (tolerance '+tolerance+')')
        i['match']={'expected_total':expected,'exceptions':exceptions,'actor':u['id'],'created':now(),'tolerance':tolerance,'commercial_version':c['commercial_version']};i['status']='BLOCKED' if exceptions else 'MATCHED';c['status']='INVOICE_BLOCKED' if exceptions else 'MATCHED'
        explain(c,'Invoice matching','Blocked: '+ '; '.join(exceptions) if exceptions else 'Three-way match passed',{'po':c['po']['id'],'invoice':i,'accepted_receipts':s['accepted']},'Cumulative billed quantity ≤ accepted receipts; exact unit price/currency; total tolerance '+tolerance+'. Freight and discount allocated by quantity.','Invoice rounding may require an authorized correction; blocking exceptions cannot be overridden silently.')
    elif a=='invoice_correct':
        auth(u,{'buyer'});state(c,{'INVOICE_BLOCKED'})
        if not reason:fail('Correction reason required')
        i=c['invoices'][-1];i.setdefault('previous_versions',[]).append(copy.deepcopy({k:v for k,v in i.items() if k!='previous_versions'}))
        for k in ['unit_price','total']:i[k]=money(p.get(k,i[k]))
        i['quantity']=quantity(p.get('quantity',i['quantity']));i['currency']=p.get('currency',i['currency']);i['status']='UNMATCHED';i.pop('match',None);c['status']='INVOICE_MATCHING'
    elif a=='finance_submit':
        auth(u,{'buyer'});state(c,{'MATCHED'})
        i=c['invoices'][-1];i['finance_submitter']=u['id'];i['status']='FINANCE_REVIEW';c['status']='FINANCE_REVIEW'
        explain(c,'Payment preparation','Finance review required for '+i['total']+' '+c['currency'],{'invoice':i['id'],'match':i['match'],'due_date':i['due_date']},'Finance approval requires a current successful three-way match. Approval does not execute payment.')
    elif a=='finance_decision':
        auth(u,{'finance'});state(c,{'FINANCE_REVIEW'})
        i=c['invoices'][-1]
        if u['id'] in {i['submitter'],i['finance_submitter']}:fail('Cannot approve your own invoice submission',403)
        decision=p.get('decision')
        if decision not in {'approve','reject','changes'}:fail('Invalid decision')
        if decision!='approve' and not reason:fail('Provide a reason')
        if decision=='approve':i['approval']={'actor':u['id'],'created':now(),'version':c['commercial_version'],'total':i['total']};i['status']='APPROVED';c['status']='PAYMENT_PENDING'
        else:i['status']='BLOCKED';i['match']['exceptions']=['Finance returned invoice: '+reason];c['status']='INVOICE_BLOCKED'
    elif a=='payment_record':
        auth(u,{'finance'});state(c,{'PAYMENT_PENDING'})
        i=c['invoices'][-1]
        if not i.get('approval') or i['approval']['version']!=c['commercial_version']:fail('Current finance approval required')
        amount=dec(p.get('amount'),'0.01');balance=dec(i['total'])-sum((dec(x['amount']) for x in c['payments'] if x['invoice']==i['id']),Decimal(0))
        if amount>balance:fail('Payment exceeds approved invoice balance')
        ref=required(p.get('reference'),'External payment reference')
        if any(x['reference']==ref for x in c['payments']):fail('Duplicate payment reference',409)
        if not p.get('document_id'):fail('Upload bank/payment evidence before reconciliation')
        if not reason:fail('Reconciliation statement required')
        c['payments'].append({'id':uid('PAY-'),'invoice':i['id'],'amount':money(amount),'reference':ref,'date':iso(p.get('date')),'actor':u['id'],'document_id':p['document_id'],'status':'Externally recorded; human reconciled','reason':reason})
        if amount==balance:
            i['status']='PAID';s=summary(c)
            c['status']='COMPLETED' if dec(s['accepted'])==dec(c['quantity']) and dec(s['invoiced'])==dec(c['quantity']) and dec(s['balance'])==0 else ('RECEIVING' if dec(s['accepted'])<dec(c['quantity']) else 'INVOICE')
    elif a=='commercial_change':
        auth(u,{'buyer'});state(c,{'QUOTATIONS','SUPPLIER_REVIEW','SUPPLIER_APPROVED','PO_REVIEW','PO_APPROVED'})
        if c['receipts'] or c['invoices'] or c['payments'] or (c['po'] and c['po'].get('external_reference')):fail('Posted/externally issued records require an amendment workflow; direct editing is blocked',409)
        if not reason:fail('Change reason required')
        for k in ['quantity','deadline','min_quality']:
            if k in p:c[k]={'quantity':quantity,'deadline':iso,'min_quality':percent}[k](p[k])
        if 'amount_tolerance' in p:
            t=dec(p['amount_tolerance'])
            if t>1:fail('Amount tolerance is capped at 1 currency unit')
            c['amount_tolerance']=money(t)
        c['commercial_version']+=1;c['selected']=None;c['supplier_approval']=None;c['po']=None;c['status']='QUOTATIONS'
    elif a=='cancel':
        auth(u,{'buyer'});
        if c['receipts'] or c['invoices'] or c['payments'] or (c['po'] and c['po'].get('external_reference')):fail('Cannot cancel posted or issued records without controlled reversal',409)
        if c['status']=='CANCELLED':fail('Already cancelled',409)
        if not reason:fail('Cancellation reason required')
        c['status']='CANCELLED'
    else:fail('Unknown action')
    return c
