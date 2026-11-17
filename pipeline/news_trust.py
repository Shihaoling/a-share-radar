"""User-confirmed source trust, separate from the truth of individual claims."""
def apply_source_whitelist(dashboard):
    for n in dashboard.get('news',[]):
        if n.get('provider')=='stock_api' and n.get('source_id')=='stock_api:cls':
            n['source_verified']=True
            n['source_verification_basis']='user_confirmed_cls_whitelist_20261118'
            if n.get('credibility')!='rumor':n['credibility']='reliable'
            if n.get('verification') in (None,'unverified'):n['verification']='trusted_source'
    return dashboard
