"""领域事件交换契约校验。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

_TYPE_CHECKERS = {
    "array": lambda value: isinstance(value, list),
    "object": lambda value: isinstance(value, Mapping),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "string": lambda value: isinstance(value, str),
}

_TYPE_MESSAGES = {
    "array": "字段必须是数组",
    "object": "字段必须是 JSON 对象",
    "number": "字段必须是数值",
    "integer": "字段必须是整数",
    "string": "字段必须是字符串",
}


@dataclass(frozen=True)
class ContractIssue:
    field: str
    code: str
    message: str


def _timezone_is_explicit(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def validate_event(payload: Any, schema: Mapping[str, Any]) -> list[ContractIssue]:
    """返回稳定排序的问题列表，不修改输入。"""
    if not isinstance(payload, Mapping):
        return [ContractIssue("$", "object_required", "事件必须是 JSON 对象")]
    issues: list[ContractIssue] = []
    for field in schema.get("required", []):
        if field not in payload:
            issues.append(ContractIssue(str(field), "required", "缺少必填字段"))
    for field in ("event_id", "event_type", "aggregate_type", "aggregate_id"):
        if field in payload and (not isinstance(payload[field], str) or not payload[field].strip()):
            issues.append(ContractIssue(field, "non_empty_string", "字段必须是非空字符串"))
    version = payload.get("version")
    if "version" in payload and (isinstance(version, bool) or not isinstance(version, int) or version < 1):
        issues.append(ContractIssue("version", "positive_integer", "版本必须是正整数"))
    occurred_at = payload.get("occurred_at")
    if "occurred_at" in payload and (not isinstance(occurred_at, str) or not _timezone_is_explicit(occurred_at)):
        issues.append(ContractIssue("occurred_at", "timezone_required", "发生时间必须包含时区"))
    properties = schema.get("properties", {})
    for field in ("event_type", "aggregate_type"):
        allowed = properties.get(field, {}).get("enum", [])
        value = payload.get(field)
        if isinstance(value, str) and allowed and value not in allowed:
            issues.append(ContractIssue(field, "unsupported_value", "字段值未在契约中登记"))
    event_type = payload.get("event_type")
    body = payload.get("payload")
    if "payload" in payload and not isinstance(body, Mapping):
        issues.append(ContractIssue("payload", "object_required", "事件载荷必须是 JSON 对象"))
    elif isinstance(event_type, str) and isinstance(body, Mapping):
        for field in schema.get("payload_required_by_event", {}).get(event_type, []):
            if field not in body:
                issues.append(ContractIssue(f"payload.{field}", "required", "事件载荷缺少必填字段"))
        for field in schema.get("payload_forbidden_by_event", {}).get(event_type, []):
            if field in body:
                issues.append(ContractIssue(f"payload.{field}", "forbidden", "该事件禁止携带此字段"))
        for field, allowed in schema.get("payload_enums_by_event", {}).get(event_type, {}).items():
            value = body.get(field)
            if isinstance(value, str) and value not in allowed:
                issues.append(ContractIssue(f"payload.{field}", "unsupported_value", "字段值未在契约中登记"))
        for field, expected_type in schema.get("payload_types_by_event", {}).get(event_type, {}).items():
            if field in body and not _TYPE_CHECKERS[expected_type](body[field]):
                issues.append(ContractIssue(f"payload.{field}", "type_mismatch", _TYPE_MESSAGES[expected_type]))
    return sorted(issues, key=lambda issue: (issue.field, issue.code))
