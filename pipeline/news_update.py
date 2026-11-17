"""Cloud news enrichment entry point, also called by the ordinary collector."""
import json
from pathlib import Path
from .calendar import shanghai_now
from .news_content import enrich_full_text
from .minimax_news import enrich_minimax, infer_article, ModelFailure
from .sentiment import aggregate_entity_sentiment, _merge_stock_api_news


def enrich_news(dashboard, config, root=None):
    from .primary_news import enrich_primary_news
    dashboard=enrich_primary_news(dashboard,config)
    _merge_stock_api_news(dashboard, shanghai_now())
    from .source_policy import apply_primary_news_policy
    dashboard=apply_primary_news_policy(dashboard)
    dashboard = enrich_full_text(dashboard, config, root=Path(root)/'news_content' if root else None)
    dashboard = enrich_minimax(dashboard, config, root=Path(root)/'news_sentiment' if root else None)
    dashboard = aggregate_entity_sentiment(dashboard, now=shanghai_now(), config=config)
    from .news_digest import enrich_digest
    return enrich_digest(dashboard, config, root=Path(root)/'news_sentiment' if root else None)


def main():
    from .run import ROOT, load, atomic_json
    from .news_archive import archive_news
    from .publish import publish_view
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--reuse-body-cache', action='store_true')
    parser.add_argument('--discover', action='store_true')
    parser.add_argument('--model-limit', type=int)
    parser.add_argument('--article-title')
    parser.add_argument('--model-budget-seconds', type=int)
    args = parser.parse_args()
    cfg = load(ROOT/'config/settings.json', {}); cfg['network_enabled'] = True
    if args.reuse_body_cache: cfg['news_content'] = {**cfg.get('news_content', {}), 'reuse_only': True}
    if args.article_title: cfg['minimax']['article_title_filter'] = args.article_title
    if args.model_limit is not None: cfg['minimax']['max_articles_per_run'] = max(0,min(16,args.model_limit))
    if args.model_budget_seconds is not None: cfg['minimax']['budget_seconds'] = max(0,min(600,args.model_budget_seconds))
    path = ROOT/'web/data/dashboard.json'
    d = load(path, {})
    if args.discover:
        from .sources import fetch_data
        discovered = fetch_data(d, {**cfg, 'akshare':{'enabled':False}, 'max_history_calls':0, 'max_constituent_calls':0}, shanghai_now())
        d['news'] = discovered.get('news', d.get('news',[]))
        d['sources'] = discovered.get('sources',d.get('sources',[]))
    d = enrich_news(d, cfg)
    # A small labelled synthetic case checks API availability only when there is
    # no fetched article to exercise the call. Never add this case to live news.
    probe = None
    if d.get('minimax_summary', {}).get('ready', 0) == 0 and not d.get('minimax_summary', {}).get('last_error'):
        import os
        key = os.environ.get('MINIMAX_API_KEY', '')
        if key:
            try:
                sample = {'title': '合成连通性测试', 'full_text': '某石油开采企业公告称原油销售价格上升，销售收入提高；同时某航空公司表示燃油成本上升，利润承压。该段为程序测试样本，并非真实资讯。'}
                checked = infer_article(sample, cfg.get('minimax', {}), key)
                probe = {'status': 'passed', 'synthetic': True, 'model': cfg.get('minimax', {}).get('model'), 'sector_count': len(checked['sector_impacts'])}
            except ModelFailure as e:
                probe = {'status': 'failed', 'error_code': e.code, 'synthetic': True}
    d['minimax_api_check'] = probe
    now = shanghai_now(); d['generated_at'] = now.isoformat()
    d['news_archive_summary'] = archive_news(d, now)
    atomic_json(path, d); publish_view(d, ROOT/'web/data/view.json')
    report = {'completed_at': now.isoformat(), 'request_id': 'news-library-digest-v1', 'primary_source_version': cfg.get('stock_api',{}).get('refresh_version'),
              'content': d.get('news_content_summary'), 'model': d.get('minimax_summary'), 'api_check': probe}
    atomic_json(ROOT/'data/news_sentiment/fulltext-report.json', report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__': main()
