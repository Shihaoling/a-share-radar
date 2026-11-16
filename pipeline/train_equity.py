"""Optional, bounded offline experiments for shared A-share sequence models.

Run ``python -m pipeline.train_equity --help``. PyTorch is optional and is never
imported by the website's daily pipeline. Checkpoints are research candidates;
this command does not replace the production forecasting model.
"""
from argparse import ArgumentParser
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import random
import time

import numpy as np

TARGET_NAMES = ["next_open_return_pct", "next_close_return_pct"]
FORMAT_VERSION = "equity-sequence-v1"


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _day(bar):
    return str(bar.get("trade_date") or bar.get("date") or "")[:10]


def _metrics(actual, predicted):
    actual, predicted = np.asarray(actual), np.asarray(predicted)
    result = {}
    for i, name in enumerate(TARGET_NAMES):
        error = predicted[:, i] - actual[:, i]
        nonzero = (actual[:, i] != 0) & (predicted[:, i] != 0)
        direction = float(np.mean(np.sign(predicted[nonzero, i]) == np.sign(actual[nonzero, i]))) if nonzero.any() else None
        result[name] = {"mae_pct": float(np.mean(np.abs(error))),
                        "rmse_pct": float(np.sqrt(np.mean(error ** 2))),
                        "direction_accuracy": direction,
                        "direction_samples": int(nonzero.sum())}
    result["mean_mae_pct"] = float(np.mean(np.abs(predicted - actual)))
    result["samples"] = len(actual)
    return result


def _sequence(history, item, window):
    from .equity_model import sequence_features
    if hasattr(history, "features"):
        if item[1] + 1 < window:
            return None
        values = history.features[item[0]][item[1] - window + 1:item[1] + 1]
        return values if values.shape == (window, 8) and np.isfinite(values).all() else None
    return sequence_features(history[item[0]], item[1], window=window)


class _SequenceHistory(dict):
    """One eight-feature row per stock/date, not repeated rolling windows."""
    def __init__(self, history):
        from .equity_model import sequence_features
        super().__init__(history)
        self.features = {}
        for code, bars in self.items():
            features = np.full((len(bars), 8), np.nan, dtype=np.float32)
            for i in range(len(bars)):
                row = sequence_features(bars, i, window=1)
                if row is not None:
                    features[i] = row[0]
            self.features[code] = features


def _sample_index(history, window, calendar_dates=None):
    """Store (code, offset, date, two labels), never expanded rolling windows."""
    from .calendar import next_trading_day
    dates = calendar_dates or sorted({_day(row) for bars in history.values() for row in bars})
    next_day = dict(zip(dates, dates[1:]))
    samples = []
    for code, bars in sorted(history.items()):
        for i in range(window - 1, len(bars) - 1):
            current, target = bars[i], bars[i + 1]
            # A suspension must not turn a multi-session return into a next-day
            # label. Observed archive dates define the available trading clock.
            if next_day.get(_day(current)) != _day(target):
                continue
            expected = next_trading_day(_day(current))
            if expected is not None and expected.isoformat() != _day(target):
                continue
            reference = _number(target.get("pre_close"))
            opening, close = _number(target.get("open")), _number(target.get("close"))
            if reference is None or reference <= 0 or opening is None or opening <= 0 or close is None or close <= 0:
                continue
            sequence = _sequence(history, (code, i), window)
            if sequence is None or sequence.shape != (window, 8) or not np.isfinite(sequence).all():
                continue
            samples.append((code, i, _day(current),
                            (100 * (opening / reference - 1), 100 * (close / reference - 1))))
    return samples


def _standardizer(history, samples, indices, window):
    # Fit on training endpoints only. These are historical observations available
    # inside the training split; no validation/test statistics enter the fit.
    total = np.zeros(8, dtype=np.float64)
    square = np.zeros(8, dtype=np.float64)
    for index in indices:
        row = _sequence(history, samples[index], window)[-1].astype(np.float64)
        total += row
        square += row * row
    mean = total / len(indices)
    scale = np.sqrt(np.maximum(square / len(indices) - mean * mean, 0))
    scale[scale < 1e-6] = 1.0
    return mean.astype(np.float32), scale.astype(np.float32)


