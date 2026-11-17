"""Inference-only calibrated next-close direction probabilities and frozen issues.

Training and model selection live in train_direction_probability. No fit or
network code is imported here. Today's close is never guessed from a daily model.
"""
from copy import deepcopy
from datetime import datetime, time
from pathlib import Path
import json
import math
import statistics
from .calendar import SHANGHAI, shanghai_now, next_trading_day
from .daily_forecast import feature_table, clean_history, canonical_bytes, digest, _tree_value

SCHEMA = 'close-direction-probability-v1'
NEWS_NAMES = ['news_tone','news_log_count','news_log_sources','news_age_hours','news_known','news_missing']
ROOT = Path(__file__).resolve().parents[1]


def news_features(entity, cutoff):
    eligible=[]
    for r in entity.get('sentiment_history', []):
        if r.get('window_policy_version') != 'published-and-observed-v1':
            continue
        try:
            at=datetime.fromisoformat(r['available_at'])
            if at.tzinfo is None or at>cutoff or at.astimezone(SHANGHAI).date()!=cutoff.astimezone(SHANGHAI).date():
                continue
        except (KeyError,TypeError,ValueError):
            continue
        if not isinstance(r.get('score'),(int,float)) or not math.isfinite(r['score']) or not r.get('scored_article_count'):
            continue
        eligible.append((at,r))
    if not eligible:
        return [0.,0.,0.,0.,0.,1.]
    at,r=max(eligible,key=lambda x:x[0])
    return [float(r['score']),math.log1p(r['scored_article_count']),math.log1p(r.get('source_count',0)),
            (cutoff-at).total_seconds()/3600,1.,0.]


def probability_table(dashboard, through=None, entity_ids=None):
    table=feature_table(dashboard,through=through,entity_ids=entity_ids)
    entities={e['id']:e for group in ('market','sectors') for e in dashboard.get(group,[])}
    for group in ('market','sectors'):
        for r in table[group]:
            # Fixed historical decision clock: 18:30 Shanghai after a completed bar.
            cutoff=datetime.fromisoformat(r['feature_date']+'T18:30:00+08:00')
            r['news']=news_features(entities[r['id']],cutoff)
            r['x']=r['x']+r['news']
            if 'y' in r:
                r['direction']=1 if r['y']>0 else -1 if r['y']<0 else 0
    table['feature_names']+=NEWS_NAMES
    return table


def sigmoid(x):
    return 1/(1+math.exp(-max(-35.,min(35.,float(x)))))


def logit(p):
    p=max(1e-7,min(1-1e-7,float(p)))
    return math.log(p/(1-p))


def predict_probability(asset, rows):
    family=asset['family'];result=[]
    if family=='blend':
        parts=[predict_probability(a,rows) for a in asset['parts']]
        return [sum(w*part[i] for w,part in zip(asset['weights'],parts)) for i in range(len(rows))]
    if family=='calibrated':
        raw=predict_probability(asset['base'],rows)
        base=predict_probability(asset['prior'],rows)
        for p,b in zip(raw,base):
            q=sigmoid(asset['slope']*logit(p)+asset['intercept']) if asset['calibration']=='sigmoid' else p
            result.append(asset['blend']*q+(1-asset['blend'])*b)
        return result
    if family=='prior':
        return [asset['by_id'].get(r['id'],asset['value']) for r in rows]
    for r in rows:
        x=[float(r['x'][c]) for c in asset['columns']]
        if not all(math.isfinite(v) for v in x):
            raise ValueError('Nonfinite input')
        if family=='logistic':
            score=asset['intercept']+sum(c*(v-m)/s for c,v,m,s in zip(asset['coef'],x,asset['mean'],asset['scale']))
        elif family=='lgb_binary':
            score=sum(_tree_value(tree,x) for tree in asset['trees'])
        else:
            raise ValueError('Unknown probability model family')
        result.append(sigmoid(score))
    return result


def load_probability_bundle(path):
    b=json.loads(Path(path).read_text())
    payload={k:v for k,v in b.items() if k!='sha256'}
    if b.get('schema_version')!=SCHEMA or digest(payload)!=b.get('sha256'):
        raise ValueError('Probability bundle integrity failure')
    if b.get('target')!='next_close_direction' or not b.get('training_cutoff'):
        raise ValueError('Probability target/cutoff missing')
    return b


