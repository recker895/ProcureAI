"""Allocation-aware extension. Legacy/manual rules remain in domain.py."""
import copy, hashlib
from decimal import Decimal
import domain as d
import optimization as opt
import storage as db

def digest(value):return hashlib.sha256(db.dumps(value).encode()).hexdigest()
def approval_current(c):
    if not c.get('supplier_approval') or c['supplier_approval']['version']!=c['commercial_version']:d.fail('Current supplier approval required',409)

def action(c,a,p,u):
    if a in {'supplier_decision','po_decision','finance_decision'} and p.get('decision')=='override':
        reason=d.required(p.get('reason'),'Override reason');p={**p,'decision':'approve','override_reason':reason}
        if a=='supplier_decision' and c.get('ai_plan') and p.get('override_quote'):
            d.auth(u,{'procurement'});d.state(c,{'SUPPLIER_REVIEW'});c=copy.deepcopy(c);candidate=copy.deepcopy(c)
            candidate['optimization']={**opt.configuration(c.get('optimization',{})),'excluded_quotes':[q['id'] for q in c['quotes'] if q['id']!=p['override_quote']]};r=opt.solve(candidate)
            if not r['allocations']:d.fail('Override violates mandatory supplier constraints')
            c['recommendations'].append(r);c['ai_plan']=r;c['selected']=r['winner'] or r['id'];c['supplier_submission']['plan_hash']=digest(r);c['supplier_submission']['reviewer_override']=reason
    if a=='optimization_config':
        d.auth(u,{'buyer'});d.state(c,{'QUOTATIONS','SUPPLIER_REVIEW','SUPPLIER_APPROVED','PO_REVIEW','PO_APPROVED'})
        # Domain enforces issuance/receipt guards and invalidates affected approvals.
        c=d.action(c,'commercial_change',{'reason':d.required(p.get('reason'),'Configuration change reason')},u)
        old=c.get('optimization',opt.DEFAULT);cfg=opt.configuration(p.get('configuration',{}));cfg['version']=old['version']+1;c['optimization']=cfg;c.pop('ai_plan',None);return c
    if a=='optimize':
        d.auth(u,{'buyer'});d.state(c,{'QUOTATIONS'});c=copy.deepcopy(c);r=opt.solve(c)
        c['recommendations'].append(r);c['ai_plan']=r if r['allocations'] else None
        d.explain(c,'Optimization',r['explanation'],r['inputs'],r['rules'],r['uncertainties']);return c
    if a=='supplier_submit' and c.get('ai_plan'):
        d.auth(u,{'buyer'});d.state(c,{'QUOTATIONS'});c=copy.deepcopy(c);r=c['ai_plan']
        if r['commercial_version']!=c['commercial_version']:d.fail('Allocation is stale; recalculate',409)
        if not r['allocations']:d.fail('No feasible allocation')
        # Override to a single supplier is re-solved with all other suppliers excluded.
        qid=p.get('quote_id')
        if qid and qid not in {r['id'],r.get('winner')}:
            reason=d.required(p.get('reason'),'Override reason');candidate=copy.deepcopy(c)
            candidate['optimization']={**opt.configuration(c.get('optimization',{})),'excluded_quotes':[q['id'] for q in c['quotes'] if q['id']!=qid]}
            override=opt.solve(candidate)
            if not override['allocations']:d.fail('Override violates mandatory constraints')
            c['recommendations'].append(override);c['ai_plan']=r=override
        c['selected']=r.get('winner') or r['id'];c['supplier_submission']={'actor':u['id'],'version':c['commercial_version'],'reason':p.get('reason',r['explanation']),'plan_hash':digest(r),'plan_id':r['id']};c['status']='SUPPLIER_REVIEW';return c
    if a=='po_submit' and c.get('ai_plan'):
        d.auth(u,{'buyer'});d.state(c,{'SUPPLIER_APPROVED'});approval_current(c);c=copy.deepcopy(c);r=c['ai_plan']
        if c['supplier_submission']['plan_hash']!=digest(r):d.fail('Approved allocation changed',409)
        lines=[]
        for allocation in r['allocations']:
            q=next(q for q in c['quotes'] if q['id']==allocation['quote_id'])
            lines.append({**copy.deepcopy(allocation),'order_id':d.uid('PO-'),'quote':copy.deepcopy(q),'transmission':'Not connected','external_reference':None})
        orders=[{'id':line['order_id'],'supplier':line['supplier'],'quote_id':line['quote_id'],'material':c['material'],'quantity':line['quantity'],'unit':c['unit'],'currency':c['currency'],'unit_price':line['unit_price'],'total':line['total'],'freight':line['quote']['freight'],'discount':line['quote']['discount'],'tax_rate':line['quote']['tax_rate'],'delivery_date':line['quote']['delivery_date'],'payment_terms':line['quote']['payment_terms']} for line in lines]
        c['po']={'id':orders[0]['id'] if len(orders)==1 else d.uid('BUNDLE-'),'kind':'purchase_order' if len(orders)==1 else 'purchase_order_bundle','orders':orders,'quote':copy.deepcopy(lines[0]['quote']),'lines':lines,'quantity':c['quantity'],'total':r['total'],'currency':c['currency'],'commercial_version':c['commercial_version'],'submitter':u['id'],'created':d.now(),'approval':None,'transmission':'Not connected','external_reference':None,'plan_hash':digest(r)}
        c['status']='PO_REVIEW';d.explain(c,'Purchase order','Review allocation-based purchase order '+c['po']['id'],c['po'],'Each line copies the approved supplier allocation. Independent PO approval is required.');return c
    if a=='quote':
        prior=next((q for q in c['quotes'] if q['id']==p.get('id')), {})
        result=d.action(c,a,p,u);q=next(q for q in result['quotes'] if q['id']==p.get('id')) if p.get('id') else result['quotes'][-1]
        minimum=d.dec(p.get('minimum_order',prior.get('minimum_order','0')))
        if minimum.as_tuple().exponent < -3:d.fail('Minimum order supports at most 3 decimals')
        q['minimum_order']=str(minimum)
        q['supplier_email']=str(p.get('supplier_email',prior.get('supplier_email',''))).strip()[:254];q['payee_reference']=str(p.get('payee_reference',prior.get('payee_reference',''))).strip()[:200]
        q['source_origin']='Groq extraction' if u['id']=='agent:3' else 'Authenticated supplier event' if u['id']=='gateway:supplier' else 'Human-entered input'
        if p.get('field_sources'):q['field_sources']=copy.deepcopy(p['field_sources'])
        result.pop('ai_plan',None);return result
    if a=='rank':
        result=d.action(c,a,p,u);result.pop('ai_plan',None);return result
    if a=='commercial_change':
        result=d.action(c,a,p,u);result.pop('ai_plan',None);return result
    lines=c.get('po',{}).get('lines',[]) if c.get('po') else []
    if lines and a in {'receipt','invoice'}:
        qid=p.get('quote_id') or (lines[0]['quote_id'] if len(lines)==1 else None)
        line=next((x for x in lines if x['quote_id']==qid),None)
        if not line:d.fail('Select the supplier/order allocation line')
        if a=='receipt':
            used=sum((d.dec(r['accepted']) if r['inspected'] else d.dec(r['quantity']) for r in c['receipts'] if r.get('quote_id')==qid),Decimal(0))
            if used+d.dec(p.get('quantity'))>d.dec(line['quantity']):d.fail('Receipt exceeds this supplier allocation')
        result=d.action(c,a,p,u);record=result['receipts'][-1] if a=='receipt' else result['invoices'][-1];record.update(quote_id=qid,supplier=line['supplier'],order_id=line['order_id']);return result
    if lines and a=='match':
        invoice=c['invoices'][-1];line=next(x for x in lines if x['quote_id']==invoice['quote_id']);temp=copy.deepcopy(c)
        temp['quantity']=line['quantity'];temp['po']['quote']=line['quote']
        temp['receipts']=[r for r in temp['receipts'] if r.get('quote_id')==line['quote_id']]
        temp['invoices']=[i for i in temp['invoices'] if i.get('quote_id')==line['quote_id']]
        matched=d.action(temp,a,p,u);result=copy.deepcopy(c);result['invoices'][-1]=matched['invoices'][-1];result['status']=matched['status'];result['explanations']=matched['explanations'];return result
    if a in {'po_decision','receipt','invoice','match','finance_submit','finance_decision','payment_record'}:
        if c.get('po') and c['po']['commercial_version']!=c['commercial_version']:d.fail('PO terms are stale',409)
        if c.get('po') and c['po'].get('lines'):approval_current(c)
    result=d.action(c,a,p,u)
    if a=='supplier_decision' and result.get('supplier_approval') and result.get('ai_plan'):result['supplier_approval']['plan_hash']=digest(result['ai_plan'])
    if a=='po_decision' and result.get('po') and result['po'].get('approval'):result['po']['approval']['terms_hash']=digest({k:v for k,v in result['po'].items() if k not in {'approval','transmission','external_reference'}})
    if a=='finance_decision' and result['invoices'][-1].get('approval'):
        i=result['invoices'][-1];i['approval']['invoice_hash']=digest({k:i[k] for k in ['id','reference','quantity','unit_price','total','currency','date','due_date']})
        q=next((line['quote'] for line in lines if line['quote_id']==i.get('quote_id')),result['po']['quote'])
        i['approval']['payee_reference']=q.get('payee_reference','')
    if p.get('override_reason'):
        approval=result.get('supplier_approval') if a=='supplier_decision' else result.get('po',{}).get('approval') if a=='po_decision' else result['invoices'][-1].get('approval')
        if approval:approval['override_reason']=p['override_reason']
    return result
