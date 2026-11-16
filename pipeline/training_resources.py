"""Measure this machine before local training; never contacts a data service.

Random tensors below only measure hardware capacity and throughput. Their loss
is deliberately not retained and is not a financial-model validation result.
"""
from __future__ import annotations

import ctypes
import gc
import math
import os
import platform
import time


def _load_torch():
    try:
        import torch
    except ImportError as exc:
        raise ImportError('PyTorch is required. Install the appropriate CPU/CUDA build into the local training environment.') from exc
    return torch


def _available_ram_gb():
    if platform.system() == 'Windows':
        class MemoryStatus(ctypes.Structure):
            _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                        ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                        ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                        ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                        ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]
        try:
            state = MemoryStatus()
            state.dwLength = ctypes.sizeof(state)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(state)):
                return round(state.ullAvailPhys / 2**30, 3)
        except (AttributeError, OSError):
            pass
    elif platform.system() == 'Linux':
        try:
            with open('/proc/meminfo', encoding='ascii') as handle:
                values = dict(line.split(':', 1) for line in handle if ':' in line)
            return round(float(values['MemAvailable'].split()[0]) / 2**20, 3)
        except (OSError, KeyError, ValueError):
            pass
    return None


def _is_oom(torch, error):
    oom_type = getattr(torch, 'OutOfMemoryError', ())
    return (bool(oom_type) and isinstance(error, oom_type)) or 'out of memory' in str(error).lower()


def _probe_cuda_batch(torch, batch_size, hidden):
    from .train_equity import build_network
    model_results = []
    # The real model, sequence dimensions, optimizer states and backward pass
    # are measured; two timed steps follow one warmup step for each candidate.
    for kind in ('gru', 'tcn'):
        net = optimizer = x = y = prediction = loss = None
        try:
            gc.collect()
            torch.cuda.empty_cache()
            torch.cuda.reset_peak_memory_stats()
            net = build_network(kind, hidden=hidden, layers=2, dropout=.1).to('cuda')
            optimizer = torch.optim.AdamW(net.parameters(), lr=3e-4, weight_decay=1e-4)
            x = torch.randn(batch_size, 60, 8, device='cuda')
            y = torch.randn(batch_size, 2, device='cuda')
            for step in range(3):
                if step == 1:
                    torch.cuda.synchronize()
                    started = time.perf_counter()
                optimizer.zero_grad(set_to_none=True)
                prediction = net(x)
                loss = torch.nn.functional.huber_loss(prediction, y)
                loss.backward()
                torch.nn.utils.clip_grad_norm_(net.parameters(), 1.)
                optimizer.step()
            torch.cuda.synchronize()
            elapsed = max(time.perf_counter() - started, 1e-6)
            model_results.append({'model': kind, 'timed_steps': 2,
                'elapsed_seconds': round(elapsed, 6),
                'samples_per_second': round(2 * batch_size / elapsed, 3),
                'peak_allocated_mb': torch.cuda.max_memory_allocated() / 2**20,
                'peak_reserved_mb': torch.cuda.max_memory_reserved() / 2**20})
        finally:
            del loss, prediction, x, y, optimizer, net
            gc.collect()
            torch.cuda.empty_cache()
    return {'batch_size': batch_size, 'models': model_results,
            'peak_memory_mb': max(r['peak_reserved_mb'] for r in model_results),
            'samples_per_second': min(r['samples_per_second'] for r in model_results)}


def profile_resources(device='auto', batch_size=0, hidden=64, cpu_threads=0):
    """Return measured settings, preserving explicit batch/thread requests.

    CUDA auto sizing is bounded to 128..2048. A shared batch must fit both GRU
    and TCN, with room left for the display, driver and other applications.
    """
    if device not in ('auto', 'cuda', 'cpu'):
        raise ValueError('Local device must be auto, cuda or cpu')
    if any(type(value) is not int or value < 0 for value in (batch_size, cpu_threads)):
        raise ValueError('batch_size and cpu_threads must be nonnegative integers')
    if hidden not in (32, 64, 128):
        raise ValueError('hidden must be 32, 64 or 128')
    torch = _load_torch()
    cores = max(1, os.cpu_count() or 1)
    threads = min(16, max(1, cores - 2)) if cpu_threads == 0 else cpu_threads
    if threads > cores:
        raise ValueError(f'Requested {threads} CPU threads, but only {cores} logical CPUs were detected')
    torch.set_num_threads(threads)
    cuda_available = bool(torch.cuda.is_available())
    selected = ('cuda' if cuda_available else 'cpu') if device == 'auto' else device
    if selected == 'cuda' and not cuda_available:
        raise ValueError('CUDA was requested but is unavailable. Check the NVIDIA driver and CUDA-enabled PyTorch installation, or explicitly select CPU.')
    ram = _available_ram_gb()
    result = {'device': selected, 'batch_size': batch_size, 'cpu_threads': threads,
              'logical_cpu_count': cores, 'available_ram_gb': ram,
              'gpu_name': None, 'gpu_total_mb': None, 'memory_budget_mb': None,
              'probe_results': [], 'probe_kind': 'random_tensor_hardware_probe_only',
              'note': 'Hardware probing is not stock-model training or predictive-performance evidence.'}
    if selected == 'cpu':
        result['batch_size'] = batch_size or (256 if threads >= 8 and ram is not None and ram >= 16 else 128)
        result['selection_reason'] = 'CPU selected explicitly or CUDA unavailable; no GPU training is claimed.'
        return result

    properties = torch.cuda.get_device_properties(0)
    free_bytes, total_bytes = torch.cuda.mem_get_info()
    budget = min(free_bytes * .70, total_bytes * .80) / 2**20
    result.update(gpu_name=properties.name, gpu_total_mb=total_bytes / 2**20,
                  gpu_free_mb=free_bytes / 2**20, memory_budget_mb=budget)
    choices = [batch_size] if batch_size else [128, 256, 512, 1024, 2048]
    safe, previous = [], None
    for candidate in choices:
        if previous and previous['peak_memory_mb'] * candidate / previous['batch_size'] > budget:
            result['probe_results'].append({'batch_size': candidate, 'status': 'not_attempted_budget_estimate'})
            break
        try:
            measured = _probe_cuda_batch(torch, candidate, hidden)
        except RuntimeError as error:
            if not _is_oom(torch, error):
                raise
            gc.collect()
            torch.cuda.empty_cache()
            result['probe_results'].append({'batch_size': candidate, 'status': 'out_of_memory'})
            if batch_size:
                raise ValueError(f'Explicit batch size {batch_size} exceeded available GPU memory. Reduce --batch-size or use 0 for automatic sizing.') from None
            break
        within_budget = measured['peak_memory_mb'] <= budget
        measured['status'] = 'safe' if within_budget else 'exceeds_memory_budget'
        result['probe_results'].append(measured)
        if not within_budget:
            if batch_size:
                raise ValueError(f'Explicit batch size {batch_size} leaves insufficient GPU memory headroom. Reduce --batch-size or use 0 for automatic sizing.')
            break
        if not math.isfinite(measured['samples_per_second']) or measured['samples_per_second'] <= 0:
            raise ValueError('GPU throughput probe returned an invalid measurement')
        safe.append(measured)
        previous = measured
    if not safe:
        raise ValueError('No probed GPU batch fits the memory budget. Close other GPU workloads or explicitly try a smaller batch size.')
    best = max(safe, key=lambda item: item['samples_per_second'])
    result['batch_size'] = best['batch_size']
    result['selection_reason'] = 'Highest measured conservative throughput across GRU and TCN among batches within the memory budget.'
    return result
