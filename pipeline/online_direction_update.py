"""Bounded online classifier training on newly matured daily bars, then inference.

The dedicated RADAR_ALLOW_DIRECTION_TRAINING opt-in leaves legacy neural and
return-regression production training guards untouched. No network calls here.
"""
from argparse import ArgumentParser
from datetime import datetime,timezone,time
import json
import os
from pathlib import Path
from .calendar import shanghai_now
from .daily_forecast import clean_history,digest
from .direction_probability import update_direction_probabilities
from .run import atomic_json,load
from .publish import publish_view,health_summary

ROOT=Path(__file__).resolve().parents[1]


def training_decision(dashboard,state,request,now):
    entities=[e for g in ('market','sectors') for e in dashboard.get(g,[])]
    if not entities:return None,'no_entities'
    latest=[]
    for e in entities:
        rows=[r for r in clean_history(e,available_by=now) if r['date']<now.date().isoformat() or now.time().replace(tzinfo=None)>=time(15,30)]
        if not rows:return None,'missing_history'
        latest.append(rows[-1]['date'])
    cutoff=min(latest)
    if state.get('request_id')==request.get('request_id') and state.get('training_cutoff','')>=cutoff:
        return None,'already_trained_for_completed_data'
    return cutoff,'new_mature_data_or_explicit_request'


def run(root=None,now=None):
    root=Path(root) if root else ROOT;now=shanghai_now(now)
    if os.environ.get('RADAR_ALLOW_DIRECTION_TRAINING','false').lower() not in ('true','1'):
        raise ValueError('Online classifier training requires its dedicated explicit opt-in')
    cfg=load(root/'config/settings.json',{}).get('direction_probability',{})
    if not cfg.get('online_training_enabled',False):return {'status':'disabled'}
    path=root/'web/data/dashboard.json';d=load(path,{})
    if d.get('data_mode')=='demo':raise ValueError('Demonstration data cannot enter training')
    folder=root/'data/direction_probability';state=load(folder/'online-state.json',{});request=load(folder/'online-request.json',{})
    cutoff,reason=training_decision(d,state,request,now)
    if cutoff:
        from .train_direction_probability import run as train
        out=folder/'studies'/('cloud-'+now.strftime('%Y%m%dT%H%M%S'))
        report=train(path,out,folder/'models/bundle.json',threads=2,through=cutoff)
        lag_report=train(path,out.with_name(out.name+'-lag2'),folder/'models/bundle-lag2.json',threads=2,through=cutoff,lead_sessions=2)
        state={'status':'trained','lagged_model_id':lag_report['model_id'],'lagged_report_path':str(out.with_name(out.name+'-lag2').relative_to(root)/'training-report.json'),'request_id':request.get('request_id'),'training_cutoff':cutoff,'trained_at':shanghai_now().isoformat(),
            'model_id':report['model_id'],'report_path':str(out.relative_to(root)/'training-report.json'),
            'runtime':'GitHub Actions CPU' if os.environ.get('GITHUB_ACTIONS')=='true' else 'local_validation_cpu','source_sha256':report['plan']['source_sha256'],'news_training':report['plan']['sentiment_training'],
            'evaluation_status':'historical_reference_not_new_forward_accuracy'}
        atomic_json(folder/'online-state.json',state)
    else:
        print(json.dumps({'training_status':'skipped','reason':reason}),flush=True)
    d=update_direction_probabilities(d,folder/'models/bundle.json',shanghai_now(),previous=d)
    d['direction_probability']['online_training']=state
    atomic_json(path,d);publish_view(d,root/'web/data/view.json');atomic_json(root/'web/data/health.json',health_summary(d))
    return state


def main():
    print(json.dumps(run(),ensure_ascii=False),flush=True)

if __name__=='__main__':main()
