"""Train and evaluate the standalone CrisisMMD damage-severity classifier."""

from __future__ import annotations

import argparse
import csv
import json
import random
import time
from collections import Counter
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import torch
from PIL import Image
from sklearn.metrics import classification_report, confusion_matrix
from sklearn.model_selection import train_test_split
from torch import nn
from torch.utils.data import DataLoader, Dataset, WeightedRandomSampler
from torchvision import models, transforms

CLASS_NAMES = ["Minor", "Moderate", "Severe"]
IMAGE_SIZE = 224
SEED = 42
MEAN = [0.485, 0.456, 0.406]
STD = [0.229, 0.224, 0.225]
MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_CSV = MODULE_DIR.parent / "damage_dataset.csv"
DEFAULT_ARTIFACT_DIR = MODULE_DIR / "artifacts"


def seed_everything(seed: int = SEED) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


def load_manifest(csv_path: Path) -> list[dict[str, Any]]:
    with csv_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    required_columns = {"image_path", "label"}
    if not rows or not required_columns.issubset(rows[0]):
        raise ValueError(f"CSV must contain columns: {sorted(required_columns)}")
    paths = [row["image_path"] for row in rows]
    labels = [row["label"] for row in rows]
    if len(paths) != len(set(paths)):
        raise ValueError("Duplicate image paths detected; refusing to train.")
    if set(labels) != set(CLASS_NAMES):
        raise ValueError(f"Labels must be exactly {CLASS_NAMES}; found {sorted(set(labels))}")
    invalid = [path for path in paths if not Path(path).is_file()]
    if invalid:
        raise FileNotFoundError(f"{len(invalid)} image paths are missing; refusing to train.")
    return rows


def make_splits(rows: list[dict[str, Any]], seed: int) -> tuple[list[dict[str, Any]], ...]:
    labels = [row["label"] for row in rows]
    train_rows, holdout_rows = train_test_split(
        rows, test_size=0.30, random_state=seed, stratify=labels
    )
    holdout_labels = [row["label"] for row in holdout_rows]
    validation_rows, test_rows = train_test_split(
        holdout_rows, test_size=0.50, random_state=seed, stratify=holdout_labels
    )
    split_paths = [set(row["image_path"] for row in split) for split in (train_rows, validation_rows, test_rows)]
    if split_paths[0] & split_paths[1] or split_paths[0] & split_paths[2] or split_paths[1] & split_paths[2]:
        raise RuntimeError("Data leakage detected between splits.")
    return train_rows, validation_rows, test_rows


class DamageDataset(Dataset[tuple[torch.Tensor, int]]):
    def __init__(self, rows: list[dict[str, Any]], transform: transforms.Compose) -> None:
        self.rows = rows
        self.transform = transform
        self.label_to_index = {label: index for index, label in enumerate(CLASS_NAMES)}

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, index: int) -> tuple[torch.Tensor, int]:
        row = self.rows[index]
        with Image.open(row["image_path"]) as image:
            image = image.convert("RGB")
            return self.transform(image), self.label_to_index[row["label"]]


def build_model(num_classes: int = len(CLASS_NAMES)) -> models.MobileNetV3:
    model = models.mobilenet_v3_small(weights=models.MobileNet_V3_Small_Weights.DEFAULT)
    for parameter in model.features.parameters():
        parameter.requires_grad = False
    model.classifier[3] = nn.Linear(model.classifier[3].in_features, num_classes)
    return model


def run_epoch(
    model: nn.Module,
    loader: DataLoader[tuple[torch.Tensor, torch.Tensor]],
    criterion: nn.Module,
    device: torch.device,
    optimizer: torch.optim.Optimizer | None,
) -> tuple[float, float]:
    training = optimizer is not None
    model.train(training)
    total_loss = 0.0
    correct = 0
    total = 0
    context = torch.enable_grad() if training else torch.no_grad()
    with context:
        for images, labels in loader:
            images, labels = images.to(device), labels.to(device)
            if training:
                optimizer.zero_grad(set_to_none=True)
            logits = model(images)
            loss = criterion(logits, labels)
            if training:
                loss.backward()
                optimizer.step()
            total_loss += loss.item() * labels.size(0)
            correct += (logits.argmax(dim=1) == labels).sum().item()
            total += labels.size(0)
    return total_loss / total, correct / total


