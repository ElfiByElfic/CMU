"""Registr asynchronních operací (schéma Operation v agent-api.yaml)."""

from __future__ import annotations

import uuid
from collections import OrderedDict

from .models import Operation, OperationStep, utcnow


class Operations:
    def __init__(self, keep: int = 200) -> None:
        self._ops: OrderedDict[str, Operation] = OrderedDict()
        self._keep = keep

    def begin(self, kind: str, target: str) -> Operation:
        op = Operation(id=uuid.uuid4().hex[:8], kind=kind, target=target)
        self._ops[op.id] = op
        while len(self._ops) > self._keep:
            self._ops.popitem(last=False)
        return op

    def step(self, op: Operation, target: str, status: str, message: str = "") -> None:
        new = OperationStep(target=target, status=status, message=message)  # type: ignore[arg-type]
        for i, s in enumerate(op.steps):
            if s.target == target:
                op.steps[i] = new
                return
        op.steps.append(new)

    def finish(self, op: Operation, ok: bool, message: str) -> None:
        op.status = "succeeded" if ok else "failed"
        op.message = message
        op.finishedAt = utcnow()

    def get(self, op_id: str) -> Operation | None:
        return self._ops.get(op_id)