def _summaries(history, samples, indices, window):
    # Ridge sees a compact last/mean/std summary, not a huge N x 60 x F matrix.
    rows = []
    for index in indices:
        x = _sequence(history, samples[index], window)
        rows.append(np.concatenate([x[-1], x.mean(axis=0), x.std(axis=0)]))
    return np.asarray(rows, dtype=np.float32)


def build_network(kind, feature_count=8, hidden=32, layers=2, dropout=.1):
    """Construct small models lazily; plain checkpoint state dictionaries only."""
    import torch
    from torch import nn

    class GRU(nn.Module):
        def __init__(self):
            super().__init__()
            self.rnn = nn.GRU(feature_count, hidden, layers, batch_first=True,
                              dropout=dropout if layers > 1 else 0)
            self.output = nn.Linear(hidden, 2)

        def forward(self, x):
            x, _ = self.rnn(x)
            return self.output(x[:, -1])

    class CausalBlock(nn.Module):
        def __init__(self, incoming, outgoing, dilation):
            super().__init__()
            self.padding = 2 * dilation
            self.first = nn.Conv1d(incoming, outgoing, 3, padding=self.padding, dilation=dilation)
            self.second = nn.Conv1d(outgoing, outgoing, 3, padding=self.padding, dilation=dilation)
            self.skip = nn.Conv1d(incoming, outgoing, 1) if incoming != outgoing else nn.Identity()
            self.drop = nn.Dropout(dropout)

        def forward(self, x):
            y = self.drop(torch.relu(self.first(x)[:, :, :-self.padding]))
            y = self.drop(torch.relu(self.second(y)[:, :, :-self.padding]))
            return torch.relu(y + self.skip(x))

    class TCN(nn.Module):
        def __init__(self):
            super().__init__()
            self.blocks = nn.Sequential(*[CausalBlock(feature_count if i == 0 else hidden, hidden, dilation)
                                           for i, dilation in enumerate([1, 2, 4, 8])])
            self.output = nn.Linear(hidden, 2)

        def forward(self, x):
            return self.output(self.blocks(x.transpose(1, 2))[:, :, -1])

    if kind not in {"gru", "tcn"}:
        raise ValueError("Unknown network kind")
    return GRU() if kind == "gru" else TCN()


def _torch_device(requested):
    import torch
    if requested == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"
    if requested == "cuda" and not torch.cuda.is_available():
        raise ValueError("CUDA was requested but is unavailable in this runtime")
    if requested == "mps" and not (hasattr(torch.backends, "mps") and torch.backends.mps.is_available()):
        raise ValueError("MPS was requested but is unavailable in this runtime")
    return requested


