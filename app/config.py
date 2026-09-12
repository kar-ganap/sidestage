"""Runtime configuration, read once at import.

Everything tunable lives here so the pipeline modules stay free of environment
lookups. See DECISIONS.md D-17 (model tiering) and D-31 (ephemeral database).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# override=True is deliberate. Without it, a variable already exported in the
# shell silently wins over .env — so a developer who rotates a key into .env
# keeps spending on the old one with no visible sign. The project's .env is the
# authority for the project. (.env.example therefore leaves ANTHROPIC_API_KEY
# commented out, so copying it cannot blank a working shell key.)
load_dotenv(override=True)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATIC_DIR = ROOT / "static"
FIXTURE_DIR = ROOT / "fixtures"


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    # --- models (D-17) -------------------------------------------------
    anthropic_api_key: str | None
    draft_model: str
    triage_model: str
    llm_mode: str  # "live" | "replay" | "auto"

    # --- latency budgets, milliseconds (DECISIONS.md § Latency) --------
    budget_triage_fast_ms: int
    budget_triage_escalated_ms: int
    budget_draft_ms: int
    budget_research_ms: int

    # --- domain thresholds --------------------------------------------
    comp_min_samples: int  # D-primer §5: never quote a bare comp
    comp_max_age_days: int
    stock_quantifier_floor: int  # below this, "plenty" is a violation
    authenticity_value_floor: float  # eBay Authenticity Guarantee, modeled

    # --- triage cascade -------------------------------------------------
    escalate_low: float  # below -> drop without an LLM call
    escalate_high: float  # above -> surface without an LLM call

    @property
    def use_live_llm(self) -> bool:
        if self.llm_mode == "replay":
            return False
        if self.llm_mode == "live":
            return True
        return bool(self.anthropic_api_key)


settings = Settings(
    anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
    draft_model=os.getenv("SIDESTAGE_DRAFT_MODEL", "claude-sonnet-5"),
    # Sonnet, not Haiku, and the reason is measured rather than assumed — see
    # DECISIONS.md D-17. Haiku's minimum cacheable prefix sits above our triage
    # prompt, so it pays full list price on every call while Sonnet pays 10% of
    # a larger list. Measured: $0.00098/call vs $0.00273, and marginally faster.
    triage_model=os.getenv("SIDESTAGE_TRIAGE_MODEL", "claude-sonnet-5"),
    llm_mode=os.getenv("SIDESTAGE_LLM_MODE", "auto"),
    budget_triage_fast_ms=_int("SIDESTAGE_BUDGET_TRIAGE_FAST_MS", 50),
    budget_triage_escalated_ms=_int("SIDESTAGE_BUDGET_TRIAGE_ESC_MS", 600),
    budget_draft_ms=_int("SIDESTAGE_BUDGET_DRAFT_MS", 1500),
    budget_research_ms=_int("SIDESTAGE_BUDGET_RESEARCH_MS", 2000),
    comp_min_samples=_int("SIDESTAGE_COMP_MIN_SAMPLES", 5),
    comp_max_age_days=_int("SIDESTAGE_COMP_MAX_AGE_DAYS", 90),
    stock_quantifier_floor=_int("SIDESTAGE_STOCK_QUANTIFIER_FLOOR", 10),
    authenticity_value_floor=float(os.getenv("SIDESTAGE_AUTH_FLOOR", "250")),
    escalate_low=float(os.getenv("SIDESTAGE_ESCALATE_LOW", "0.25")),
    escalate_high=float(os.getenv("SIDESTAGE_ESCALATE_HIGH", "0.75")),
)
