"""Main training loop."""

from ..extraction import Extractor, ExtractionConfig
from ..utils import select_usable_gpus
from .preprocessing import normalize
from .reporter import OptimConfig, Reporter, ReporterConfig
from dataclasses import dataclass
from datasets import DatasetDict
from functools import partial
from pathlib import Path
from simple_parsing import Serializable
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, roc_auc_score
from torch import Tensor
from tqdm.auto import tqdm
from typing import cast, Literal
import csv
import numpy as np
import os
import pickle
import random
import torch
import torch.multiprocessing as mp


@dataclass
class RunConfig(Serializable):
    """Full specification of a reporter training run.

    Args:
        data: Config specifying hidden states on which the reporter will be trained.
        net: Config for building the reporter network.
        optim: Config for the `.fit()` loop.
    """

    data: ExtractionConfig
    net: ReporterConfig
    optim: OptimConfig

    label_frac: float = 0.0
    max_gpus: int = -1
    normalization: Literal["legacy", "elementwise", "meanonly"] = "meanonly"
    skip_baseline: bool = False


def train_reporter(
    cfg: RunConfig,
    dataset: DatasetDict,
    out_dir: Path,
    layer: int,
    devices: list[str],
    world_size: int = 1,
):
    """Train a single reporter on a single layer."""

    # Reproducibility
    seed = cfg.net.seed + layer
    np.random.seed(seed)
    random.seed(seed)
    torch.manual_seed(seed)

    rank = os.getpid() % world_size
    device = devices[rank]

    # Note: currently we're just upcasting to float32 so we don't have to deal with
    # grad scaling (which isn't supported for LBFGS), while the hidden states are
    # saved in float16 to save disk space. In the future we could try to use mixed
    # precision training in at least some cases.
    with dataset.formatted_as("torch", device=device):
        train, val = dataset["train"], dataset.get("validation") or dataset["test"]
        train_labels = cast(Tensor, train["label"])
        val_labels = cast(Tensor, val["label"])

        x0, x1 = cast(Tensor, train[f"hidden_{layer}"]).unbind(dim=-2)
        val_x0, val_x1 = cast(Tensor, val[f"hidden_{layer}"]).unbind(dim=-2)

    reporter = Reporter(x0.shape[-1], cfg.net, device=device)
    if cfg.label_frac:
        num_labels = round(cfg.label_frac * len(train_labels))
        labels = train_labels[:num_labels]
    else:
        labels = None

    train_loss = reporter.fit((x0, x1), labels, cfg.optim)
    val_result = reporter.score(
        (val_x0, val_x1),
        val_labels,
    )

    lr_dir = out_dir / "lr_models"
    reporter_dir = out_dir / "reporters"

    lr_dir.mkdir(parents=True, exist_ok=True)
    reporter_dir.mkdir(parents=True, exist_ok=True)
    stats = [layer, train_loss, *val_result]

    if not cfg.skip_baseline:
        train_labels_aug = torch.cat([train_labels, 1 - train_labels])
        val_labels_aug = torch.cat([val_labels, 1 - val_labels])

        # TODO: Once we implement cross-validation for CCS, we should benchmark
        # against LogisticRegressionCV here.
        X = torch.cat([x0, x1]).cpu()
        lr_model = LogisticRegression(max_iter=10_000)
        lr_model.fit(X.view(-1, X.shape[-1]), train_labels_aug)

        lr_preds = lr_model.predict_proba(torch.cat([val_x0, val_x1]).cpu())[:, 1]
        lr_acc = accuracy_score(val_labels_aug, lr_preds > 0.5)
        lr_auroc = roc_auc_score(val_labels_aug, lr_preds)

        stats += [lr_auroc, lr_acc]
        with open(lr_dir / f"layer_{layer}.pkl", "wb") as file:
            pickle.dump(lr_model, file)

    with open(reporter_dir / f"layer_{layer}.pt", "wb") as file:
        torch.save(reporter, file)

    return stats


def train(cfg: RunConfig, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)

    with open(out_dir / "cfg.yaml", "w") as f:
        cfg.dump_yaml(f)

    builder = Extractor(cfg.data, max_gpus=cfg.max_gpus)
    ds = builder.extract()

    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    # TODO: Re-implement this in a way that doesn't require loading all the hidden
    # states into memory at once.
    # train_hiddens, val_hiddens = normalize(
    #     train_hiddens, val_hiddens, cfg.normalization
    # )

    devices = [f"cuda:{i}" for i in builder.gpus] if builder.gpus else ["cpu"]
    num_devices = len(devices)

    cols = ["layer", "train_loss", "loss", "acc", "cal_acc", "auroc"]
    if not cfg.skip_baseline:
        cols += ["lr_auroc", "lr_acc"]

    layers = [
        int(feat[len("hidden_") :])
        for feat in ds["train"].features
        if feat.startswith("hidden_")
    ]
    # Train reporters for each layer in parallel
    with mp.Pool(num_devices) as pool, open(out_dir / "eval.csv", "w") as f:
        fn = partial(
            train_reporter, cfg, ds, out_dir, devices=devices, world_size=num_devices
        )
        writer = csv.writer(f)
        writer.writerow(cols)

        mapper = pool.imap_unordered if num_devices > 1 else map
        for i, *stats in tqdm(mapper(fn, layers), total=len(layers)):
            writer.writerow([i] + [f"{s:.4f}" for s in stats])