def _train_network(kind, history, samples, train, validation, mean, scale, args, device):
    import torch
    from torch.utils.data import DataLoader, Dataset

    class WindowDataset(Dataset):
        def __init__(self, indices):
            self.indices = indices

        def __len__(self):
            return len(self.indices)

        def __getitem__(self, index):
            item = samples[self.indices[index]]
            x = (_sequence(history, item, args.window) - mean) / scale
            return torch.from_numpy(x.astype(np.float32)), torch.tensor(item[3], dtype=torch.float32)

    torch.manual_seed(args.seed)
    if device == "cuda":
        torch.cuda.manual_seed_all(args.seed)
        torch.cuda.reset_peak_memory_stats()
    net = build_network(kind, hidden=args.hidden, layers=args.layers, dropout=args.dropout).to(device)
    generator = torch.Generator().manual_seed(args.seed)
    loader = DataLoader(WindowDataset(train), batch_size=args.batch_size, shuffle=True,
                        generator=generator, num_workers=0)
    validation_loader = DataLoader(WindowDataset(validation), batch_size=args.batch_size, shuffle=False, num_workers=0)
    optimizer = torch.optim.AdamW(net.parameters(), lr=args.learning_rate, weight_decay=1e-4)
    loss_fn = torch.nn.HuberLoss(delta=1.0)
    best_loss, best_state, waiting, curve = math.inf, None, 0, []
    started = time.monotonic()
    for epoch in range(args.epochs):
        net.train()
        total_loss, total_samples = 0.0, 0
        for x, y in loader:
            x, y = x.to(device), y.to(device)
            optimizer.zero_grad(set_to_none=True)
            loss = loss_fn(net(x), y)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(net.parameters(), 1.0)
            optimizer.step()
            total_loss += float(loss.detach()) * len(x)
            total_samples += len(x)
        net.eval()
        actual, predicted = [], []
        with torch.no_grad():
            for x, y in validation_loader:
                actual.append(y.numpy())
                predicted.append(net(x.to(device)).cpu().numpy())
        metrics = _metrics(np.concatenate(actual), np.concatenate(predicted))
        score = metrics["mean_mae_pct"]
        curve.append({"epoch": epoch + 1, "train_huber": total_loss / total_samples,
                      "validation_mae_pct": score})
        print(json.dumps({"model": kind, **curve[-1]}), flush=True)
        if score < best_loss - 1e-6:
            best_loss = score
            best_state = {name: tensor.detach().cpu().clone() for name, tensor in net.state_dict().items()}
            waiting = 0
        else:
            waiting += 1
            if waiting >= args.patience:
                break
    net.load_state_dict(best_state)
    net.eval()
    with torch.no_grad():
        predicted = np.concatenate([net(x.to(device)).cpu().numpy() for x, _ in validation_loader])
    return net, {"validation": _metrics(np.asarray([samples[i][3] for i in validation]), predicted),
                 "epochs_completed": len(curve), "curve": curve,
                 "parameters": sum(p.numel() for p in net.parameters()),
                 "elapsed_seconds": round(time.monotonic() - started, 3),
                 "peak_cuda_allocated_bytes": int(torch.cuda.max_memory_allocated()) if device == "cuda" else None}


def load_neural_bundle(path, device="cpu"):
    """Load a locally trusted candidate checkpoint. This does not promote it."""
    import torch
    payload = torch.load(Path(path), map_location="cpu", weights_only=True)
    if payload.get("format_version") != FORMAT_VERSION or payload.get("target_unit") != "percent":
        raise ValueError("Unsupported equity checkpoint format")
    chosen = _torch_device(device)
    net = build_network(payload["kind"], **payload["architecture"])
    net.load_state_dict(payload["state_dict"])
    net.to(chosen).eval()
    return {"network": net, "device": chosen, "mean": np.asarray(payload["mean"], dtype=np.float32),
            "scale": np.asarray(payload["scale"], dtype=np.float32), "window": payload["window"],
            "target_names": payload["target_names"], "metadata": payload["metadata"]}


def predict_neural_samples(bundle, sequences, batch_size=128):
    """Return N x 2 percentages for explicitly supplied same-feature windows."""
    import torch
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    values = np.asarray(sequences, dtype=np.float32)
    if values.ndim != 3 or values.shape[1:] != (bundle["window"], len(bundle["mean"])):
        raise ValueError("Expected N x window x feature_count sequences")
    if not np.isfinite(values).all():
        raise ValueError("Sequence contains nonfinite values")
    if not len(values):
        return np.empty((0, 2), dtype=np.float32)
    predictions = []
    with torch.no_grad():
        for start in range(0, len(values), batch_size):
            x = (values[start:start + batch_size] - bundle["mean"]) / bundle["scale"]
            predictions.append(bundle["network"](torch.from_numpy(x).to(bundle["device"])).cpu().numpy())
    return np.concatenate(predictions)


