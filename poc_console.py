"""Single-user test reviews; never authorize operational actions."""
import domain as d

ROLES={'supplier_decision':'procurement','po_decision':'procurement',
       'finance_decision':'finance','receipt':'warehouse','quality':'quality',
       'test_payment':'finance'}

def reviewer(data, action, user, enabled):
    if not enabled:return user
    if data.get('mode')!='ai' or data.get('record_kind')!='test':
        d.fail('The POC console is restricted to AI Test cases',403)
    if user['role']!='buyer' or user['id']!=data['creator']:
        d.fail('Only the buyer who created this Test case can use its POC console',403)
    if action not in ROLES:return user
    # Explicit test persona, not another authenticated employee or independent review.
    return {**user,'id':'poc:'+user['id']+':'+ROLES[action],'role':ROLES[action],
            'performed_by':user['id'],'attribution':'Single-user POC review'}

def record(data,action,user):
    data.setdefault('poc_reviews',[]).append({'action':action,'performed_by':user['id'],
        'stage_role':ROLES.get(action,user['role']),'created':d.now(),
        'attribution':'Single-user POC exercise; not independent operational approval'})
