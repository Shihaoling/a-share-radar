"""Local-only tuning, final confirmation, and a loopback progress dashboard."""
from argparse import ArgumentParser, Namespace
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import webbrowser

from .training_progress import ProgressWriter, MonitorServer

ROOT = Path(__file__).resolve().parents[1]


def _save(path, value):
    path = Path(path)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def choose_candidate(reports):
    """Only validation metrics may select a configuration or architecture."""
    candidates = []
    for trial, report in enumerate(reports, 1):
        if report.get('test_metrics_accessed') or report.get('confirmation'):
            raise ValueError('Tuning reports must not contain confirmation metrics')
        for model, result in report.get('models', {}).items():
            value = result.get('validation', {}).get('mean_mae_pct')
            if isinstance(value, (int, float)) and 0 <= value < float('inf'):
                candidates.append({'trial': trial, 'model': model, 'validation_mae_pct': value})
    if not candidates:
        raise ValueError('No valid validation candidates; check historical coverage')
    return min(candidates, key=lambda row: (row['validation_mae_pct'], row['trial'], row['model']))


def run_local(args, writer):
    from .training_resources import profile_resources
    from .train_equity import run_training

    output = Path(args.output_dir).resolve()
    writer.write(stage='hardware_probe', message='Detecting local resources and measuring safe batches; this is not model training.',
                 completed_trials=0, total_trials=(4 if args.hidden == 128 else 3) if args.tune else 2, uploaded=False)
    resources = profile_resources(args.device, args.batch_size, args.hidden, args.cpu_threads)
    _save(output / 'resources.json', resources)
    writer.write(device=resources['device'], resources=resources,
                 memory={'gpu_total_mb': resources.get('gpu_total_mb')}, stage='preparing',
                 message='Local data only. No credentials, market API requests, or uploads.')
    configs = ([{'hidden': 32, 'learning_rate': 3e-4}, {'hidden': args.hidden, 'learning_rate': 1e-4}]
               if args.tune else [{'hidden': args.hidden, 'learning_rate': 3e-4}])
    if args.tune and args.hidden == 128:
        configs.insert(1, {'hidden': 64, 'learning_rate': 3e-4})
    total = len(configs) + 1
    base = dict(history_dir=str(Path(args.history_dir).resolve()), models='ridge,gru,tcn',
        max_stocks=args.max_stocks, epochs=args.epochs, batch_size=resources['batch_size'],
        layers=2, dropout=.1, window=60, patience=4, min_train_dates=60,
        validation_dates=20, test_dates=20, seed=42, device=resources['device'],
        cpu_threads=resources['cpu_threads'], progress_file=str(output / 'progress.json'),
        total_trials=total, confirm_model=None)
    reports, ranking = [], []
    for trial, config in enumerate(configs, 1):
        trial_args = Namespace(**base, **config, trial=trial, validation_only=True,
                               model_dir=str(output / f'trial-{trial:02d}'))
        writer.write(stage='preparing', trial=trial, total_trials=total,
                     completed_trials=trial - 1, message='Tuning on validation data; final test metrics remain uncomputed.')
        report = run_training(trial_args)
        if report['status'] != 'validation_only':
            raise ValueError('Not enough validated historical sessions for local tuning')
        reports.append(report)
        for model, result in report['models'].items():
            ranking.append({'name': f'T{trial} / {model.upper()}', 'trial': trial, 'model': model,
                            'validation_mae_pct': result['validation']['mean_mae_pct'], 'status': 'completed'})
        writer.write(completed_trials=trial, candidates=ranking)
    selected = choose_candidate(reports)
    if selected['model'] == 'ensemble':
        selected['weights'] = reports[selected['trial'] - 1]['models']['ensemble']['weights']
    # Persist the decision before the final confirmation run. No configuration
    # is selected using the held-out period, including when a baseline wins.
    _save(output / 'selection.json', {'selected_at': datetime.now(timezone.utc).isoformat(),
        'selected': selected, 'criterion': 'lowest validation mean MAE',
        'test_metrics_used_for_selection': False, 'configurations': configs})
    chosen = configs[selected['trial'] - 1]
    final_base = {**base, 'models': 'gru,tcn' if selected['model'] == 'ensemble' else selected['model'] if selected['model'] in ('gru', 'tcn') else 'ridge',
                  'confirm_model': selected['model']}
    if selected['model'] == 'ensemble':
        final_base['ensemble_weight'] = reports[selected['trial'] - 1]['models']['ensemble']['weights']['gru']
    final_args = Namespace(**final_base, **chosen, trial=total, validation_only=False,
                           model_dir=str(output / 'final'))
    writer.write(stage='preparing_confirmation', trial=total, total_trials=total,
                 message='Configuration is fixed. Reproducing it with the same seed before one confirmation evaluation.')
    final = run_training(final_args)
    summary = {'status': 'completed', 'completed_at': datetime.now(timezone.utc).isoformat(),
        'execution': 'local', 'uploaded': False, 'promoted': False, 'resources': resources,
        'selected': selected, 'configurations': configs, 'final_report': 'final/training_report.json',
        'eligible_for_promotion': final.get('eligible_for_promotion', False),
        'note': 'Single chronological split. Candidate only; no automatic model deployment or upload.'}
    _save(output / 'local-summary.json', summary)
    if summary['eligible_for_promotion']:
        from .deployed_equity import export_local_model
        writer.write(stage='exporting', message='Verifying a local upload package; no network upload is performed.')
        export_local_model(output, output / 'upload-ready')
        summary['upload_package'] = 'upload-ready'
        _save(output / 'local-summary.json', summary)
    writer.write(status='completed', stage='completed', model=selected['model'], completed_trials=total,
                 batch=None, total_batches=None, report_path=str(output / 'final/training_report.json'),
                 message='Local tuning and confirmation completed. Results remain on this computer; no upload occurred.')
    return summary


