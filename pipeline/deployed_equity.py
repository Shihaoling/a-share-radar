"""Export validated local candidates and load them for CPU-only cloud inference.

This module does not train, contact a service, upload, or replace an existing
deployment. A successful export creates a separate reviewable directory.
"""
from __future__ import annotations

from argparse import ArgumentParser
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile

import numpy as np

TARGETS = ['next_open_return_pct', 'next_close_return_pct']
KINDS = {'gru', 'tcn', 'ensemble'}
MANIFEST = 'deployed-model.json'


def _reject_constant(value):
    raise ValueError('Non-finite JSON values are not allowed')


def _read_json(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('Missing or oversized deployment metadata')
    try:
        value = json.loads(path.read_text(encoding='utf-8'), parse_constant=_reject_constant)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError('Invalid deployment metadata') from error
    if not isinstance(value, dict):
        raise ValueError('Deployment metadata must be a JSON object')
    return value


def _filename(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,180}', value):
        raise ValueError('Deployment assets must use simple same-directory filenames')
    return value


def _inside(root, path):
    root, resolved = Path(root).resolve(), Path(path).resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError('Model file is missing or escapes its permitted directory')
    return resolved


def _asset(root, name):
    return _inside(root, Path(root) / _filename(name))


def _checkpoint_source(final_dir, value):
    if not isinstance(value, str) or not value:
        raise ValueError('Candidate checkpoint path is missing')
    path = Path(value)
    return _inside(final_dir, path if path.is_absolute() else Path(final_dir) / path)


def _sha256(path):
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError('Deployment asset exceeds the small-model size limit')
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _instant(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError('Training timestamp must be a valid ISO timestamp') from error
    if result.tzinfo is None:
        raise ValueError('Training timestamp must include its timezone')
    return result


def _mae(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('Validation evidence contains an invalid MAE')
    return value


def _validate_report(report, kind):
    if (report.get('eligible_for_promotion') is not True or report.get('promoted') is not False
            or report.get('confirmation_selection_fixed') is not True
            or report.get('test_metrics_accessed') is not True
            or report.get('selected_by_validation') != kind
            or report.get('target_names') != TARGETS or report.get('target_unit') != 'percent'):
        raise ValueError('Candidate lacks completed local selection and confirmation evidence')
    try:
        for target in TARGETS:
            for results in (report['confirmation'],
                            {name: result['validation'] for name, result in report['models'].items()}):
                candidate = _mae(results[kind][target]['mae_pct'])
                baseline = min(_mae(results[name][target]['mae_pct']) for name in ('zero', 'ridge'))
                if not candidate < baseline * .98:
                    raise ValueError('Candidate has not beaten both baseline gates by 2% on both targets')
    except (KeyError, TypeError) as error:
        raise ValueError('Candidate validation or confirmation metrics are incomplete') from error


def _ensemble_components(manifest):
    if (manifest.get('format_version') != 'equity-ensemble-v1' or manifest.get('kind') != 'ensemble'
            or manifest.get('target_names') != TARGETS or manifest.get('target_unit') != 'percent'
            or manifest.get('feature_count') != 8 or manifest.get('window') != 60):
        raise ValueError('Unsupported ensemble manifest')
    components, weights = manifest.get('components'), manifest.get('weights')
    if not isinstance(components, dict) or set(components) != {'gru', 'tcn'}:
        raise ValueError('Ensemble requires exactly GRU and TCN components')
    names = [_filename(components[name]) for name in ('gru', 'tcn')]
    if len(set(names)) != 2 or any(not name.endswith('.pt') for name in names):
        raise ValueError('Ensemble component checkpoints must be distinct .pt files')
    if not isinstance(weights, dict) or set(weights) != {'gru', 'tcn'}:
        raise ValueError('Ensemble weights are missing')
    values = [_mae(weights[name]) for name in ('gru', 'tcn')]
    if any(value > 1 for value in values) or not math.isclose(sum(values), 1., abs_tol=1e-8):
        raise ValueError('Ensemble weights must be nonnegative and sum to one')
    return components


def load_deployed_model(model_dir):
    """Return a validated CPU inference bundle, or None if no manifest exists."""
    root = Path(model_dir).resolve()
    if not (root / MANIFEST).exists():
        return None
    manifest = _read_json(_asset(root, MANIFEST))
    kind = manifest.get('kind')
    if (type(manifest.get('schema_version')) is not int or manifest['schema_version'] != 1
            or kind not in KINDS or manifest.get('window') != 60
            or manifest.get('source') != 'local' or manifest.get('status') != 'validated_candidate'):
        raise ValueError('Unsupported deployed-model schema or provenance')
    _instant(manifest.get('trained_at'))
    try:
        datetime.strptime(manifest['label_cutoff'], '%Y-%m-%d')
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError('Deployment label cutoff is missing or invalid') from error
    model_name, report_name = _filename(manifest.get('model_file')), _filename(manifest.get('report_file'))
    if model_name == report_name or model_name == MANIFEST or report_name == MANIFEST:
        raise ValueError('Deployment files must have distinct names')
    expected = {model_name, report_name}
    model_path, report_path = _asset(root, model_name), _asset(root, report_name)
    if kind == 'ensemble':
        ensemble = _read_json(model_path)
        expected.update(_ensemble_components(ensemble).values())
        if len(expected) != 4:
            raise ValueError('Ensemble assets must have distinct names')
    elif not model_name.endswith('.pt'):
        raise ValueError('Neural deployment requires a .pt checkpoint')
    hashes = manifest.get('sha256_files')
    if not isinstance(hashes, dict) or set(hashes) != expected:
        raise ValueError('Deployment hashes must cover exactly every model and report asset')
    for name, claimed in hashes.items():
        if not isinstance(claimed, str) or not re.fullmatch(r'[0-9a-f]{64}', claimed):
            raise ValueError('Invalid deployment SHA256')
        if _sha256(_asset(root, name)) != claimed:
            raise ValueError('Deployment asset checksum mismatch')
    report = _read_json(report_path)
    _validate_report(report, kind)
    from .train_equity import load_neural_bundle, load_ensemble_bundle
    model = (load_ensemble_bundle(model_path, device='cpu') if kind == 'ensemble'
             else load_neural_bundle(model_path, device='cpu'))
    if model.get('kind') != kind or model.get('window') != 60 or model.get('target_names') != TARGETS:
        raise ValueError('Checkpoint architecture or prediction target differs from deployment metadata')
    metadata = model.get('metadata', {})
    if (metadata.get('validation') != report['models'][kind]['validation']
            or metadata.get('split') != report.get('split')):
        raise ValueError('Checkpoint validation evidence differs from the exported report')
    if kind == 'ensemble':
        if model.get('weights') != report['models'][kind].get('weights'):
            raise ValueError('Ensemble weights differ from the locally validated weights')
    else:
        mean, scale = np.asarray(model.get('mean')), np.asarray(model.get('scale'))
        if (mean.shape != (8,) or scale.shape != (8,) or not np.isfinite(mean).all()
                or not np.isfinite(scale).all() or np.any(scale <= 0)):
            raise ValueError('Checkpoint normalization parameters are invalid')
    return {'kind': kind, 'model': model, 'manifest': manifest, 'window': 60,
            'trained_at': manifest['trained_at'], 'label_cutoff': manifest['label_cutoff'],
            'model_id': kind + '-' + hashes[model_name][:12]}


def predict_deployed(bundle, sequences):
    """Return N x 2 percentage returns without fitting or changing parameters."""
    values = np.asarray(sequences, dtype=np.float32)
    if values.ndim != 3 or values.shape[1:] != (60, 8) or not np.isfinite(values).all():
        raise ValueError('Deployed prediction requires finite N x 60 x 8 windows')
    if len(values) == 0:
        return np.empty((0, 2), dtype=np.float32)
    from .train_equity import predict_neural_samples, predict_ensemble_samples
    if bundle['kind'] == 'ensemble':
        output = predict_ensemble_samples(bundle['model'], values)
    elif bundle['kind'] in ('gru', 'tcn'):
        output = predict_neural_samples(bundle['model'], values)
    else:
        raise ValueError('Unknown deployed model kind')
    output = np.asarray(output, dtype=np.float32)
    if output.shape != (len(values), 2) or not np.isfinite(output).all():
        raise ValueError('Deployed model returned malformed predictions')
    return output


def _public_report(report):
    # Preserve selection evidence while removing machine paths and unrelated
    # experiment checkpoints from a directory intended for publication.
    clean = deepcopy(report)
    for key in ('history_dir', 'model_dir', 'progress_file'):
        clean.get('parameters', {}).pop(key, None)
    for result in clean.get('models', {}).values():
        for key in ('checkpoint', 'checkpoints', 'manifest'):
            result.pop(key, None)
    return clean


def export_local_model(run_dir, destination):
    """Create a new upload candidate directory; never performs an upload."""
    run = Path(run_dir).resolve()
    final_dir = run / 'final'
    if not final_dir.resolve().is_relative_to(run):
        raise ValueError('Final model directory escapes the local run')
    summary = _read_json(_asset(run, 'local-summary.json'))
    report = _read_json(_asset(final_dir, 'training_report.json'))
    if (summary.get('status') != 'completed' or summary.get('uploaded') is not False
            or summary.get('execution') != 'local' or summary.get('eligible_for_promotion') is not True):
        raise ValueError('Local run is incomplete, already uploaded, or ineligible for model export')
    kind = report.get('selected_by_validation')
    if kind not in KINDS or summary.get('selected', {}).get('model') != kind:
        raise ValueError('Only the locally selected GRU, TCN or ensemble can be exported')
    _validate_report(report, kind)
    trained_at = summary.get('completed_at') or report.get('created_at')
    completed = _instant(trained_at)
    cutoff = report.get('label_cutoff')
    basis = 'explicit_last_training_label' if cutoff else 'training_completion_date_conservative'
    if not cutoff:
        # split.test.end is a feature date, not necessarily its label date.
        cutoff = completed.date().isoformat()
    destination = Path(destination)
    if destination.is_symlink() or (destination.exists() and (not destination.is_dir() or any(destination.iterdir()))):
        raise ValueError('Export destination must be new or empty; existing models are never overwritten')
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.equity-export-', dir=destination.parent))
    try:
        model_result = report['models'][kind]
        model_source = _checkpoint_source(final_dir, model_result.get('checkpoint'))
        asset_names = ['validation-report.json']
        if kind == 'ensemble':
            ensemble = _read_json(model_source)
            components = _ensemble_components(ensemble)
            for component, name in components.items():
                source = _asset(model_source.parent, name)
                if not source.is_relative_to(final_dir.resolve()):
                    raise ValueError('Ensemble checkpoint escapes the final run directory')
                shutil.copyfile(source, stage / f'{component}.pt')
                asset_names.append(f'{component}.pt')
            ensemble['components'] = {'gru': 'gru.pt', 'tcn': 'tcn.pt'}
            model_name = 'ensemble.json'
            (stage / model_name).write_text(json.dumps(ensemble, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        else:
            model_name = f'{kind}.pt'
            shutil.copyfile(model_source, stage / model_name)
        asset_names.append(model_name)
        (stage / 'validation-report.json').write_text(json.dumps(_public_report(report), ensure_ascii=False,
            indent=2, allow_nan=False), encoding='utf-8')
        manifest = {'schema_version': 1, 'kind': kind, 'model_file': model_name,
                    'trained_at': trained_at, 'window': 60, 'label_cutoff': cutoff,
                    'label_cutoff_basis': basis, 'source': 'local', 'status': 'validated_candidate',
                    'report_file': 'validation-report.json',
                    'sha256_files': {name: _sha256(stage / name) for name in asset_names}}
        (stage / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        load_deployed_model(stage)  # Includes safe CPU checkpoint loading before publishing any directory.
        if destination.exists():
            destination.rmdir()  # Fails safely if another process populated it.
        os.replace(stage, destination)
        return manifest
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main(argv=None):
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args(argv)
    manifest = export_local_model(args.run_dir, args.output_dir)
    print(json.dumps({'status': 'exported_locally', 'uploaded': False,
                      'kind': manifest['kind'], 'output_dir': str(Path(args.output_dir).resolve())}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
