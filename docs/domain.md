# 领域约定

描述游泳多项目报名、恢复占用、接力让渡和检录锁定的事件。

聚合对象包括`event_entry`、`load_window`、`relay_roster`、`checkin_revision`。事件类型包括`ENTRY_REGISTERED`、`LOAD_RECORDED`、`MEDICAL_HOLD_SET`、`ROSTER_SUBSTITUTED`、`CHECKIN_LOCKED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `LOAD_RECORDED`：载荷还需包含 `source_ref`, `load_value`。
- `ROSTER_SUBSTITUTED`：载荷还需包含 `replaced_athlete`, `replacement`。
- `CHECKIN_LOCKED`：载荷还需包含 `roster_version`, `locked_at`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。