def main(argv=None):
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--history-dir', default=str(ROOT / 'data/equity/daily'))
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--device', choices=['auto', 'cuda', 'cpu'], default='auto')
    parser.add_argument('--max-stocks', type=int, default=0)
    parser.add_argument('--epochs', type=int, default=20)
    parser.add_argument('--hidden', type=int, choices=[32, 64, 128], default=128)
    parser.add_argument('--batch-size', type=int, default=0, help='0 probes a safe batch on this device')
    parser.add_argument('--cpu-threads', type=int, default=0)
    parser.add_argument('--port', type=int, default=8768)
    parser.add_argument('--tune', action='store_true')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--exit-after-training', action='store_true')
    parser.add_argument('--serve-only', action='store_true', help='View an existing run without retraining')
    args = parser.parse_args(argv)
    if args.max_stocks < 0 or args.epochs < 1 or args.batch_size < 0 or args.cpu_threads < 0:
        parser.error('Invalid resource counts')
    output = Path(args.output_dir).resolve()
    progress = output / 'progress.json'
    if not args.serve_only and progress.exists():
        parser.error('Run output already exists; use a new directory or --serve-only')
    if not args.serve_only and not list(Path(args.history_dir).glob('*.json.gz')):
        parser.error('No local daily archive found; update the repository data first')
    if args.serve_only and not progress.exists():
        parser.error('No saved progress exists for --serve-only')
    output.mkdir(parents=True, exist_ok=True)
    writer = ProgressWriter(progress)
    with MonitorServer(progress, ROOT / 'web/training-monitor.html', args.port) as monitor:
        print(f'LOCAL_PROGRESS {monitor.url}', flush=True)
        if not args.no_browser:
            webbrowser.open(monitor.url)
        try:
            if not args.serve_only:
                summary = run_local(args, writer)
                print(json.dumps(summary, ensure_ascii=False), flush=True)
            if not args.exit_after_training:
                print('Monitor remains available. Press Ctrl+C to close; saved models and logs are retained.', flush=True)
                while True:
                    time.sleep(.5)
        except KeyboardInterrupt:
            if not args.serve_only:
                current = json.loads(progress.read_text(encoding='utf-8')) if progress.exists() else {}
                if current.get('status') != 'completed':
                    writer.write(status='failed', stage='interrupted', error='Training interrupted by the local user.')
            return 0 if args.serve_only or current.get('status') == 'completed' else 130
        except Exception as exc:
            if not args.serve_only:
                writer.write(status='failed', stage='failed', error=f'{type(exc).__name__}: {exc}')
            raise
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
