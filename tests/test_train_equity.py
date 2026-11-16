"""Neural architecture and safe checkpoint contracts; no live market requests."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from pipeline.train_equity import (ENSEMBLE_FORMAT_VERSION, FORMAT_VERSION, TARGET_NAMES, build_network,
    load_ensemble_bundle, load_neural_bundle, predict_ensemble_samples, predict_neural_samples)


def manifest_payload():
    return {'format_version': ENSEMBLE_FORMAT_VERSION, 'kind': 'ensemble', 'target_unit': 'percent',
            'target_names': TARGET_NAMES, 'feature_count': 8, 'window': 60,
            'components': {'gru': 'gru.pt', 'tcn': 'tcn.pt'},
            'weights': {'gru': .25, 'tcn': .75}, 'metadata': {'promoted': False}}


class EnsembleManifestSafety(unittest.TestCase):
    def test_rejects_nonlocal_paths_before_loading_any_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'gru.pt').write_bytes(b'not loaded')
            (root / 'tcn.pt').write_bytes(b'not loaded')
            manifest = root / 'ensemble.json'
            for reference in ('../outside.pt', '/tmp/outside.pt', r'C:\outside.pt', r'..\outside.pt',
                              'sub/candidate.pt', 'C:candidate.pt', 'https://example.com/model.pt'):
                with self.subTest(reference=reference):
                    payload = manifest_payload()
                    payload['components']['gru'] = reference
                    manifest.write_text(json.dumps(payload))
                    with patch('pipeline.train_equity.load_neural_bundle') as load:
                        with self.assertRaises(ValueError):
                            load_ensemble_bundle(manifest)
                        load.assert_not_called()

    def test_rejects_invalid_weights_before_loading_checkpoints(self):
        bad = ({'gru': .2, 'tcn': .3}, {'gru': -.1, 'tcn': 1.1},
               {'gru': True, 'tcn': 0}, {'gru': '0.5', 'tcn': .5},
               {'gru': float('nan'), 'tcn': .5}, {'gru': .5}, {'gru': .5, 'tcn': .5, 'extra': 0})
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / 'ensemble.json'
            for weights in bad:
                with self.subTest(weights=weights):
                    payload = manifest_payload()
                    payload['weights'] = weights
                    manifest.write_text(json.dumps(payload))
                    with patch('pipeline.train_equity.load_neural_bundle') as load:
                        with self.assertRaises(ValueError):
                            load_ensemble_bundle(manifest)
                        load.assert_not_called()

    def test_rejects_checkpoint_symlink_outside_manifest_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            models = root / 'models'
            models.mkdir()
            external = root / 'outside.pt'
            external.write_bytes(b'not loaded')
            try:
                (models / 'gru.pt').symlink_to(external)
            except OSError:
                self.skipTest('symlink creation unavailable on this platform')
            (models / 'tcn.pt').write_bytes(b'not loaded')
            manifest = models / 'ensemble.json'
            manifest.write_text(json.dumps(manifest_payload()))
            with patch('pipeline.train_equity.load_neural_bundle') as load:
                with self.assertRaises(ValueError):
                    load_ensemble_bundle(manifest)
                load.assert_not_called()


@unittest.skipUnless(importlib.util.find_spec('torch'), 'optional PyTorch is not installed')
class NeuralContracts(unittest.TestCase):
    def test_synthetic_training_emits_real_epochs_without_confirmation(self):
        from pipeline.train_equity import main, _metrics, _sample_index
        from tests.test_equity_model import make_history, write_history

        def test_labels_are_canaries(*args, **kwargs):
            samples = _sample_index(*args, **kwargs)
            test_dates = set(sorted({row[2] for row in samples})[-20:])
            return [(code, index, day, (9999., 9999.) if day in test_dates else target)
                    for code, index, day, target in samples]

        def metrics_without_test(actual, predicted):
            self.assertFalse(np.any(np.asarray(actual) == 9999.), 'confirmation labels were scored while tuning')
            return _metrics(actual, predicted)

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            write_history(root / 'daily', make_history(count=2))
            with patch('pipeline.train_equity._sample_index', side_effect=test_labels_are_canaries), \
                 patch('pipeline.train_equity._metrics', side_effect=metrics_without_test):
                self.assertEqual(main(['--history-dir', str(root / 'daily'), '--model-dir', str(root / 'models'),
                    '--models', 'gru,tcn', '--device', 'cpu', '--max-stocks', '2', '--epochs', '1',
                    '--cpu-threads', '2', '--validation-only', '--progress-file', str(root / 'progress.json')]), 0)
            report = json.loads((root / 'models/training_report.json').read_text())
            progress = json.loads((root / 'progress.json').read_text())
            self.assertFalse(report['test_metrics_accessed'])
            self.assertEqual(report['confirmation'], {})
            self.assertEqual({p['model'] for p in progress['history']}, {'gru', 'tcn'})
            self.assertTrue(all(p['epoch'] == 1 and p['train_huber'] >= 0 for p in progress['history']))
            ensemble = report['models']['ensemble']
            self.assertIn(ensemble['weights']['gru'], [.25, .5, .75])
            self.assertEqual(ensemble['weight_selection'], 'validation_grid')
            self.assertEqual(sum(ensemble['weights'].values()), 1.)
            self.assertTrue(Path(ensemble['manifest']).is_file())

    def test_fixed_ensemble_confirmation_preserves_prior_validation_weight(self):
        from pipeline.train_equity import main
        from tests.test_equity_model import make_history, write_history
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_history(root / 'daily', make_history(count=2))
            self.assertEqual(main(['--history-dir', str(root / 'daily'), '--model-dir', str(root / 'models'),
                '--models', 'ridge', '--device', 'cpu', '--max-stocks', '2', '--epochs', '1',
                '--cpu-threads', '2', '--confirm-model', 'ensemble', '--ensemble-weight', '.37']), 0)
            report = json.loads((root / 'models/training_report.json').read_text())
            self.assertEqual(report['selected_by_validation'], 'ensemble')
            self.assertTrue(report['confirmation_selection_fixed'])
            self.assertTrue(report['test_metrics_accessed'])
            self.assertEqual(set(report['confirmation']), {'zero', 'ridge', 'ensemble'})
            self.assertEqual(report['models']['ensemble']['weights'], {'gru': .37, 'tcn': .63})
            self.assertEqual(report['models']['ensemble']['validation_weight_grid'], [.37])
            self.assertEqual(report['models']['ensemble']['weight_selection'], 'fixed_prior_validation')

    def test_ensemble_confirmation_requires_preselected_weight(self):
        from pipeline.train_equity import main
        with self.assertRaisesRegex(ValueError, 'requires --ensemble-weight'):
            main(['--confirm-model', 'ensemble', '--device', 'cpu'])

    def test_tcn_is_causal_before_forecast_head(self):
        import torch
        torch.set_num_threads(2)
        torch.manual_seed(7)
        model = build_network('tcn', hidden=8, dropout=0).eval()
        original = torch.randn(2, 8, 60)
        changed = original.clone()
        changed[:, :, 40:] += 100
        with torch.no_grad():
            self.assertTrue(torch.allclose(model.blocks(original)[:, :, :40], model.blocks(changed)[:, :, :40]))

    def test_candidate_checkpoint_roundtrip_preserves_predictions(self):
        import torch
        torch.set_num_threads(2)
        torch.manual_seed(11)
        for kind in ('gru', 'tcn'):
            architecture = {'feature_count': 8, 'hidden': 8, 'layers': 1, 'dropout': 0}
            model = build_network(kind, **architecture).eval()
            sequence = np.random.default_rng(11).normal(size=(3, 60, 8)).astype(np.float32)
            with torch.no_grad():
                expected = model(torch.from_numpy(sequence)).numpy()
            with tempfile.TemporaryDirectory() as root:
                path = Path(root) / 'candidate.pt'
                torch.save({'format_version': FORMAT_VERSION, 'target_unit': 'percent',
                    'kind': kind, 'architecture': architecture, 'state_dict': model.state_dict(),
                    'mean': [0.] * 8, 'scale': [1.] * 8, 'window': 60,
                    'target_names': TARGET_NAMES, 'metadata': {'promoted': False}}, path)
                bundle = load_neural_bundle(path)
                self.assertFalse(bundle['metadata']['promoted'])
                np.testing.assert_allclose(predict_neural_samples(bundle, sequence, batch_size=2), expected,
                    rtol=1e-5, atol=1e-6)
                with self.assertRaises(ValueError):
                    predict_neural_samples(bundle, sequence[:, :20])

    def test_ensemble_manifest_roundtrip_equals_fixed_weighted_predictions(self):
        import torch
        torch.set_num_threads(2)
        torch.manual_seed(12)
        sequence = np.random.default_rng(12).normal(size=(3, 60, 8)).astype(np.float32)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = np.zeros((3, 2), dtype=np.float32)
            for kind, weight in [('gru', .25), ('tcn', .75)]:
                architecture = {'feature_count': 8, 'hidden': 8, 'layers': 1, 'dropout': 0}
                model = build_network(kind, **architecture).eval()
                with torch.no_grad():
                    expected += weight * model(torch.from_numpy(sequence)).numpy()
                torch.save({'format_version': FORMAT_VERSION, 'target_unit': 'percent',
                    'kind': kind, 'architecture': architecture, 'state_dict': model.state_dict(),
                    'mean': [0.] * 8, 'scale': [1.] * 8, 'window': 60,
                    'target_names': TARGET_NAMES, 'metadata': {'promoted': False}}, root / (kind + '.pt'))
            manifest = root / 'ensemble.json'
            manifest.write_text(json.dumps(manifest_payload()))
            loaded = load_ensemble_bundle(manifest)
            np.testing.assert_allclose(predict_ensemble_samples(loaded, sequence, batch_size=2), expected,
                                       rtol=1e-5, atol=1e-6)

    def test_hidden_128_networks_produce_two_targets(self):
        import torch
        torch.set_num_threads(2)
        for kind in ('gru', 'tcn'):
            model = build_network(kind, hidden=128).eval()
            with torch.no_grad():
                self.assertEqual(tuple(model(torch.zeros(2, 60, 8)).shape), (2, 2))


if __name__ == '__main__':
    unittest.main()
