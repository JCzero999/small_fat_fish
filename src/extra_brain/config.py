from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MemoryConfig:
    """Tunable limits and scoring parameters from the architecture specification."""

    core_token_limit: int = 1200
    injection_token_limit: int = 1800
    decay_lambda: float = 0.06
    archive_decay_threshold: float = 0.05
    archive_after_days: int = 90
    min_importance: float = 0.20
    min_retrieval_score: float = 0.25
    semantic_top_k: int = 5
    episodic_top_k: int = 2
    retrieval_candidate_limit: int = 50
    recency_half_life_days: float = 30.0
    promotion_importance: float = 0.85
    promotion_activations: int = 3
    embedding_dimensions: int = 384

    def __post_init__(self) -> None:
        positive_ints = {
            "core_token_limit": self.core_token_limit,
            "injection_token_limit": self.injection_token_limit,
            "archive_after_days": self.archive_after_days,
            "semantic_top_k": self.semantic_top_k,
            "episodic_top_k": self.episodic_top_k,
            "retrieval_candidate_limit": self.retrieval_candidate_limit,
            "promotion_activations": self.promotion_activations,
            "embedding_dimensions": self.embedding_dimensions,
        }
        for name, value in positive_ints.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

        unit_intervals = {
            "archive_decay_threshold": self.archive_decay_threshold,
            "min_importance": self.min_importance,
            "min_retrieval_score": self.min_retrieval_score,
            "promotion_importance": self.promotion_importance,
        }
        for name, value in unit_intervals.items():
            if isinstance(value, bool) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")

        positive_numbers = {
            "decay_lambda": self.decay_lambda,
            "recency_half_life_days": self.recency_half_life_days,
        }
        for name, value in positive_numbers.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be positive")