def update_direction_probabilities(dashboard, model_path=None, now=None, previous=None):
    result=deepcopy(dashboard);now=shanghai_now(now)
    issue_slot=now.replace(minute=(now.minute//30)*30,second=0,microsecond=0).isoformat()
    old=(previous or dashboard).get('direction_probability',{})
    ledger=deepcopy(old.get('ledger',[]))
    state=dict(schema_version=SCHEMA,status='pending',target='next_close_direction',market=[],sectors=[],ledger=ledger,
        update_policy='half_hour_immutable_revisions',today_close_status='not_fitted',today_close_reason='日线模型不用于盘中收盘预测；需要独立分钟样本与回测')
    result['direction_probability']=state
    try:
        bundle=load_probability_bundle(model_path or ROOT/'data/direction_probability/models/bundle.json')
        trained=datetime.fromisoformat(bundle['trained_at'])
        if trained.tzinfo is None or trained>now:
            raise ValueError('Model trained after issue time')
        if old.get('online_training',{}).get('model_id')==bundle['model_id']:
            state['online_training']=deepcopy(old['online_training'])
        state.update(model_id=bundle['model_id'],trained_at=bundle['trained_at'],training_cutoff=bundle['training_cutoff'],
            evaluation=bundle['evaluation'],selection=bundle['selection'],sentiment_training=bundle['sentiment_training'],
            target_definition='目标交易日收盘与参考交易日收盘相比；上涨/下跌/持平',information_cutoff='完整日线与预测发布时已知新闻')
        entities={e['id']:e for g in ('market','sectors') for e in dashboard.get(g,[])}
        for record in ledger:
            if record.get('settled_at'):
                continue
            bars=clean_history(entities.get(record['id'],{}),available_by=now)
            bar=next((b for b in bars if b['date']==record['target_date'] and (b['date']<now.date().isoformat() or now.time().replace(tzinfo=None)>=time(15,30))),None)
            if bar:
                actual=1 if bar['close']>record['reference_close'] else -1 if bar['close']<record['reference_close'] else 0
                p=record['up_probability'];actual_up=float(actual==1)
                record.update(actual_direction=actual,actual_close=bar['close'],brier=(p-actual_up)**2,
                    direction_correct=(1 if p>record['down_probability'] else -1 if p<record['down_probability'] else 0)==actual,
                    settled_at=now.isoformat())
        usable=deepcopy(dashboard)
        for g in ('market','sectors'):
            for entity in usable.get(g,[]):
                entity['history']=[b for b in clean_history(entity,available_by=now) if b['date']<now.date().isoformat() or now.time().replace(tzinfo=None)>=time(15,30)]
        table=probability_table(usable,entity_ids=bundle['entity_ids'])
        latest={}
        for g in ('market','sectors'):
            for r in table[g]:
                latest[r['id']]=r
        stale=[]
        for g,key in (('market','market'),('sectors','sector')):
            use_news=bundle['sentiment_training'].get('by_group',{}).get(key,bundle['sentiment_training']['enabled'])
            rows=[]
            for e in usable.get(g,[]):
                r=latest.get(e['id'])
                if not r:
                    continue
                target=next_trading_day(r['feature_date'])
                if target is None or target<now.date() or target==now.date() and now.time().replace(tzinfo=None)>=time(15,0) or r['feature_date']<bundle['training_cutoff']:
                    stale.append({'id':e['id'],'name':e.get('name'), 'reference_date':r['feature_date'],'reason':'等待最近交易日完整收盘日线'})
                    continue
                # A price-bar delay must not attach unavailable future news to the old date.
                r['x'][-len(NEWS_NAMES):]=news_features(e,now) if use_news else [0.,0.,0.,0.,0.,1.]
                rows.append(r)
            probabilities=predict_probability(bundle['models'][key],rows)
            prior=predict_probability(bundle['priors'][key],rows)
            for r,p,baseline in zip(rows,probabilities,prior):
                if not math.isfinite(p) or not 0<=p<=1:
                    raise ValueError('Probability outside [0,1]')
                target=next_trading_day(r['feature_date']).isoformat()
                flat=bundle['flat_rates'].get(r['id'],0.)
                p_up=(1-flat)*p;p_down=(1-flat)*(1-p)
                item=dict(id=r['id'],name=r['name'],target_date=target,reference_date=r['feature_date'],reference_close=r['reference_close'],
                    up_probability=p_up,down_probability=p_down,flat_probability=flat,baseline_up_probability=baseline,
                    model_id=bundle['model_id'],issued_at=now.isoformat(),issue_slot=issue_slot,status='research',
                    sentiment_used=use_news,news_available=bool(r['x'][-2]),
                    sentiment_note='情绪特征参与模型' if use_news else '情绪尚未通过对照检验或历史不足，当前价格概率不受新闻分值调整')
                frozen=next((x for x in ledger if x['id']==item['id'] and x['target_date']==target and x['model_id']==item['model_id'] and x.get('issue_slot')==issue_slot),None)
                if frozen:item=deepcopy(frozen)
                else:ledger.append(deepcopy(item))
                state[g].append(item)
        settled=[x for x in ledger if x.get('settled_at') and x.get('model_id')==bundle['model_id']]
        unique={}
        for record in sorted(settled,key=lambda r:r['issued_at']):
            unique.setdefault((record['id'],record['target_date']),record)
        cohort=list(unique.values())
        state.update(status='ready' if len(state['market'])==len(usable.get('market',[])) and len(state['sectors'])==len(usable.get('sectors',[])) else 'partial',
            stale_entities=stale,forward_evaluation={'count':len(cohort),'revision_count':len(settled),'target_dates':len({x['target_date'] for x in cohort}),'cohort':'每标的每目标日第一版，半小时修订不作为独立命中样本','brier':statistics.fmean(x['brier'] for x in cohort) if cohort else None,
                'direction_accuracy':statistics.fmean(x['direction_correct'] for x in cohort) if cohort else None})
    except FileNotFoundError:
        state['reason']='等待本机完成概率分类训练'
    except (ValueError,KeyError,TypeError,OverflowError,IndexError) as exc:
        state.update(status='error',reason='概率模型校验失败：'+type(exc).__name__,market=[],sectors=[])
    return result
