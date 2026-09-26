"""月饼营养核验库领域服务。"""
from .labels import LabelService
from .nutrition import DEFAULT_RULES, evaluate_label, normalize_label
from .service import DomainStore, ServiceError

__all__ = [
    "DomainStore",
    "LabelService",
    "ServiceError",
    "normalize_label",
    "evaluate_label",
    "DEFAULT_RULES",
]