def save_confusion_matrix(matrix: np.ndarray, output_path: Path) -> None:
    figure, axis = plt.subplots(figsize=(6, 5))
    axis.imshow(matrix, interpolation="nearest", cmap="Blues")
    axis.set_title("Damage Severity Confusion Matrix")
    axis.set_xticks(range(len(CLASS_NAMES)), CLASS_NAMES)
    axis.set_yticks(range(len(CLASS_NAMES)), CLASS_NAMES)
    axis.set_xlabel("Predicted label")
    axis.set_ylabel("True label")
    threshold = matrix.max() / 2
    for row_index in range(matrix.shape[0]):
        for column_index in range(matrix.shape[1]):
            axis.text(column_index, row_index, matrix[row_index, column_index], ha="center", va="center", color="white" if matrix[row_index, column_index] > threshold else "black")
    figure.tight_layout()
    figure.savefig(output_path, dpi=160)
    plt.close(figure)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACT_DIR)
    parser.add_argument("--head-epochs", type=int, default=5)
    parser.add_argument("--fine-tune-epochs", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--balanced-sampler", action="store_true")
    args = parser.parse_args()

    seed_everything()
    start_time = time.perf_counter()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    rows = load_manifest(args.csv.resolve())
    train_rows, validation_rows, test_rows = make_splits(rows, SEED)
    train_transform = transforms.Compose([
        transforms.Resize((IMAGE_SIZE, IMAGE_SIZE)),
        transforms.RandomHorizontalFlip(),
        transforms.RandomRotation(10),
        transforms.ColorJitter(brightness=0.15, contrast=0.15, saturation=0.15),
        transforms.ToTensor(),
        transforms.Normalize(MEAN, STD),
    ])
    eval_transform = transforms.Compose([
        transforms.Resize((IMAGE_SIZE, IMAGE_SIZE)),
        transforms.ToTensor(),
        transforms.Normalize(MEAN, STD),
    ])
    train_dataset = DamageDataset(train_rows, train_transform)
    validation_dataset = DamageDataset(validation_rows, eval_transform)
    test_dataset = DamageDataset(test_rows, eval_transform)
    counts = Counter(row["label"] for row in train_rows)
    class_weights = torch.tensor([len(train_rows) / (len(CLASS_NAMES) * counts[label]) for label in CLASS_NAMES], dtype=torch.float32)
    if args.balanced_sampler:
        label_weights = {label: 1.0 / counts[label] for label in CLASS_NAMES}
        sample_weights = [label_weights[row["label"]] for row in train_rows]
        train_sampler = WeightedRandomSampler(sample_weights, len(sample_weights), replacement=True)
        train_loader = DataLoader(train_dataset, batch_size=args.batch_size, sampler=train_sampler, num_workers=0)
    else:
        train_loader = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True, num_workers=0)
    validation_loader = DataLoader(validation_dataset, batch_size=args.batch_size, shuffle=False, num_workers=0)
    test_loader = DataLoader(test_dataset, batch_size=args.batch_size, shuffle=False, num_workers=0)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = build_model().to(device)
    criterion = nn.CrossEntropyLoss() if args.balanced_sampler else nn.CrossEntropyLoss(weight=class_weights.to(device))
    history: list[dict[str, float | int | str]] = []

    def train_stage(stage: str, epochs: int, learning_rate: float) -> None:
        if epochs <= 0:
            return
        optimizer = torch.optim.AdamW((parameter for parameter in model.parameters() if parameter.requires_grad), lr=learning_rate, weight_decay=1e-4)
        best_validation_loss = float("inf")
        best_state: dict[str, torch.Tensor] | None = None
        for epoch in range(1, epochs + 1):
            train_loss, train_accuracy = run_epoch(model, train_loader, criterion, device, optimizer)
            validation_loss, validation_accuracy = run_epoch(model, validation_loader, criterion, device, None)
            history.append({"stage": stage, "epoch": epoch, "train_loss": train_loss, "train_accuracy": train_accuracy, "validation_loss": validation_loss, "validation_accuracy": validation_accuracy})
            print(f"{stage} epoch {epoch}/{epochs}: train_loss={train_loss:.4f} train_accuracy={train_accuracy:.4f} validation_loss={validation_loss:.4f} validation_accuracy={validation_accuracy:.4f}")
            if validation_loss < best_validation_loss:
                best_validation_loss = validation_loss
                best_state = {name: parameter.detach().cpu().clone() for name, parameter in model.state_dict().items()}
        if best_state is not None:
            model.load_state_dict(best_state)

    train_stage("head", args.head_epochs, 1e-3)
    for parameter in model.features[-2:].parameters():
        parameter.requires_grad = True
    train_stage("fine_tune", args.fine_tune_epochs, 1e-5)

    model.eval()
    predictions: list[int] = []
    targets: list[int] = []
    with torch.no_grad():
        for images, labels in test_loader:
            predictions.extend(model(images.to(device)).argmax(dim=1).cpu().tolist())
            targets.extend(labels.tolist())
    matrix = confusion_matrix(targets, predictions, labels=range(len(CLASS_NAMES)))
    report = classification_report(targets, predictions, labels=range(len(CLASS_NAMES)), target_names=CLASS_NAMES, output_dict=True, zero_division=0)
    test_accuracy = float(np.mean(np.array(targets) == np.array(predictions)))
    torch.save({"model_state_dict": model.state_dict(), "class_names": CLASS_NAMES, "image_size": IMAGE_SIZE, "mean": MEAN, "std": STD, "architecture": "mobilenet_v3_small"}, args.artifacts / "damage_severity_mobilenet_v3_small.pth")
    (args.artifacts / "classification_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (args.artifacts / "metrics.json").write_text(json.dumps({"test_accuracy": test_accuracy, "confusion_matrix": matrix.tolist(), "split_sizes": {"train": len(train_rows), "validation": len(validation_rows), "test": len(test_rows)}, "class_distribution_train": dict(counts), "class_weights": class_weights.tolist(), "sampling": "balanced_sampler" if args.balanced_sampler else "shuffle_with_weighted_loss", "device": str(device), "seed": SEED}, indent=2), encoding="utf-8")
    with (args.artifacts / "training_history.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["stage", "epoch", "train_loss", "train_accuracy", "validation_loss", "validation_accuracy"])
        writer.writeheader()
        writer.writerows(history)
    save_confusion_matrix(matrix, args.artifacts / "confusion_matrix.png")
    elapsed = time.perf_counter() - start_time
    print(json.dumps({"training_seconds": round(elapsed, 2), "test_accuracy": test_accuracy, "classification_report": report, "confusion_matrix": matrix.tolist(), "model": str((args.artifacts / "damage_severity_mobilenet_v3_small.pth").resolve())}, indent=2))


if __name__ == "__main__":
    main()