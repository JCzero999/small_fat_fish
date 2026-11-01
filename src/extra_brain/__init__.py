from .config import MemoryConfig
from .consolidation import ConsolidationReport, calculate_decay
from .embeddings import (
    EmbeddingProvider,
    HNSWIndex,
    LocalHashingEmbedder,
    MemoryIndexer,
    OpenAICompatibleEmbedder,
    VectorEntry,
    VectorIndex,
    build_vector_index,
    cosine_similarity,
)
from .engine import ExtraBrain
from .extraction import (
    ConversationMessage,
    ExtractionError,
    ExtractionResult,
    MemoryExtractor,
    OpenAICompatibleExtractor,
)
from .models import (
    ActiveThread,
    CoreMemory,
    CoreMemoryLimitError,
    EpisodicMemory,
    Identity,
    MemoryAssociation,
    MemoryValidationError,
    NarrativeLayer,
    ProactiveConfig,
    RelationshipModel,
    SemanticMemory,
    StyleMemory,
    UserSchema,
    UserStableProfile,
)
from .retrieval import Injection, MemoryRetriever, RetrievalResult, ScoredMemory
from .session import SessionTracker
from .storage import MemoryStore, WriteBatch

__all__ = [
    "ActiveThread",
    "ConsolidationReport",
    "ConversationMessage",
    "CoreMemory",
    "CoreMemoryLimitError",
    "EmbeddingProvider",
    "EpisodicMemory",
    "ExtraBrain",
    "ExtractionError",
    "ExtractionResult",
    "HNSWIndex",
    "Identity",
    "Injection",
    "LocalHashingEmbedder",
    "MemoryAssociation",
    "MemoryConfig",
    "MemoryExtractor",
    "MemoryIndexer",
    "MemoryRetriever",
    "MemoryStore",
    "MemoryValidationError",
    "NarrativeLayer",
    "OpenAICompatibleEmbedder",
    "OpenAICompatibleExtractor",
    "ProactiveConfig",
    "RelationshipModel",
    "RetrievalResult",
    "ScoredMemory",
    "SemanticMemory",
    "SessionTracker",
    "StyleMemory",
    "UserSchema",
    "UserStableProfile",
    "VectorEntry",
    "VectorIndex",
    "WriteBatch",
    "build_vector_index",
    "calculate_decay",
    "cosine_similarity",
]

__version__ = "0.5.0"
