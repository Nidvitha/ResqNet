import pandas as pd
from pathlib import Path

# CHANGE THIS if your CrisisMMD folder is somewhere else
CRISIS_DIR = Path(r"C:\Users\user\Downloads\CrisisMMD_v2.0\CrisisMMD_v2.0")

# Where we'll save the clean dataset CSV
OUTPUT_FILE = Path("damage_dataset.csv")

# Labels we need
VALID_LABELS = {
    "little_or_no_damage": "Minor",
    "mild_damage": "Moderate",
    "severe_damage": "Severe",
}

records = []

annotations_dir = CRISIS_DIR / "annotations"

print("Reading annotation files...")

for file in annotations_dir.glob("*_final_data.tsv"):
    try:
        df = pd.read_csv(file, sep="\t")

        if "image_damage" not in df.columns or "image_path" not in df.columns:
            continue

        df = df[df["image_damage"].isin(VALID_LABELS.keys())].copy()

        for _, row in df.iterrows():
            records.append({
                "image_path": str(CRISIS_DIR / row["image_path"]),
                "original_label": row["image_damage"],
                "label": VALID_LABELS[row["image_damage"]],
                "annotation_confidence": row["image_damage_conf"],
            })

        print(f"Processed: {file.name} → {len(df)} images")

    except Exception as e:
        print(f"Could not process {file.name}: {e}")

# Create final dataframe
dataset = pd.DataFrame(records)

# Remove duplicate image paths
dataset = dataset.drop_duplicates(subset=["image_path"])

# Save
dataset.to_csv(OUTPUT_FILE, index=False)

print("\n====================================")
print("Dataset preparation complete!")
print("====================================")
print(f"Total images: {len(dataset)}")
print("\nClass distribution:")
print(dataset["label"].value_counts())
print(f"\nSaved to: {OUTPUT_FILE.resolve()}")