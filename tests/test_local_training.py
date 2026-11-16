import json
from argparse import Namespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pipeline.local_training import choose_candidate, run_local
from pipeline.training_progress import ProgressWriter
from pipeline.train_equity import run_training
from tests.test_equity_model import make_history, write_history


class LocalTrainingTests(unittest.TestCase):
    def test_selection_rejects_test_metrics_and_uses_validation_only(self):
        report = {'confirmation': {}, 'test_metrics_accessed': False,
                  'models': {'gru': {'validation': {'mean_mae_pct': .4}},
                             'zero': {'validation': {'mean_mae_pct': .6}}}}
        self.assertEqual(choose_candidate([report])['model'], 'gru')
        with self.assertRaises(ValueError):
            choose_candidate([{**report, 'confirmation': {'gru': {'mean_mae_pct': .1}}}])

    def test_tuning_saves_selection_before_single_fixed_confirmation(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root)
            args = Namespace(output_dir=root, history_dir=root, device='cpu', batch_size=128,
                hidden=64, cpu_threads=2, tune=True, max_stocks=3, epochs=1)
            seen = []
            def training(config):
                seen.append(config)
                if config.validation_only:
                    score = .5 if config.trial == 1 else .3
                    return {'status': 'validation_only', 'confirmation': {}, 'test_metrics_accessed': False,
                            'models': {'tcn': {'validation': {'mean_mae_pct': score}},
                                       'zero': {'validation': {'mean_mae_pct': .7}}}}
                selection = json.loads((output / 'selection.json').read_text())
                self.assertEqual(selection['selected']['trial'], 2)
                self.assertEqual(config.confirm_model, 'tcn')
                return {'status': 'trained_candidates', 'eligible_for_promotion': False}
            with patch('pipeline.training_resources.profile_resources', return_value={
                    'device': 'cpu', 'batch_size': 128, 'cpu_threads': 2}), \
                    patch('pipeline.train_equity.run_training', side_effect=training):
                summary = run_local(args, ProgressWriter(output / 'progress.json'))
            self.assertEqual([x.validation_only for x in seen], [True, True, False])
            self.assertEqual(seen[-1].hidden, 64)
            self.assertEqual(seen[-1].learning_rate, 1e-4)
            self.assertFalse(summary['uploaded'])
            progress = json.loads((output / 'progress.json').read_text())
            self.assertEqual(progress['completed_trials'], 3)
            self.assertEqual(progress['status'], 'completed')

    def test_real_ridge_path_does_not_compute_test_metrics_while_tuning(self):
        # Generated fixtures verify execution, not predictive performance.
        with tempfile.TemporaryDirectory() as root:
            data = Path(root) / 'daily'
            write_history(data, make_history(count=2))
            args = Namespace(history_dir=str(data), model_dir=str(Path(root) / 'models'),
                seed=42, max_stocks=2, window=60, min_train_dates=60, validation_dates=20,
                test_dates=20, models='ridge', validation_only=True, progress_file=str(Path(root) / 'progress.json'))
            report = run_training(args)
            self.assertEqual(report['status'], 'validation_only')
            self.assertEqual(report['confirmation'], {})
            self.assertFalse(report['test_metrics_accessed'])
            self.assertTrue((Path(root) / 'progress.json').exists())


if __name__ == '__main__':
    unittest.main()
