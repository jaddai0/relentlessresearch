#!/usr/bin/env python3
"""Harness gate: verify a generated image is a real image, not noise or a flat fill.

Usage: python3 image_noise_check.py <image.png>

Exit 0 = looks like a real image. Exit 1 = fails a check (noise, uniform,
missing, unreadable). Prints the measured statistics either way so the gate
log doubles as evidence.

Checks (all must pass):
  1. File exists and decodes.
  2. Not uniform: global per-channel std must exceed MIN_STD.
  3. Not noise: mean adjacent-pixel correlation must exceed MIN_NEIGHBOR_CORR.
     Natural/generated images are locally smooth (corr typically > 0.8);
     latent noise decoded through a VAE lands near zero.

Deliberately runs on numpy + PIL only, with the interpreter of whatever venv
invokes it, so the target project's environment can be used directly.
"""

from __future__ import annotations

import sys

import numpy as np
from PIL import Image

MIN_STD = 4.0
MIN_NEIGHBOR_CORR = 0.5


def neighbor_correlation(gray: np.ndarray) -> float:
    a = gray[:, :-1].ravel().astype(np.float64)
    b = gray[:, 1:].ravel().astype(np.float64)
    if a.std() < 1e-6 or b.std() < 1e-6:
        return 0.0
    horizontal = float(np.corrcoef(a, b)[0, 1])
    a = gray[:-1, :].ravel().astype(np.float64)
    b = gray[1:, :].ravel().astype(np.float64)
    if a.std() < 1e-6 or b.std() < 1e-6:
        return 0.0
    vertical = float(np.corrcoef(a, b)[0, 1])
    return (horizontal + vertical) / 2.0


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: image_noise_check.py <image>", file=sys.stderr)
        return 1
    try:
        image = Image.open(sys.argv[1]).convert("RGB")
    except Exception as exc:  # noqa: BLE001 - any decode failure is a gate failure
        print(f"FAIL: cannot open image: {exc}")
        return 1
    pixels = np.asarray(image, dtype=np.float64)
    stds = pixels.reshape(-1, 3).std(axis=0)
    gray = pixels.mean(axis=2)
    corr = neighbor_correlation(gray)
    print(f"size={image.size} channel_std={[round(float(s), 2) for s in stds]} neighbor_corr={corr:.4f}")
    if float(stds.max()) < MIN_STD:
        print(f"FAIL: image is (near-)uniform, max channel std {stds.max():.2f} < {MIN_STD}")
        return 1
    if corr < MIN_NEIGHBOR_CORR:
        print(f"FAIL: image looks like noise, neighbor correlation {corr:.4f} < {MIN_NEIGHBOR_CORR}")
        return 1
    print("PASS: image has structure (not noise, not uniform)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
