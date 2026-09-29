# Damage severity ML module

This package is intentionally separate from Django. It trains a three-class
damage classifier from the verified root-level `damage_dataset.csv` manifest.

## Train

```powershell
.\.venv\Scripts\python.exe -m ml_damage_severity.train
```

The default run uses a pretrained MobileNetV3-Small, 224x224 RGB inputs,
stratified 70/15/15 splits, class-weighted cross-entropy, five classifier-head
epochs, and three final fine-tuning epochs. Outputs are written to `artifacts/`.

## Predict one image

```powershell
.\.venv\Scripts\python.exe -m ml_damage_severity.predict C:\path\to\image.jpg
```

The confidence is the softmax probability produced by the trained model, not
the CrisisMMD annotation confidence.