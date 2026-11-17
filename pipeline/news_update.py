"""Cloud news enrichment entry point, also called by the ordinary collector."""
import json
from pathlib import Path
from .calendar import shanghai_now
from .news_content import enrich_full_text
from .minimax_news import enrich_minimax, infer_article, ModelFailure
from .sentiment import aggregate_entity_sentiment, _merge_stock_api_news


def enrich_news(dashboard, config, root=None):
    _merge_stock_api_news(dashboard, shanghai_now())
    dashboard = enrich_full_text(dashboard, config, root=Path(root)/'news_content' if root else None)
    dashboard = enrich_minimax(dashboard, config, root=Path(root)/'news_sentiment' if root else None)
    return aggregate_entity_sentiment(dashboard, now=shanghai_now(), config=config)


def main():
    from .run import ROOT, load, atomic_json
    from .news_archive import archive_news
    from .publish import publish_view
    cfg = load(ROOT/'config/settings.json', {}); cfg['network_enabled'] = True
    path = ROOT/'web/data/dashboard.json'
    d = enrich_news(load(path, {}), cfg)
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
    report = {'completed_at': now.isoformat(), 'request_id': 'fulltext-minimax-subscription-v2',
              'content': d.get('news_content_summary'), 'model': d.get('minimax_summary'), 'api_check': probe}
    atomic_json(ROOT/'data/news_sentiment/fulltext-report.json', report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__': main()
