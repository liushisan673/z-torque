from enum import StrEnum


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"
    CANCELING = "CANCELING"
    # Compatibility name for callers written before Stage 08 finalized the contract.
    CANCEL_REQUESTED = "CANCELING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"


TERMINAL_STATES = {TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.TIMEOUT, TaskStatus.CANCELLED}

ALLOWED_TRANSITIONS = {
    TaskStatus.PENDING: {TaskStatus.QUEUED, TaskStatus.CANCELLED, TaskStatus.TIMEOUT},
    TaskStatus.QUEUED: {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.TIMEOUT},
    TaskStatus.RUNNING: {TaskStatus.QUEUED, TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.TIMEOUT, TaskStatus.WAITING_HUMAN, TaskStatus.CANCELING},
    TaskStatus.WAITING_HUMAN: {TaskStatus.QUEUED, TaskStatus.SUCCESS, TaskStatus.CANCELLED},
    TaskStatus.CANCELING: {TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.TIMEOUT},
}


def can_transition(source: TaskStatus, target: TaskStatus) -> bool:
    return target in ALLOWED_TRANSITIONS.get(source, set())
