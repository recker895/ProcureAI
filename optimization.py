"""Versioned HiGHS integer allocation; monetary verification uses Decimal."""
import copy, math
from decimal import Decimal
from datetime import date
import highspy
import numpy as np
import domain as d

VERSION='highs-allocation/1.0'
DEFAULT={'version':1,'objective':'landed_cost','budget':None,'max_suppliers':1,'max_share':'100','risk_penalty':'0','quality_penalty':'0','excluded_quotes':[]}

def configuration(p):
    cfg={**DEFAULT,**p}
    if cfg['objective'] not in {'landed_cost','cost_risk_quality'}:d.fail('Invalid optimization objective')
    if not isinstance(cfg['max_suppliers'],int) or not 1<=cfg['max_suppliers']<=4:d.fail('Maximum suppliers must be 1–4')
    cfg['max_share']=d.percent(cfg['max_share'])
    if d.dec(cfg['max_share'])==0:d.fail('Maximum supplier share must be positive')
    for k in ['risk_penalty','quality_penalty']:cfg[k]=d.money(cfg[k])
    if cfg['budget'] is not None:cfg['budget']=d.money(cfg['budget'])
    if not isinstance(cfg['excluded_quotes'],list) or len(cfg['excluded_quotes'])>4:d.fail('Invalid excluded quotation list')
    return cfg

def line_total(q,n):
    return d.money((d.dec(n)*d.dec(q['unit_price'])+d.dec(q['freight'])-d.dec(q['discount']))*(1+d.dec(q['tax_rate'])/100))

