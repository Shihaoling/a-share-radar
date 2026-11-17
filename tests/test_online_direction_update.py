from datetime import datetime
import os
from unittest.mock import patch
import unittest
from pipeline.online_direction_update import training_decision,run

NOW=datetime.fromisoformat('2026-11-17T17:00:00+08:00')

class OnlineDirectionTests(unittest.TestCase):
    def fixture(self):
        bar={'date':'2026-11-16','open':100.,'high':102.,'low':99.,'close':101.,'complete':True}
        return {'market':[{'id':'a','history':[bar]}],'sectors':[{'id':'b','history':[bar]}]}

    def test_online_training_requires_a_separate_explicit_optin(self):
        with patch.dict(os.environ,{'RADAR_ALLOW_DIRECTION_TRAINING':'false'}):
            with self.assertRaisesRegex(ValueError,'opt-in'):run()

    def test_no_retraining_on_same_completed_data_every_half_hour(self):
        request={'request_id':'v1'};state={'request_id':'v1','training_cutoff':'2026-11-16'}
        self.assertEqual(training_decision(self.fixture(),state,request,NOW)[1],'already_trained_for_completed_data')
        self.assertEqual(training_decision(self.fixture(),{},request,NOW)[0],'2026-11-16')

    def test_partial_new_day_waits_for_all_objects_instead_of_using_incomplete_labels(self):
        d=self.fixture();d['market'][0]['history'].append({'date':'2026-11-17','open':101.,'high':103.,'low':100.,'close':102.,'complete':True})
        self.assertEqual(training_decision(d,{}, {'request_id':'v1'},NOW)[0],'2026-11-16')
        d['sectors'][0]['history']=d['market'][0]['history']
        self.assertEqual(training_decision(d,{}, {'request_id':'v1'},NOW)[0],'2026-11-17')

if __name__=='__main__':unittest.main()
