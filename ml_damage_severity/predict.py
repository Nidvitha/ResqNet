"""Single-image inference for the standalone damage-severity model."""

from __future__ import annotations

import argparse
from pathlib import Path

import torch
from PIL import Image
from torchvision import models, transforms

from .train import CLASS_NAMES, DEFAULT_ARTIFACT_DIR, IMAGE_SIZE, MEAN, STD


class DamageSeverityPredictor:
    """CPU predictor that loads one checkpoint once and reuses it."""

    def __init__(self, model_path: str | Path | None = None) -> None:
        checkpoint_path = Path(model_path) if model_path else DEFAULT_ARTIFACT_DIR / "damage_severity_mobilenet_v3_small.pth"
        checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=True)
        self.model = models.mobilenet_v3_small(weights=None)
        self.model.classifier[3] = torch.nn.Linear(self.model.classifier[3].in_features, len(CLASS_NAMES))
        self.model.load_state_dict(checkpoint["model_state_dict"])
        self.model.eval()
        self.transform = transforms.Compose([transforms.Resize((IMAGE_SIZE, IMAGE_SIZE)), transforms.ToTensor(), transforms.Normalize(MEAN, STD)])

    def predict(self, image_path: str | Path) -> dict[str, str]:
        with Image.open(image_path) as image:
            tensor = self.transform(image.convert("RGB")).unsqueeze(0)
        with torch.no_grad():
            probabilities = torch.softmax(self.model(tensor), dim=1)[0]
        index = int(probabilities.argmax())
        return {"predicted_class": CLASS_NAMES[index], "confidence_percentage": f"{float(probabilities[index]) * 100:.2f}"}


def predict_image(image_path: str | Path, model_path: str | Path | None = None) -> dict[str, str]:
    predictor = DamageSeverityPredictor(model_path)
    return predictor.predict(image_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--model", type=Path, default=None)
    args = parser.parse_args()
    print(predict_image(args.image, args.model))


if __name__ == "__main__":
    main()