def run_training(args):
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from .equity_model import chronological_split, load_history

    random.seed(args.seed)
    np.random.seed(args.seed)
    started = time.monotonic()
    history = load_history(args.history_dir)
    available_stocks = len(history)
    calendar_dates = sorted({_day(row) for bars in history.values() for row in bars})
    if args.max_stocks:
        history = {code: history[code] for code in sorted(history)[:args.max_stocks]}
    history = _SequenceHistory(history)
    samples = _sample_index(history, args.window, calendar_dates)
    output = Path(args.model_dir)
    output.mkdir(parents=True, exist_ok=True)
    report = {"format_version": FORMAT_VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
              "status": "insufficient_data", "promoted": False, "eligible_for_promotion": False,
              "experiment_status": "bounded_pilot" if args.max_stocks else "offline_research",
              "production_unchanged": True, "target_names": TARGET_NAMES, "target_unit": "percent",
              "labels": "100*(next.open_or_close/next.pre_close-1)",
              "samples": len(samples), "stocks": len({s[0] for s in samples}),
              "archive_stocks": available_stocks, "selected_stocks": len(history),
              "selection": "first_sorted_codes_limited_pilot" if args.max_stocks else "all_archive_stocks",
              "parameters": vars(args), "models": {}, "warnings": [],
              "universe_caveat": "Archive may contain current-universe survivorship bias; historical membership is not inferred.",
              "promotion_policy": "Research only: validation and confirmation gates do not automatically change production."}

    def save():
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        # Atomic report replacement; checkpoints below are separate run artifacts.
        temporary = output / "training_report.json.tmp"
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(output / "training_report.json")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(output / "training-report.json")
        return report

    if not samples:
        report["warnings"].append("No valid rolling sequences and next-session labels")
        return save()
    split = chronological_split([s[2] for s in samples], min_train_dates=args.min_train_dates,
                                min_validation_dates=args.validation_dates, min_test_dates=args.test_dates, gap=1)
    if split is None:
        report["warnings"].append("Not enough distinct dates for train, validation, test and two one-session gaps")
        return save()
    indices = {name: np.flatnonzero(split[name + "_mask"]) for name in ("train", "validation", "test")}
    if any(not len(value) for value in indices.values()):
        report["warnings"].append("Empty chronological split")
        return save()
    report["split"] = {name: {"samples": len(items), "start": min(samples[i][2] for i in items),
                              "end": max(samples[i][2] for i in items),
                              "dates": len({samples[i][2] for i in items})} for name, items in indices.items()}
    report["split"]["gap_sessions"] = 1
    actual = {name: np.asarray([samples[i][3] for i in items], dtype=np.float32) for name, items in indices.items()}
    compact = {name: _summaries(history, samples, items, args.window) for name, items in indices.items()}
    ridge = make_pipeline(StandardScaler(), Ridge(alpha=10.0))
    ridge.fit(compact["train"], actual["train"])
    report["models"]["zero"] = {"validation": _metrics(actual["validation"], np.zeros_like(actual["validation"]))}
    report["models"]["ridge"] = {"validation": _metrics(actual["validation"], ridge.predict(compact["validation"])),
                                    "alpha": 10.0, "features": "sequence_last_mean_std_24"}
    requested = [name.strip() for name in args.models.split(",") if name.strip()]
    unknown = set(requested) - {"ridge", "gru", "tcn"}
    if unknown:
        raise ValueError("Unknown models: " + ", ".join(sorted(unknown)))
    neural_requested = [name for name in requested if name != "ridge"]
    candidates = {}
    report["device"] = "cpu"
    mean, scale = _standardizer(history, samples, indices["train"], args.window)
    if neural_requested:
        try:
            import torch
        except ImportError:
            report["warnings"].append("PyTorch is not installed; only zero and Ridge baselines ran. Install requirements-equity.txt for GRU/TCN.")
        else:
            device = _torch_device(args.device)
            report["device"] = device
            report["runtime_device_name"] = torch.cuda.get_device_name() if device == "cuda" else "current runtime " + device
            torch.set_num_threads(args.cpu_threads)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            for kind in neural_requested:
                net, result = _train_network(kind, history, samples, indices["train"], indices["validation"], mean, scale, args, device)
                checkpoint = output / (kind + "_" + stamp + ".pt")
                payload = {"format_version": FORMAT_VERSION, "kind": kind, "window": args.window,
                           "architecture": {"feature_count": 8, "hidden": args.hidden, "layers": args.layers, "dropout": args.dropout},
                           "target_names": TARGET_NAMES, "target_unit": "percent", "mean": mean.tolist(), "scale": scale.tolist(),
                           "state_dict": {name: tensor.detach().cpu() for name, tensor in net.state_dict().items()},
                           "metadata": {"created_at": report["created_at"], "split": report["split"], "seed": args.seed,
                                        "promoted": False, "train_only_scaler": True, "validation": result["validation"]}}
                torch.save(payload, checkpoint)
                result["checkpoint"] = str(checkpoint)
                result["promoted"] = False
                report["models"][kind] = result
                candidates[kind] = {"network": net, "device": device, "mean": mean, "scale": scale,
                                    "window": args.window, "metadata": payload["metadata"]}
    winner = min(report["models"], key=lambda name: report["models"][name]["validation"]["mean_mae_pct"])
    report["selected_by_validation"] = winner
    # Inspect the untouched confirmation period only after validation selection.
    report["confirmation"] = {
        "zero": _metrics(actual["test"], np.zeros_like(actual["test"])),
        "ridge": _metrics(actual["test"], ridge.predict(compact["test"]))}
    if winner in candidates:
        predictions = []
        for start in range(0, len(indices["test"]), args.batch_size):
            batch = indices["test"][start:start + args.batch_size]
            sequences = np.asarray([_sequence(history, samples[i], args.window) for i in batch])
            predictions.append(predict_neural_samples(candidates[winner], sequences, args.batch_size))
        report["confirmation"][winner] = _metrics(actual["test"], np.concatenate(predictions))
        report["eligible_for_promotion"] = all(
            report["models"][winner]["validation"][target]["mae_pct"] <
            min(report["models"][base]["validation"][target]["mae_pct"] for base in ("zero", "ridge")) * .98
            and report["confirmation"][winner][target]["mae_pct"] <
            min(report["confirmation"][base][target]["mae_pct"] for base in ("zero", "ridge")) * .98
            for target in TARGET_NAMES)
    report["status"] = "trained_candidates" if candidates else "trained_baselines"
    report["warnings"].append("A single chronological split is a pilot, not robust multi-regime evidence; no expected investment return is claimed.")
    return save()


