"""
MetaTrader 5 position supervisor rules (see ``metatrader_client.supervisor``).
"""

from typing import Optional

from .supervisor import (
    create_position_rule,
    list_position_rules,
    pause_position_rule,
    resume_position_rule,
    remove_position_rule,
)


class MT5Supervisor:
    """Create and manage the rules evaluated by the local position supervisor."""

    def __init__(self, connection):
        self._connection = connection

    def create_position_rule(self, *, operator: str, threshold: float, ticket: Optional[int] = None,
                             strategy_id: Optional[str] = None, symbol: Optional[str] = None,
                             metric: str = "pnl", action: str = "close"):
        return create_position_rule(self._connection, operator=operator, threshold=threshold, ticket=ticket,
                                    strategy_id=strategy_id, symbol=symbol, metric=metric, action=action)

    def list_position_rules(self, *, state: Optional[str] = None, include_events: bool = False):
        return list_position_rules(self._connection, state=state, include_events=include_events)

    def pause_position_rule(self, rule_id: str):
        return pause_position_rule(self._connection, rule_id)

    def resume_position_rule(self, rule_id: str):
        return resume_position_rule(self._connection, rule_id)

    def remove_position_rule(self, rule_id: str):
        return remove_position_rule(self._connection, rule_id)
