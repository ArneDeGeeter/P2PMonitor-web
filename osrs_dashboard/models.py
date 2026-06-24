from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Account:
    id: int
    username: str
    created_at: str
    state: str = 'running'
    notes: Optional[str] = None
    last_polled: Optional[str] = None
    total_level: Optional[int] = None
    total_xp: Optional[int] = None
    total_cost_gbp: float = 0.0
    total_cost_usd: float = 0.0
    total_cost_eur: float = 0.0
    total_cost_gp: float = 0.0
    last_bank_gp: Optional[int] = None


@dataclass
class SkillSnapshot:
    rank: int
    level: int
    xp: int


@dataclass
class Snapshot:
    id: int
    account_id: int
    polled_at: str
    skills: dict = field(default_factory=dict)  # skill_name -> SkillSnapshot


@dataclass
class Expense:
    id: int
    account_id: int
    recorded_at: str
    category: str
    currency: str
    amount: float
    date_of: str
    type: str = 'expense'
    notes: Optional[str] = None
