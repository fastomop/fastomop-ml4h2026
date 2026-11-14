"""The reference resource: the OHDSI/DARWIN gold cohort standards.

Loads index.rich.json (curated, logic-bearing descriptions) and, per cohort, returns the
clinical description text and the raw native CIRCE JSON. The CIRCE is passed straight to
circe-be for execution — no parsing, no subset restriction.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..settings import settings


@dataclass
class GoldCohort:
    id: str
    name: str
    description: str
    circe: Dict[str, Any]


class GoldSet:
    def __init__(self, root: Optional[str] = None):
        self.root = Path(root or settings.gold_root)
        if not self.root.exists():
            raise ValueError(f"GOLD_ROOT not found: {self.root}")
        self.index: List[Dict[str, Any]] = json.loads(
            (self.root / "index.rich.json").read_text(encoding="utf-8")
        )

    def _read_circe(self, entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        rel = entry.get("circeFile")
        if not rel or not (self.root / rel).exists():
            return None
        blob = json.loads((self.root / rel).read_text(encoding="utf-8"))
        # some files wrap the expression: {"circeDefinition": {...}}
        if isinstance(blob, dict) and "circeDefinition" in blob and "ConceptSets" not in blob:
            blob = blob["circeDefinition"]
        return blob

    def _read_description(self, entry: Dict[str, Any]) -> str:
        rel = entry.get("descriptionFile")
        if rel and (self.root / rel).exists():
            return (self.root / rel).read_text(encoding="utf-8").strip()
        return str(entry.get("description") or entry.get("name") or "").strip()

    def get(self, entry: Dict[str, Any]) -> Optional[GoldCohort]:
        circe = self._read_circe(entry)
        if circe is None:
            return None
        return GoldCohort(
            id=str(entry.get("id")),
            name=str(entry.get("name") or entry.get("id")),
            description=self._read_description(entry),
            circe=circe,
        )

    def all(self) -> List[GoldCohort]:
        out = []
        for e in self.index:
            if e.get("isDuplicate"):
                continue
            g = self.get(e)
            if g:
                out.append(g)
        return out

    def sample(self, n: int, seed: int = 7) -> List[GoldCohort]:
        cohorts = self.all()
        rng = random.Random(seed)
        rng.shuffle(cohorts)
        return cohorts[:n]

    def stratified(self, counts: Dict[str, int], seed: int = 7) -> List[GoldCohort]:
        """Select `counts[bucket]` cohorts from each structural-complexity bucket
        (SIMPLE/MODERATE/COMPLEX), shuffled deterministically. Raises if a bucket
        has fewer cohorts than requested."""
        from ..strata import complexity_bucket, BUCKETS
        pools: Dict[str, List[GoldCohort]] = {b: [] for b in BUCKETS}
        for g in self.all():
            pools[complexity_bucket(g.circe)].append(g)
        rng = random.Random(seed)
        out: List[GoldCohort] = []
        for bucket, n in counts.items():
            pool = pools.get(bucket, [])
            if len(pool) < n:
                raise ValueError(
                    f"stratum {bucket}: requested {n} but only {len(pool)} available")
            rng.shuffle(pool)
            out.extend(pool[:n])
        return out
