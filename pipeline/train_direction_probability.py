"""Local, time-blocked probability training. Never called from production jobs."""
from argparse import ArgumentParser
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import math
import statistics
import time
import platform
import sys
import numpy as np
from .direction_probability import SCHEMA,NEWS_NAMES,probability_table,predict_probability,logit
from .daily_forecast import digest,canonical_bytes
from .train_daily_forecast import _prune_tree

CANDIDATES=[{'id':'historical_frequency','kind':'prior'},
 *[{'id':f'logistic_c{c}_{cal}','kind':'logistic','C':c,'calibration':cal,'blend':1.} for c in (.01,.1,1.) for cal in ('none','sigmoid')],
 *[{'id':f'lgb_leaves{leaves}_{cal}','kind':'lgb','leaves':leaves,'rounds':100,'calibration':cal,'blend':.5} for leaves in (3,7) for cal in ('none','sigmoid')],
 {'id':'logistic_recent120','kind':'logistic','C':.1,'window':120,'calibration':'sigmoid','blend':.75},
 {'id':'logistic_small6','kind':'logistic','C':.1,'small':True,'calibration':'sigmoid','blend':.75}]
# Matched price-only controls let actual PIT news compete on development Brier.
CANDIDATES += [{**c,'id':c['id']+'_price_only','use_news':False} for c in CANDIDATES if c['id'] in ('logistic_c0.1_none','logistic_c0.1_sigmoid','lgb_leaves3_none','lgb_leaves3_sigmoid','lgb_leaves7_none','lgb_leaves7_sigmoid')]


def write(path,value):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    t=p.with_suffix('.tmp');t.write_bytes(canonical_bytes(value)+b'\n');t.replace(p)


def prior_asset(rows):
    moving=[r for r in rows if r['direction']!=0]
    p=(sum(r['direction']==1 for r in moving)+1)/(len(moving)+2)
    return {'family':'prior','value':p,'by_id':{i:(sum(r['direction']==1 for r in moving if r['id']==i)+20*p)/(sum(r['id']==i for r in moving)+20) for i in sorted({r['id'] for r in rows})}}


def fit_base(spec,rows,names,threads):
    if spec['kind']=='prior':return prior_asset(rows)
    rows=[r for r in rows if r['direction']!=0]
    days=sorted({r['label_date'] for r in rows})
    if spec.get('window'):
        rows=[r for r in rows if r['label_date'] in set(days[-spec['window']:])]
    columns=list(range(len(names)))
    if spec.get('small'):
        keep={'return_1','overnight_1','intraday_1','range_1','close_location','momentum_5','momentum_20','volatility_20','market_return_1','sector_up_fraction'}
        columns=[i for i,n in enumerate(names) if n in keep or n.startswith('identity_')]
    if spec.get('use_news') is False:columns=[i for i in columns if names[i] not in NEWS_NAMES]
    x=np.array([[r['x'][i] for i in columns] for r in rows],float);y=np.array([r['direction']==1 for r in rows],int)
    if len(set(y))!=2:raise ValueError('Training needs both directions')
    if spec['kind']=='logistic':
        from sklearn.preprocessing import StandardScaler
        from sklearn.linear_model import LogisticRegression
        scaler=StandardScaler().fit(x)
        model=LogisticRegression(C=spec['C'],max_iter=1500,solver='lbfgs',random_state=1711).fit(scaler.transform(x),y)
        a={'family':'logistic','columns':columns,'coef':model.coef_[0].tolist(),'intercept':float(model.intercept_[0]),'mean':scaler.mean_.tolist(),'scale':scaler.scale_.tolist()}
        expected=model.predict_proba(scaler.transform(x[:30]))[:,1]
    else:
        import lightgbm as lgb
        model=lgb.LGBMClassifier(objective='binary',n_estimators=spec['rounds'],num_leaves=spec['leaves'],min_child_samples=60,
            learning_rate=.035,reg_lambda=30.,verbosity=-1,deterministic=True,force_col_wise=True,random_state=1711,n_jobs=threads)
        model.fit(x,y)
        a={'family':'lgb_binary','columns':columns,'trees':[_prune_tree(t['tree_structure']) for t in model.booster_.dump_model()['tree_info']]}
        expected=model.predict_proba(x[:30])[:,1]
    if not np.allclose(predict_probability(a,rows[:30]),expected,rtol=1e-8,atol=1e-8):
        raise ValueError('Portable probability export mismatch')
    return a


