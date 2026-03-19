import os
"""
Hive Memory Schema
Canonical field definitions for the 4-agent hive memory store.
All agents that write to LanceDB should use these constants.

Sprint 5: Added controlled vocabulary tag fields.
"""

from dataclasses import dataclass, field
from typing import Optional
import time

LAYERS = ["genome", "hive", "private"]
AGENTS = ["thea", "athena", "iris", "guru", "pythagoras", "forge", "luma", "two", "three", "collective", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""]
DEFAULT_SCORE = None  # null until first scoring event; 0.5 is mid-baseline, not a valid initial state
DEFAULT_THRESHOLD = 0.3

LANCEDB_PATH = os.path.expanduser("/home/qtxit/.openclaw/shared/memory/lancedb")
TABLE_NAME   = "hybrid_facts"
# Embedder now uses the local MiniLM model to keep everything on box
EMBED_MODEL  = "all-MiniLM-L6-v2"
EMBED_DIM    = 384

# Sprint 5: Controlled vocabulary tag namespaces
TAG_DOMAINS  = ["ops", "comms", "supply-chain", "math", "cross-domain"]
TAG_TYPES    = ["fix", "rubric", "fact", "insight", "decision", "procedure"]
TAG_SOURCES  = ["session", "kb", "paper", "external", "inferred"]
TAG_STATUSES = ["active", "under-review", "superseded", "provisional"]

# Surfacing threshold overrides by type
FIX_THRESHOLD_OVERRIDE = 0.45   # type:fix surfaces more readily (saves the most time)
SUPERSEDED_THRESHOLD   = 0.95   # type:superseded effectively suppressed

# Staleness windows (days) by tag_source
STALENESS_WINDOWS = {
    "external": 60,
    "session":  90,
    "kb":       180,
    "paper":    180,
    "inferred": 30,
    None:       90,  # default
}


@dataclass
class HiveMemory:
    # Core content
    text: str
    vector: list  # embedding vector (384-dim MiniLM)

    # Hive fields
    layer: str = "hive"                          # genome | hive | private
    owner_agent: str = "thea"                    # which agent owns this memory
    score: float = DEFAULT_SCORE                 # activation score (0.0-1.0)
    family_id: str = ""                          # cluster family (Phase 3)
    activation_threshold: float = DEFAULT_THRESHOLD

    # Provenance
    source: str = ""                             # file path or session ID
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # Standard fields (inherited from original schema)
    id: str = field(default_factory=lambda: __import__('uuid').uuid4().__str__())
    decay_class: str = "permanent"

    # Sprint 5: Controlled vocabulary tags
    tag_domain: str = ""                         # ops | comms | supply-chain | math | cross-domain
    tag_type: str = ""                           # fix | rubric | fact | insight | decision | procedure
    tag_source: str = ""                         # session | kb | paper | external | inferred
    tag_status: str = "active"                   # active | under-review | superseded | provisional
    superseded_by: str = ""                      # memory_id of replacement (only when tag_status=superseded)
    surfacing_threshold_override: float = 0.0    # 0.0 = use default; set to FIX_THRESHOLD_OVERRIDE for type:fix

    def to_dict(self) -> dict:
        return self.__dict__

    def validate(self):
        assert self.layer in LAYERS, f"Invalid layer: {self.layer}"
        assert self.owner_agent in AGENTS, f"Invalid agent: {self.owner_agent}"
        if self.score is not None:
            assert 0.0 <= self.score <= 1.0, f"Score out of range: {self.score}"
        assert 0.0 <= self.activation_threshold <= 1.0, \
            f"Threshold out of range: {self.activation_threshold}"
        if self.tag_domain:
            assert self.tag_domain in TAG_DOMAINS, f"Invalid tag_domain: {self.tag_domain}"
        if self.tag_type:
            assert self.tag_type in TAG_TYPES, f"Invalid tag_type: {self.tag_type}"
        if self.tag_source:
            assert self.tag_source in TAG_SOURCES, f"Invalid tag_source: {self.tag_source}"
        if self.tag_status:
            assert self.tag_status in TAG_STATUSES, f"Invalid tag_status: {self.tag_status}"
        if self.tag_status == "superseded" and not self.superseded_by:
            raise AssertionError("status=superseded requires superseded_by field")
