import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pipeline import training_resources as resources


def fake_torch(cuda=False):
    return SimpleNamespace(set_num_threads=Mock(), cuda=SimpleNamespace(
        is_available=lambda: cuda, empty_cache=Mock(),
        get_device_properties=lambda _: SimpleNamespace(name='Test GPU'),
        mem_get_info=lambda: (8 * 2**30, 8 * 2**30)))


class ResourceTests(unittest.TestCase):
    def test_cpu_auto_is_explicit_and_reserves_threads(self):
        with patch.object(resources, '_load_torch', return_value=fake_torch()), \
             patch.object(resources.os, 'cpu_count', return_value=16), \
             patch.object(resources, '_available_ram_gb', return_value=48.):
            result = resources.profile_resources()
        self.assertEqual(result['device'], 'cpu')
        self.assertEqual(result['cpu_threads'], 14)
        self.assertEqual(result['batch_size'], 256)
        self.assertEqual(result['probe_results'], [])

    def test_explicit_cuda_does_not_fall_back(self):
        with patch.object(resources, '_load_torch', return_value=fake_torch()):
            with self.assertRaisesRegex(ValueError, 'CUDA was requested'):
                resources.profile_resources(device='cuda')

    def test_invalid_thread_request_is_not_silently_changed(self):
        with patch.object(resources, '_load_torch', return_value=fake_torch()), \
             patch.object(resources.os, 'cpu_count', return_value=4):
            with self.assertRaisesRegex(ValueError, 'only 4'):
                resources.profile_resources(cpu_threads=5)

    def test_gpu_uses_measured_throughput_not_largest_batch(self):
        def probe(_torch, batch, hidden):
            return {'batch_size': batch, 'peak_memory_mb': batch * 2,
                    'samples_per_second': {128: 100, 256: 200, 512: 180, 1024: 160, 2048: 150}[batch]}
        with patch.object(resources, '_load_torch', return_value=fake_torch(True)), \
             patch.object(resources, '_available_ram_gb', return_value=48.), \
             patch.object(resources, '_probe_cuda_batch', side_effect=probe):
            result = resources.profile_resources()
        self.assertEqual(result['device'], 'cuda')
        self.assertEqual(result['batch_size'], 256)
        self.assertAlmostEqual(result['memory_budget_mb'], 8192 * .70)

    def test_oom_retains_previous_safe_batch(self):
        def probe(_torch, batch, hidden):
            if batch > 128:
                raise RuntimeError('CUDA out of memory')
            return {'batch_size': batch, 'peak_memory_mb': 1000., 'samples_per_second': 50.}
        with patch.object(resources, '_load_torch', return_value=fake_torch(True)), \
             patch.object(resources, '_probe_cuda_batch', side_effect=probe):
            result = resources.profile_resources()
        self.assertEqual(result['batch_size'], 128)
        self.assertEqual(result['probe_results'][-1]['status'], 'out_of_memory')

    def test_explicit_oom_requires_user_to_reduce_batch(self):
        with patch.object(resources, '_load_torch', return_value=fake_torch(True)), \
             patch.object(resources, '_probe_cuda_batch', side_effect=RuntimeError('CUDA out of memory')):
            with self.assertRaisesRegex(ValueError, 'Explicit batch size 4096'):
                resources.profile_resources(batch_size=4096)

    def test_bad_counts_rejected_before_loading_torch(self):
        with patch.object(resources, '_load_torch') as load:
            with self.assertRaises(ValueError):
                resources.profile_resources(batch_size=-1)
            with self.assertRaises(ValueError):
                resources.profile_resources(cpu_threads=True)
        load.assert_not_called()


if __name__ == '__main__':
    unittest.main()