def fit(spec,rows,names,threads=4):
    if spec['kind']=='prior':return prior_asset(rows),{'fit_through':max(r['label_date'] for r in rows)}
    if spec['calibration']=='none':
        # Uncalibrated/shrunk candidates need no reserved calibration labels.
        # Fit all mature training dates, rather than silently discarding 20 days.
        base=fit_base(spec,rows,names,threads);prior=prior_asset(rows)
        return dict(family='calibrated',base=base,prior=prior,calibration='none',slope=1.,intercept=0.,blend=spec['blend']),{'base_fit_through':max(r['label_date'] for r in rows),'calibration':'not_requested','base_fit_dates':len({r['label_date'] for r in rows})}
    days=sorted({r['label_date'] for r in rows});cal_days=set(days[-20:]);cal=[r for r in rows if r['label_date'] in cal_days]
    cutoff=min(r['feature_date'] for r in cal)
    train=[r for r in rows if r['label_date']<cutoff]
    if len({r['label_date'] for r in train})<60:raise ValueError('Insufficient pre-calibration dates')
    base=fit_base(spec,train,names,threads);prior=prior_asset(train)
    slope,intercept=1.,0.
    if spec['calibration']=='sigmoid':
        from sklearn.linear_model import LogisticRegression
        cal=[r for r in cal if r['direction']!=0]
        p=predict_probability(base,cal);x=np.array([[logit(v)] for v in p]);y=[int(r['direction']==1) for r in cal]
        calibrator=LogisticRegression(C=.1,max_iter=500,random_state=1711).fit(x,y)
        slope=float(calibrator.coef_[0][0]);intercept=float(calibrator.intercept_[0])
    return dict(family='calibrated',base=base,prior=prior,calibration=spec['calibration'],slope=slope,intercept=intercept,blend=spec['blend']),{
        'base_fit_through':max(r['label_date'] for r in train),'calibration_start':min(cal_days),'calibration_end':max(cal_days),
        'base_fit_dates':len({r['label_date'] for r in train}),'calibration_dates':len(cal_days)}


def evaluate(rows,probs,bootstrap=True):
    from sklearn.metrics import roc_auc_score
    p=np.array(probs,float);y=np.array([r['direction']==1 for r in rows],int)
    if len(p)!=len(y) or not np.isfinite(p).all() or ((p<0)|(p>1)).any():raise ValueError('Invalid probabilities')
    by_day={}
    for i,r in enumerate(rows):by_day.setdefault(r['label_date'],[]).append(i)
    brier=np.array([float(np.mean((p[ix]-y[ix])**2)) for ix in by_day.values()])
    logloss=[float(np.mean(-(y[ix]*np.log(np.clip(p[ix],1e-7,1-1e-7))+(1-y[ix])*np.log(np.clip(1-p[ix],1e-7,1-1e-7))))) for ix in by_day.values()]
    acc=[float(np.mean((p[ix]>=.5)==y[ix])) for ix in by_day.values()]
    bins=[]
    for low,high in zip(np.linspace(0,1,11)[:-1],np.linspace(0,1,11)[1:]):
        mask=(p>=low)&(p<high if high<1 else p<=high)
        if mask.any():bins.append({'lower':float(low),'upper':float(high),'count':int(mask.sum()),'mean_probability':float(p[mask].mean()),'observed_up_frequency':float(y[mask].mean())})
    ece=sum(b['count']/len(rows)*abs(b['mean_probability']-b['observed_up_frequency']) for b in bins)
    high=np.abs(p-.5)>=.1
    output=dict(rows=len(rows),dates=len(by_day),first_date=min(by_day),last_date=max(by_day),brier=float(brier.mean()),log_loss=statistics.fmean(logloss),
        direction_accuracy=statistics.fmean(acc),auc=float(roc_auc_score(y,p)) if len(set(y))==2 else None,
        observed_up_frequency=float(y.mean()),flat_count=sum(r['direction']==0 for r in rows),ece=float(ece),reliability=bins,
        probability_min=float(p.min()),probability_max=float(p.max()),probability_std=float(p.std()),
        conviction_60pct={'coverage':float(high.mean()),'count':int(high.sum()),'accuracy':float(((p[high]>=.5)==y[high]).mean()) if high.any() else None})
    output['by_id']={identity:evaluate([r for r in rows if r['id']==identity],[prob for r,prob in zip(rows,probs) if r['id']==identity],False) for identity in sorted({r['id'] for r in rows})} if bootstrap else {}
    return output


