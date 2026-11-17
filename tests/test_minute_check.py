import json,unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import datetime
from pipeline.minute_check import run,failure_details
from pipeline.stock_api import _Failure
from pipeline.primary_minutes import collect_primary_minutes

NOW=datetime.fromisoformat('2026-11-17T22:00:00+08:00')
class MinuteDiagnosticsTests(unittest.TestCase):
 def test_permission_and_unsupported_business_errors_are_distinct(self):
  self.assertEqual(failure_details(_Failure('api_denied',provider_code=-2001,provider_reason='unsupported_api'))['error_code'],'unsupported_api')
  self.assertEqual(failure_details(_Failure('api_denied',provider_code=-2001,provider_reason='permission_denied'))['error_code'],'permission_denied')
  self.assertEqual(failure_details(_Failure('api_denied'))['error_code'],'provider_error')
 def test_cloud_check_only_calls_historical_apis_once_each(self):
  class Client:
   def __init__(self): self.calls=[]
   def request(self,api,params):
    self.calls.append((api,params));raise _Failure('api_denied',provider_code=-2001,provider_reason='unsupported_api')
  c=Client()
  with TemporaryDirectory() as d:
   p=Path(d,'web/data');p.mkdir(parents=True);(p/'dashboard.json').write_text('{}')
   report=run(d,c,NOW);run(d,c,NOW)
   self.assertEqual([a for a,_ in c.calls],['sw_mins','idx_mins'])
   self.assertTrue(all(params['freq']=='5min' and params['start_date'].startswith('2026-11-16') for _,params in c.calls))
   self.assertTrue(all(r['kind']=='historical_minutes' and r['provider_reason']=='unsupported_api' for r in report['results']))
 def test_regular_minutes_preserve_actual_business_reason(self):
  class Client:
   def request(self,*a):raise _Failure('api_denied',86400,provider_code=-2001,provider_reason='invalid_parameters')
  result=collect_primary_minutes({'sectors':[{'id':'801780.SI','index_code':'801780.SI','taxonomy':'SW2021'}]},{'network_enabled':True},NOW,Client())
  endpoint=result['primary_minutes_state']['endpoints']['sw_mins']
  self.assertEqual(endpoint['error_code'],'invalid_parameters');self.assertEqual(endpoint['provider_code'],-2001)
