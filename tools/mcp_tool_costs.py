"""Declared per-tool MCP costs and the per-chat spend tally behind the budget gate.

A server may declare what its tools spend (``tool_costs`` in config.yaml, ``costs`` in a portable
package) and a ``spend_limit`` per chat. The approval prompt then names the cost and the chat's
running total, and a call that would take the chat past the limit needs an explicit yes even on a
``trust: full`` server. Figures are the server's own declared estimates: Hermes counts the declared
figure (the top of a range) for every call it dispatched, never a vendor's bill.

Undeclared and zero-cost tools never reach any of this, so a server without a table behaves
exactly as before.
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Mapping, Optional

DEFAULT_UNIT = "credits"
# Native ``mcp_servers.<name>`` keys; a portable package's ``costs`` maps to ``tool_costs``.
CONFIG_KEYS = ("tool_costs", "spend_limit", "cost_unit")


@dataclass(frozen=True)
class ToolCost:
    low: float
    high: float
    note: str = ""


@dataclass(frozen=True)
class CostPolicy:
    costs: Mapping[str, ToolCost]
    limit: float  # 0 = no limit
    unit: str


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{where} must be a non-negative number, not {value!r}")
    return float(value)


def _parse_cost(value: Any, where: str) -> ToolCost:
    note = ""
    if isinstance(value, dict):
        if "credits" not in value or set(value) - {"credits", "note"}:
            raise ValueError(f"{where} must be a number, [min, max], or {{\"credits\": ..., \"note\": \"...\"}}")
        note = value.get("note", "")
        if not isinstance(note, str):
            raise ValueError(f"{where} note must be a string")
        value = value["credits"]
    if isinstance(value, (list, tuple)):
        if len(value) != 2:
            raise ValueError(f"{where} range must be [min, max]")
        low, high = _number(value[0], where), _number(value[1], where)
        if low > high:
            raise ValueError(f"{where} range must be [min, max] with min <= max")
        return ToolCost(low, high, note.strip())
    amount = _number(value, where)
    return ToolCost(amount, amount, note.strip())


def parse_cost_policy(costs: Any = None, spend_limit: Any = None, cost_unit: Any = None, *,
                      where: str) -> Optional[CostPolicy]:
    """Validate one server's declaration. None when it declares no tool cost (a limit alone has
    nothing to count); raises ValueError naming the bad key otherwise, never a silent pass."""
    if costs is not None and not isinstance(costs, dict):
        raise ValueError(f"{where} costs must be an object mapping MCP tool names to costs")
    table: dict[str, ToolCost] = {}
    for tool, value in (costs or {}).items():
        if not isinstance(tool, str) or not tool:
            raise ValueError(f"{where} costs keys must be non-empty MCP tool names")
        table[tool] = _parse_cost(value, f"{where} cost of '{tool}'")
    limit = 0.0 if spend_limit is None else _number(spend_limit, f"{where} spend_limit")
    unit = DEFAULT_UNIT if cost_unit is None else cost_unit
    if not isinstance(unit, str) or not unit.strip() or len(unit) > 32:
        raise ValueError(f"{where} cost_unit must be a short non-empty label, not {cost_unit!r}")
    if not any(cost.high > 0 for cost in table.values()):
        return None
    return CostPolicy(table, limit, unit.strip())


def config_cost_policy(server_name: str, config: Mapping[str, Any] | None) -> Optional[CostPolicy]:
    """Policy of a native/translated ``mcp_servers`` entry (raises ValueError when malformed)."""
    config = config or {}
    return parse_cost_policy(config.get("tool_costs"), config.get("spend_limit"), config.get("cost_unit"),
                             where=f"MCP server '{server_name}'")


def _fmt(amount: float) -> str:
    return f"{int(amount):,}" if float(amount).is_integer() else f"{amount:,.2f}".rstrip("0").rstrip(".")


# Spend per (policy key, approval session key): the policy key carries the profile scope under a
# multiplexer and the session key is the chat the approval system already isolates. In memory only.
_lock = threading.Lock()
_spent: OrderedDict[tuple, float] = OrderedDict()
_MAX_TALLIES = 16384  # bounds a long-lived gateway; the least recently charged chat falls off first


@dataclass(frozen=True)
class Charge:
    """One costed call about to be admitted: what it costs and whose tally it lands on."""
    key: tuple
    server: str
    tool: str
    cost: ToolCost
    policy: CostPolicy

    def spent(self) -> float:
        with _lock:
            return _spent.get(self.key, 0.0)

    def over_limit(self, spent: float) -> bool:
        return self.policy.limit > 0 and spent + self.cost.high > self.policy.limit

    def reserve_within_limit(self) -> tuple[bool, float]:
        """Book the estimate when it keeps the chat within its limit; ``(booked, spent before)``.
        Check and book share the lock, so parallel calls cannot both slip under the limit."""
        with _lock:
            spent = _spent.get(self.key, 0.0)
            if self.over_limit(spent):
                return False, spent
            self._add_locked(self.cost.high)
            return True, spent

    def book(self) -> None:
        with _lock:
            self._add_locked(self.cost.high)

    def refund(self) -> None:
        with _lock:
            self._add_locked(-self.cost.high)

    def _add_locked(self, amount: float) -> None:
        _spent[self.key] = max(0.0, _spent.get(self.key, 0.0) + amount)
        _spent.move_to_end(self.key)
        while len(_spent) > _MAX_TALLIES:
            _spent.popitem(last=False)

    def cost_sentence(self) -> str:
        unit, cost = self.policy.unit, self.cost
        text = (f"Costs about {_fmt(cost.high)} {unit}" if cost.low == cost.high
                else f"Costs {_fmt(cost.low)}-{_fmt(cost.high)} {unit}")
        return f"{text} ({cost.note})." if cost.note else f"{text}."

    def spend_sentence(self, spent: float) -> str:
        unit, limit = self.policy.unit, self.policy.limit
        if spent > 0:
            cap = f" (limit {_fmt(limit)})" if limit > 0 else ""
            return f"This chat has used about {_fmt(spent)} {unit} on '{self.server}'{cap}."
        return f"Spend limit for this chat on '{self.server}': {_fmt(limit)} {unit}." if limit > 0 else ""

    def lines(self, spent: float) -> list[str]:
        return [s for s in (self.cost_sentence(), self.spend_sentence(spent)) if s]

    def refusal(self, spent: float) -> str:
        unit = self.policy.unit
        return (f"MCP tool '{self.tool}' on server '{self.server}' was NOT run: this chat has used about "
                f"{_fmt(spent)} {unit} on '{self.server}' and its spend limit is {_fmt(self.policy.limit)} {unit}. "
                f"This call (about {_fmt(self.cost.high)} {unit}) needed an explicit approval and did not get "
                f"one. Do not retry; tell the user the spend limit was reached.")


def pending_charge(policy_key: Any, server_name: str, tool_name: str, session_key: str) -> Optional[Charge]:
    """The charge a call to *tool_name* would add, or None for an undeclared/zero-cost tool."""
    from tools.mcp_tool_common import _core
    policy = _core._server_cost_policies.get(policy_key)
    if policy is None:
        return None
    cost = policy.costs.get(tool_name)
    if cost is None or cost.high <= 0:
        return None
    return Charge((policy_key, session_key), server_name, tool_name, cost, policy)