def solve(c, alternatives=True):
    cfg=configuration(c.get('optimization',{}))
    if not 3<=len(c['quotes'])<=4:d.fail('Provide quotations from 3–4 distinct suppliers')
    if len({q['supplier'].casefold() for q in c['quotes']})!=len(c['quotes']):d.fail('Suppliers must be distinct')
    inputs=copy.deepcopy({k:c[k] for k in ['quantity','currency','deadline','min_quality','certifications','quotes']});inputs['configuration']=cfg
    rows=[];eligible=[];Q=int(d.dec(c['quantity'])*1000)
    for q in c['quotes']:
        reasons=[]
        if q['currency']!=c['currency']:reasons.append('Currency differs; reviewed conversion required')
        if q['delivery_date']>c['deadline']:reasons.append('Delivery exceeds required date')
        if d.dec(q['quality'])<d.dec(c['min_quality']):reasons.append('Quality below minimum')
        if not set(c['certifications']).issubset(q['certifications']):reasons.append('Required certification missing')
        if q['id'] in cfg['excluded_quotes']:reasons.append('Excluded by reviewed configuration')
        if d.dec(q.get('minimum_order','0'))>d.dec(q['quantity']):reasons.append('Minimum order exceeds available capacity')
        rows.append({'quote_id':q['id'],'supplier':q['supplier'],'eligible':not reasons,'excluded_reasons':reasons,'capacity':q['quantity'],'allocated_quantity':'0','landed_cost':'0.00','score':None})
        if not reasons:eligible.append(q)
    result={'id':d.uid('OPT-'),'algorithm':VERSION,'solver':'HiGHS','solver_version':highspy.Highs().version(),'created':d.now(),'commercial_version':c['commercial_version'],'inputs':inputs,'configuration':cfg,'rows':rows,'allocations':[],'winner':None,'optimal':False,'alternatives':[],
            'rules':'Integer quantities in thousandths. Meet total demand; obey capacity, MOQ, supplier count, share, qualification, delivery, currency and budget. Landed cost includes freight, discount and tax. Optional risk/quality penalties are explicit currency amounts per point per unit.',
            'uncertainties':'Supplier ratings and capacity require source evidence. The optimum applies to this model and its input snapshot; it does not predict future supplier performance.'}
    if not eligible:
        result.update(solver_status='Infeasible',explanation='No eligible suppliers meet the mandatory constraints.');return result
    h=highspy.Highs();h.setOptionValue('output_flag',False);h.setOptionValue('time_limit',15.0);h.setOptionValue('random_seed',0);h.setOptionValue('threads',1);h.setOptionValue('mip_rel_gap',0.0)
    n=len(eligible);costs=[];fixed=[];capacities=[]
    for i,q in enumerate(eligible):
        tax=1+d.dec(q['tax_rate'])/100;cost=d.dec(q['unit_price'])*tax/1000;f=(d.dec(q['freight'])-d.dec(q['discount']))*tax
        costs.append(cost);fixed.append(f);cap=min(int(d.dec(q['quantity'])*1000),int(Decimal(Q)*d.dec(cfg['max_share'])/100));capacities.append(cap)
        h.addVar(0,cap);h.changeColIntegrality(i,highspy.HighsVarType.kInteger)
        penalty=(d.dec(q['risk'])*d.dec(cfg['risk_penalty'])+(100-d.dec(q['quality']))*d.dec(cfg['quality_penalty']))/1000 if cfg['objective']=='cost_risk_quality' else Decimal(0)
        h.changeColCost(i,float(cost+penalty))
    for i,f in enumerate(fixed):h.addVar(0,1);h.changeColIntegrality(n+i,highspy.HighsVarType.kInteger);h.changeColCost(n+i,float(f))
    def row(lo,hi,indices,values):h.addRow(float(lo),float(hi),len(indices),np.array(indices,dtype=np.int32),np.array(values,dtype=np.float64))
    row(Q,Q,list(range(n)),[1]*n);row(0,cfg['max_suppliers'],list(range(n,2*n)),[1]*n)
    for i,q in enumerate(eligible):
        row(-highspy.kHighsInf,0,[i,n+i],[1,-capacities[i]])
        minimum=max(1,int(d.dec(q.get('minimum_order','0'))*1000))
        row(0,highspy.kHighsInf,[i,n+i],[1,-minimum])
        # Discount cannot create a negative line total.
        row(0,highspy.kHighsInf,[i,n+i],[float(costs[i]),float(fixed[i])])
    if cfg['budget'] is not None:row(-highspy.kHighsInf,float(cfg['budget']),list(range(2*n)),[float(x) for x in costs+fixed])
    h.run();status=h.getModelStatus();result['solver_status']=h.modelStatusToString(status);solution=h.getSolution();info=h.getInfo()
    result['optimal']=status==highspy.HighsModelStatus.kOptimal
    result['mip_gap']=float(info.mip_gap) if math.isfinite(info.mip_gap) else None
    if not solution.value_valid:
        result['explanation']='No feasible allocation returned. Review constraints and supplier capacity.';return result
    for i,q in enumerate(eligible):
        units=int(round(solution.col_value[i]))
        if not units:continue
        qty=str(Decimal(units)/1000);total=line_total(q,qty)
        result['allocations'].append({'quote_id':q['id'],'supplier':q['supplier'],'quantity':qty,'unit_price':q['unit_price'],'total':total})
        r=next(x for x in rows if x['quote_id']==q['id']);r.update(allocated_quantity=qty,landed_cost=total)
    if sum(d.dec(x['quantity']) for x in result['allocations'])!=d.dec(c['quantity']):d.fail('Solver result failed quantity verification')
    total=sum(d.dec(x['total']) for x in result['allocations']);result['total']=d.money(total)
    if cfg['budget'] is not None and total>d.dec(cfg['budget']):d.fail('Rounded line totals exceed budget; revise the budget or rounding policy')
    result['objective_value']=str(info.objective_function_value)
    if len(result['allocations'])==1:result['winner']=result['allocations'][0]['quote_id']
    for i,r in enumerate(sorted(rows,key=lambda x:(-d.dec(x['allocated_quantity']),x['supplier'].casefold())),1):r['rank']=i
    result['explanation']=('Optimal' if result['optimal'] else 'Feasible; optimality not proven')+' allocation: '+', '.join(x['supplier']+' '+x['quantity']+' '+c['unit'] for x in result['allocations'])+'. Landed cost '+result['total']+' '+c['currency']+'.'
    if alternatives:
        for allocation in result['allocations']:
            alt=copy.deepcopy(c);alt['optimization']={**cfg,'excluded_quotes':list(set(cfg['excluded_quotes']+[allocation['quote_id']]))};r=solve(alt,False)
            result['alternatives'].append({'scenario':'Exclude '+allocation['supplier'],'status':r['solver_status'],'total':r.get('total'),'allocations':r['allocations'],'optimal':r['optimal']})
    return result