def main(argv=None):
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--history-dir", default="data/equity/daily")
    parser.add_argument("--model-dir", default="data/equity/models")
    parser.add_argument("--models", default="ridge,gru,tcn")
    parser.add_argument("--max-stocks", type=int, default=200, help="0 selects all archived stocks; default is a bounded pilot")
    parser.add_argument("--epochs", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--hidden", type=int, choices=[32, 64], default=32)
    parser.add_argument("--layers", type=int, choices=[1, 2], default=2)
    parser.add_argument("--dropout", type=float, default=.1)
    parser.add_argument("--window", type=int, default=60)
    parser.add_argument("--learning-rate", type=float, default=3e-4)
    parser.add_argument("--patience", type=int, default=4)
    parser.add_argument("--min-train-dates", type=int, default=60)
    parser.add_argument("--validation-dates", type=int, default=20)
    parser.add_argument("--test-dates", type=int, default=20)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--device", choices=["auto", "cpu", "cuda", "mps"], default="auto")
    parser.add_argument("--cpu-threads", type=int, default=4)
    args = parser.parse_args(argv)
    if min(args.epochs, args.batch_size, args.patience, args.min_train_dates, args.validation_dates, args.test_dates, args.cpu_threads) < 1:
        parser.error("Counts must be positive")
    if args.max_stocks < 0 or args.window < 2 or not 0 <= args.dropout < 1 or args.learning_rate <= 0:
        parser.error("Invalid model configuration")
    report = run_training(args)
    print(json.dumps({"status": report["status"], "report": str(Path(args.model_dir) / "training_report.json"),
                      "device": report.get("device"), "samples": report["samples"],
                      "promoted": report["promoted"], "selected_by_validation": report.get("selected_by_validation")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
