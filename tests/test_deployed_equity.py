import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from pipeline import deployed_equity as deployed


def metrics(mae):
    return {name: {'mae_pct': mae} for name in deployed.TARGETS}


class DeployedEquityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run = self.root / 'run'
        self.final = self.run / 'final'
        self.final.mkdir(parents=True)
        self.destination = self.root / 'export'

    def write(self, path, value):
        path.write_text(json.dumps(value), encoding='utf-8')

    def fixture(self, kind='gru'):
        self.report = {'eligible_for_promotion': True, 'promoted': False,
            'confirmation_selection_fixed': True, 'test_metrics_accessed': True,
            'selected_by_validation': kind, 'target_names': deployed.TARGETS,
            'target_unit': 'percent', 'created_at': '2026-11-16T10:00:00+00:00',
            'split': {'test': {'start': '2026-10-20', 'end': '2026-11-13'}},
            'models': {'zero': {'validation': metrics(1.)}, 'ridge': {'validation': metrics(.9)}},
            'confirmation': {'zero': metrics(1.2), 'ridge': metrics(1.)},
            'parameters': {'history_dir': '/private/history', 'model_dir': '/private/models',
                           'progress_file': '/private/progress.json', 'hidden': 64}}
        self.summary = {'status': 'completed', 'uploaded': False, 'execution': 'local',
            'eligible_for_promotion': True, 'selected': {'model': kind},
            'completed_at': '2026-11-16T11:00:00+00:00'}
        for name in ('gru', 'tcn'):
            checkpoint = self.final / f'{name}_candidate.pt'
            checkpoint.write_bytes((name + ' fake checkpoint').encode())
            self.report['models'][name] = {'validation': metrics(.8), 'checkpoint': str(checkpoint)}
            self.report['confirmation'][name] = metrics(.85)
        if kind == 'ensemble':
            manifest = {'format_version': 'equity-ensemble-v1', 'kind': 'ensemble',
                'target_names': deployed.TARGETS, 'target_unit': 'percent', 'feature_count': 8,
                'window': 60, 'components': {'gru': 'gru_candidate.pt', 'tcn': 'tcn_candidate.pt'},
                'weights': {'gru': .25, 'tcn': .75}}
            self.write(self.final / 'blend.json', manifest)
            self.report['models'][kind] = {'validation': metrics(.7),
                'checkpoint': str(self.final / 'blend.json'), 'weights': manifest['weights']}
            self.report['confirmation'][kind] = metrics(.8)
        self.save_fixture()
        self.bundle = {'kind': kind, 'window': 60, 'target_names': deployed.TARGETS,
            'mean': np.zeros(8), 'scale': np.ones(8), 'weights': {'gru': .25, 'tcn': .75},
            'metadata': {'validation': self.report['models'][kind]['validation'],
                         'split': self.report['split']}}
        return self.bundle

    def save_fixture(self):
        self.write(self.run / 'local-summary.json', self.summary)
        self.write(self.final / 'training_report.json', self.report)

    def export(self, kind='gru'):
        self.fixture(kind)
        with patch('pipeline.train_equity.load_neural_bundle', return_value=self.bundle), \
             patch('pipeline.train_equity.load_ensemble_bundle', return_value=self.bundle):
            return deployed.export_local_model(self.run, self.destination)

    def test_absent_manifest_is_not_a_deployment(self):
        self.assertIsNone(deployed.load_deployed_model(self.root / 'absent'))

    def test_export_and_load_require_cpu_and_strip_local_paths(self):
        manifest = self.export()
        self.assertEqual(manifest['source'], 'local')
        self.assertEqual(manifest['label_cutoff'], '2026-11-16')
        self.assertEqual(manifest['label_cutoff_basis'], 'training_completion_date_conservative')
        saved = json.loads((self.destination / 'validation-report.json').read_text())
        self.assertNotIn('history_dir', saved['parameters'])
        self.assertNotIn('checkpoint', saved['models']['gru'])
        with patch('pipeline.train_equity.load_neural_bundle', return_value=self.bundle) as loader:
            loaded = deployed.load_deployed_model(self.destination)
        loader.assert_called_once_with(self.destination / 'gru.pt', device='cpu')
        self.assertTrue(loaded['model_id'].startswith('gru-'))

    def test_hash_tamper_fails_before_torch_loading(self):
        self.export()
        (self.destination / 'gru.pt').write_bytes(b'changed')
        with patch('pipeline.train_equity.load_neural_bundle') as loader:
            with self.assertRaisesRegex(ValueError, 'checksum'):
                deployed.load_deployed_model(self.destination)
        loader.assert_not_called()

    def test_missing_gate_or_repeated_test_selection_rejected(self):
        self.fixture()
        for key, value in [('eligible_for_promotion', False), ('confirmation_selection_fixed', False),
                           ('test_metrics_accessed', False), ('promoted', True)]:
            original = self.report[key]
            self.report[key] = value
            self.save_fixture()
            with self.assertRaises(ValueError):
                deployed.export_local_model(self.run, self.destination)
            self.assertFalse(self.destination.exists())
            self.report[key] = original

    def test_boolean_eligible_flag_cannot_override_failed_metrics(self):
        self.fixture()
        self.report['confirmation']['gru'][deployed.TARGETS[1]]['mae_pct'] = 1.1
        self.save_fixture()
        with self.assertRaisesRegex(ValueError, '2%'):
            deployed.export_local_model(self.run, self.destination)

    def test_escape_checkpoint_and_symlink_escape_rejected(self):
        self.fixture()
        outside = self.root / 'outside.pt'
        outside.write_bytes(b'outside')
        self.report['models']['gru']['checkpoint'] = str(outside)
        self.save_fixture()
        with self.assertRaisesRegex(ValueError, 'escapes'):
            deployed.export_local_model(self.run, self.destination)
        link = self.final / 'escape.pt'
        try:
            link.symlink_to(outside)
        except OSError:
            return
        self.report['models']['gru']['checkpoint'] = str(link)
        self.save_fixture()
        with self.assertRaisesRegex(ValueError, 'escapes'):
            deployed.export_local_model(self.run, self.destination)

    def test_existing_destination_is_not_overwritten(self):
        self.fixture()
        self.destination.mkdir()
        keep = self.destination / 'keep.txt'
        keep.write_text('keep')
        with self.assertRaisesRegex(ValueError, 'never overwritten'):
            deployed.export_local_model(self.run, self.destination)
        self.assertEqual(keep.read_text(), 'keep')

    def test_ensemble_copies_two_components_and_relative_manifest(self):
        manifest = self.export('ensemble')
        self.assertEqual(set(manifest['sha256_files']), {'ensemble.json', 'gru.pt', 'tcn.pt', 'validation-report.json'})
        ensemble = json.loads((self.destination / 'ensemble.json').read_text())
        self.assertEqual(ensemble['components'], {'gru': 'gru.pt', 'tcn': 'tcn.pt'})
        self.assertTrue((self.destination / 'gru.pt').is_file())
        self.assertTrue((self.destination / 'tcn.pt').is_file())

    def test_manifest_filename_traversal_rejected(self):
        manifest = self.export()
        manifest['model_file'] = '../outside.pt'
        self.write(self.destination / deployed.MANIFEST, manifest)
        with self.assertRaisesRegex(ValueError, 'same-directory'):
            deployed.load_deployed_model(self.destination)

    def test_prediction_shape_and_nonfinite_values_rejected(self):
        bundle = {'kind': 'gru', 'model': {}}
        with patch('pipeline.train_equity.predict_neural_samples', return_value=np.ones((2, 2))):
            result = deployed.predict_deployed(bundle, np.zeros((2, 60, 8)))
            self.assertEqual(result.shape, (2, 2))
        for value in (np.zeros((2, 59, 8)), np.full((1, 60, 8), np.nan)):
            with self.assertRaises(ValueError):
                deployed.predict_deployed(bundle, value)
        with patch('pipeline.train_equity.predict_neural_samples', return_value=np.full((2, 2), np.inf)):
            with self.assertRaisesRegex(ValueError, 'malformed'):
                deployed.predict_deployed(bundle, np.zeros((2, 60, 8)))
        self.assertEqual(deployed.predict_deployed(bundle, np.empty((0, 60, 8))).shape, (0, 2))

    def test_checkpoint_metadata_must_match_report(self):
        self.export()
        changed = {**self.bundle, 'metadata': {'validation': metrics(.3), 'split': self.report['split']}}
        with patch('pipeline.train_equity.load_neural_bundle', return_value=changed):
            with self.assertRaisesRegex(ValueError, 'evidence differs'):
                deployed.load_deployed_model(self.destination)


if __name__ == '__main__':
    unittest.main()