def paired_brier_interval(rows,p,base):
    days=sorted({r['label_date'] for r in rows});diff=[]
    for day in days:
        ix=[i for i,r in enumerate(rows) if r['label_date']==day]
        diff.append(statistics.fmean((p[i]-int(rows[i]['direction']==1))**2-(base[i]-int(rows[i]['direction']==1))**2 for i in ix))
    # Moving-block bootstrap resamples entire market dates, preserving cross-sector dependence.
    rng=np.random.default_rng(1711);n=len(days);means=[]
    for _ in range(1000):
        indices=[]
        while len(indices)<n:
            start=int(rng.integers(0,max(1,n-4)));indices.extend(range(start,min(n,start+5)))
        means.append(float(np.mean([diff[i] for i in indices[:n]])))
    return {'mean_model_minus_baseline':statistics.fmean(diff),'block_days':5,'replicates':1000,
            'ci95':[float(x) for x in np.quantile(means,[.025,.975])],'independent_dates':n}


def run(source,output,model_output,threads=4,through=None,lead_sessions=1):
    start=time.monotonic();out=Path(output);out.mkdir(parents=True,exist_ok=True)
    if (out/'study-plan.json').exists():raise ValueError('Experiment already exists; preserve locked results')
    raw=Path(source).read_bytes();d=json.loads(raw);table=probability_table(d,through=through,lead_sessions=lead_sessions)
    plan={'schema_version':SCHEMA,'started_at':datetime.now(timezone.utc).isoformat(),'source_sha256':hashlib.sha256(raw).hexdigest(),
        'target':'target day close > / < / = previous trading day close; classify up conditional on nonflat, empirical flat rate','prediction_lead_sessions':lead_sessions,'price_feature_delay_sessions':lead_sessions-1,
        'selection_metric':'equal-date Brier; ties log loss; compare entity-shrunk training frequency',
        'calibration':'Sigmoid candidates: reserve last20 mature dates, purge earlier fit before first calibration feature date; no post-calibration base refit. None candidates: fit all mature training dates.',
        'validation':'3 consecutive 15-date development blocks, then one predeclared final20 historical dates',
        'final_data_status':'Dates were previously inspected for regression research and V1 classification. V2 corrects unused calibration reservation for none candidates; all choices still use development Brier only. Repeated historical reference, not a pristine unseen-market holdout.',
        'uncertainty':'paired 5-day moving-block bootstrap over entire dates; never treat 31 same-day sectors as independent market days',
        'candidates':CANDIDATES,'training_cutoffs':{},'feature_names':table['feature_names'],'entity_ids':table['entity_ids'],
        'sentiment_gate':'>=30 distinct mature labeled dates with PIT-scored news; otherwise freeze six news features to missing, never inject today into the past',
        'deployment_selection':'best development candidate, including prior if it wins; final metrics never choose model',
        'implementation_sha256':{n:hashlib.sha256((Path(__file__).parent/n).read_bytes()).hexdigest() for n in ('direction_probability.py','train_direction_probability.py')},
        'threads':threads,'through':through}
    groups={};splits={}
    for group in ('market','sectors'):
        rows=[r for r in table[group] if 'direction' in r]
        last_by_id={i:max(r['label_date'] for r in rows if r['id']==i) for i in {r['id'] for r in rows}}
        through=min(last_by_id.values());rows=[r for r in rows if r['label_date']<=through]
        days=sorted({r['label_date'] for r in rows})
        if len(days)<160:raise ValueError('Need >=160 labeled dates')
        groups[group]=rows;splits[group]={'final':days[-20:],'folds':[days[-65+i*15:-65+(i+1)*15] if i<2 else days[-35:-20] for i in range(3)]}
        plan['training_cutoffs'][group]={'through':through,'dates':len(days),'rows':len(rows),'flat_count':sum(r['direction']==0 for r in rows)}
    news_days={r['label_date'] for rows in groups.values() for r in rows if r['news'][-2]}
    group_news_dates={g:len({r['label_date'] for r in rows if r['news'][-2]}) for g,rows in groups.items()}
    sentiment_enabled=any(n>=30 for n in group_news_dates.values())
    for g,rows in groups.items():
        if group_news_dates[g]<30:
            for r in rows:r['x'][-len(NEWS_NAMES):]=[0.,0.,0.,0.,0.,1.]
    import sklearn,lightgbm
    plan['versions']={'numpy':np.__version__,'sklearn':sklearn.__version__,'lightgbm':lightgbm.__version__,'python':sys.version,'platform':platform.platform(),'runtime':'online_or_local_recorded_by_caller'}
    plan['splits']=splits;plan['sentiment_training']={'enabled':sentiment_enabled,'mature_dates_with_news':len(news_days),'group_mature_dates':group_news_dates,'required_dates':30,
        'status':'enabled' if sentiment_enabled else 'insufficient_point_in_time_history','news_backfilled':False}
    write(out/'study-plan.json',plan)
    study={};selection={};assets={};evaluations={};predictions={};priors={};flat_rates={}
    for group in ('market','sectors'):
        rows=groups[group];ranked=[];saved={}
        for spec in CANDIDATES:
            oof_rows=[];oof_probs=[];boundaries=[]
            for valid_dates in splits[group]['folds']:
                test=[r for r in rows if r['label_date'] in set(valid_dates)]
                first_feature=min(r['feature_date'] for r in test)
                train=[r for r in rows if r['label_date']<=first_feature]
                asset,boundary=fit(spec,train,table['feature_names'],threads)
                if max(r['label_date'] for r in train)>=min(valid_dates):raise ValueError('Training label leakage')
                probs=predict_probability(asset,test);oof_rows+=test;oof_probs+=probs;boundaries.append(boundary)
            e=evaluate(oof_rows,oof_probs)
            ranked.append({'candidate':spec['id'],'brier':e['brier'],'log_loss':e['log_loss'],'direction_accuracy':e['direction_accuracy'],'auc':e['auc']})
            saved[spec['id']]={'rows':oof_rows,'probabilities':oof_probs,'evaluation':e,'boundaries':boundaries}
            print(json.dumps({'group':group,'candidate':spec['id'],**{k:e[k] for k in ['brier','log_loss','direction_accuracy','auc']} }),flush=True)
        ranked.sort(key=lambda x:(x['brier'],x['log_loss']))
        chosen=next(c for c in CANDIDATES if c['id']==ranked[0]['candidate'])
        # Choice is written before any final holdout labels are evaluated.
        selection[group]={'candidate':chosen['id'],'spec':chosen,'development':saved[chosen['id']]['evaluation'],'leaderboard':ranked}
        write(out/'selection.json',selection)
        test=[r for r in rows if r['label_date'] in set(splits[group]['final'])]
        train=[r for r in rows if r['label_date']<=min(r['feature_date'] for r in test)]
        asset,boundary=fit(chosen,train,table['feature_names'],threads)
        baseline=prior_asset(train);p=predict_probability(asset,test);bp=predict_probability(baseline,test)
        e=evaluate(test,p);be=evaluate(test,bp)
        e['baseline']=be;e['brier_skill']=1-e['brier']/be['brier'];e['paired_brier_difference']=paired_brier_interval(test,p,bp);e['fit_boundary']=boundary
        e['beats_frequency_on_final']=e['brier']<be['brier'];e['statistically_supported']=e['paired_brier_difference']['ci95'][1]<0
        evaluations[group]=e
        predictions[group]=[{'id':r['id'],'name':r['name'],'feature_date':r['feature_date'],'target_date':r['label_date'],'actual_direction':r['direction'],'up_probability':p0,'baseline_up_probability':b0} for r,p0,b0 in zip(test,p,bp)]
        # Fixed-choice deployment fit uses all mature labels; final set is not used to reselect.
        assets[group],deploy_boundary=fit(chosen,rows,table['feature_names'],threads)
        priors[group]=prior_asset(rows);selection[group]['deployment_fit_boundary']=deploy_boundary
        for identity in {r['id'] for r in rows}:
            subset=[r for r in rows if r['id']==identity];flat_rates[identity]=sum(r['direction']==0 for r in subset)/len(subset)
        study[group]={'development_leaderboard':ranked,'final':e}
        write(out/'holdout-predictions.json',predictions)
    trained=datetime.now(timezone.utc).isoformat();stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    bundle={'schema_version':SCHEMA,'model_id':'direction-'+out.name+'-'+stamp,'trained_at':trained,
        'training_cutoff':max(v['through'] for v in plan['training_cutoffs'].values()),'target':'next_close_direction','prediction_lead_sessions':lead_sessions,
        'feature_names':table['feature_names'],'entity_ids':table['entity_ids'],'models':{'market':assets['market'],'sector':assets['sectors']},
        'priors':{'market':priors['market'],'sector':priors['sectors']},'flat_rates':flat_rates,
        'selection':selection,'evaluation':{'market':evaluations['market'],'sector':evaluations['sectors']},
        'sentiment_training':{**plan['sentiment_training'],'by_group':{'market':group_news_dates['market']>=30 and selection['market']['spec'].get('use_news',True),'sector':group_news_dates['sectors']>=30 and selection['sectors']['spec'].get('use_news',True)}},'source_sha256':plan['source_sha256'],'study_plan_sha256':digest(plan)}
    bundle['sentiment_training']['enabled']=any(bundle['sentiment_training']['by_group'].values())
    bundle['sha256']=digest(bundle);write(model_output,bundle)
    report={'plan':plan,'results':study,'model_id':bundle['model_id'],'bundle_sha256':bundle['sha256'],
        'elapsed_seconds':time.monotonic()-start,'actual_training':True,'final_set_used_for_selection':False,'fit_count':len(CANDIDATES)*3*2+4}
    write(out/'training-report.json',report)
    print(json.dumps({'model_id':bundle['model_id'],'elapsed_seconds':report['elapsed_seconds'],'results':{g:{k:study[g]['final'][k] for k in ['brier','direction_accuracy','auc','brier_skill']} for g in study}}),flush=True)
    return report


def main():
    p=ArgumentParser(description=__doc__);p.add_argument('--source',default='web/data/dashboard.json');p.add_argument('--output',required=True);p.add_argument('--model-output',default='data/direction_probability/models/bundle.json');p.add_argument('--threads',type=int,default=4);a=p.parse_args();run(a.source,a.output,a.model_output,a.threads)

if __name__=='__main__':main()
