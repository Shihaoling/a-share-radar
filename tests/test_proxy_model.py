import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from pipeline.model import update_predictions

class ProxyModelTests(unittest.TestCase):
    def test_proxy_never_settles_against_sector_index(self):
        now=datetime(2026,11,16,16,tzinfo=ZoneInfo('Asia/Shanghai'))
        record={'id':'p1','sector_id':'semi:proxy:sh588000','issued_at':'2026-11-13T16:00:00+08:00','target_date':'2026-11-16','target_kind':'next_open','reference_price':1.,'predicted_return_pct':1.,'status':'ready'}
        data={'sectors':[{'id':'semi','name':'半导体','history':[{'date':'2026-11-16','open':4000,'close':4100}], 'proxy':{'code':'sh588000','name':'参考基金','history':[{'date':'2026-11-16','open':1.02,'close':1.03}]}}]}
        out=update_predictions(data,{'ledger':[record]},now=now)
        self.assertEqual(out['ledger'][0]['actual_price'],1.02)
    def test_conflicting_prices_abstain(self):
        data={'market':[{'id':'a','name':'指数','data_quality':'conflict','history':[]}]}
        out=update_predictions(data,now=datetime(2026,11,16,16,tzinfo=ZoneInfo('Asia/Shanghai')))
        self.assertIn('冲突',out['predictions'][0]['reason'])
if __name__=='__main__':unittest.main()
