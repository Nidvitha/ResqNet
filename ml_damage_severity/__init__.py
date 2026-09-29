"""Standalone transfer-learning module for CrisisMMD damage severity."""

__all__ = ["predict_image"]


def predict_image(*args, **kwargs):
	from .predict import predict_image as _predict_image

	return _predict_image(*args, **kwargs)