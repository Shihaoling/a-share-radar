"""Neural architecture and safe checkpoint contracts; no live market requests."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

import numpy as np

from pipeline.train_equity import (FORMAT_VERSION, TARGET_NAMES, build_network,
    load_neural_bundle, predict_neural_samples)


@unittest.skipUnless(importlib.util.find_spec('torch'), 'optional PyTorch is not installed')
class NeuralContracts(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
