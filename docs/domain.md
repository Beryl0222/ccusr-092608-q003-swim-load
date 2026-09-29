# 领域约定

描述游泳多项目报名、实测分段、恢复占用、医疗限制、接力让渡、检录锁定、冻结推演与纪录申报的事件。

聚合对象包括 `event_entry`、`load_window`、`relay_roster`、`checkin_revision`、`plan_snapshot`、`lineup_plan`、`record_claim`。事件类型包括 `ENTRY_REGISTERED`、`SPLIT_RECORDED`、`LOAD_RECORDED`、`MEDICAL_HOLD_SET`、`MEDICAL_HOLD_LIFTED`、`COACH_DECISION_RECORDED`、`ROSTER_SUBSTITUTED`、`CHECKIN_LOCKED`、`SNAPSHOT_TAKEN`、`PLAN_DRAFTED`、`PLAN_CONFIRMED`、`PLAN_DISCARDED`、`RECORD_CLAIM_FILED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 对象与事件对应

- `event_entry`：个人项目的报名资格与轮次（`ENTRY_REGISTERED`），个人项目成绩或医疗观察到达时以 `SPLIT_RECORDED`、`MEDICAL_HOLD_SET/LIFTED` 记录；尚未锁定的安排由上层服务依据这些事实重算。
- `load_window`：`LOAD_RECORDED` 记录热身（`warmup`）或恢复（`recovery`）占用，载荷以 `window_kind` 区分。
- `relay_roster`：`ROSTER_SUBSTITUTED` 记录棒次换人，必须携带被替代者与替代者。
- `checkin_revision`：`CHECKIN_LOCKED` 在检录截止时形成独立名单版本，载荷携带 `roster_version` 与 `locked_at`；已锁定版本不随后续事件改写。
- `plan_snapshot`：`SNAPSHOT_TAKEN` 冻结当时的报名、名单与成绩引用（`entry_refs`/`roster_refs`/`result_refs`），换人推演只能基于快照进行。
- `lineup_plan`：`PLAN_DRAFTED` 在快照上给出方案及四项代价分量——相邻项目准备时间（`score_prep_time`）、恢复负荷（`score_recovery_load`）、替补可用性（`score_substitute_availability`）、纪录材料完整性（`score_record_material`）；`PLAN_CONFIRMED` 一次性占用 `reserved_windows` 并释放 `released_arrangements`；`PLAN_DISCARDED` 记录放弃原因，供赛后还原。
- `record_claim`：`RECORD_CLAIM_FILED` 申报纪录，必须引用锁定的 `roster_version`、`locked_revision` 与当时的 `result_ref`。

## 事件载荷

- `LOAD_RECORDED`：载荷还需包含 `source_ref`, `load_value`（数值）, `athlete_id`, `window_kind`, `session_id`。
- `ROSTER_SUBSTITUTED`：载荷还需包含 `replaced_athlete`, `replacement`, `relay_event_code`, `round`, `authority`。
- `CHECKIN_LOCKED`：载荷还需包含 `roster_version`（整数）, `locked_at`, `session_id`。
- `MEDICAL_HOLD_SET/LIFTED`：载荷需包含 `athlete_id`, `authority`, `reason`。
- `COACH_DECISION_RECORDED`：`authority` 只能为 `coach`，`decision` 取 `START`/`SCRATCH`/`CONCEDE`。

## 角色隔离（契约层可执行的部分）

- 队医可以暂停或解除参赛：`MEDICAL_HOLD_*` 的 `authority` 只能为 `medical`，且禁止携带 `replacement`/`replaced_athlete`——医疗事件不能指定替代者。
- 接力教练只能换人：`ROSTER_SUBSTITUTED` 的 `authority` 只能为 `relay_coach`，且禁止携带 `medical_conclusion`——换人事件不能撤销或改写医疗结论。
- 教练判断单独落在 `COACH_DECISION_RECORDED`，与医疗结论互不覆盖。

## 纪录材料约束

`RECORD_CLAIM_FILED` 只能引用锁定名单版本与锁定时成绩：禁止携带 `post_session_result_ref`。赛后数据不得进入申报引用，证明链为 `roster_version` → `locked_revision` → `result_ref`。

相同事件标识的业务幂等、冲突隔离、"只重算未锁定安排"、快照与当前状态的比对以及窗口占用的并发仲裁由上层服务负责；本仓库只定义可稳定交换的基础事实，并通过必填、禁带字段、枚举和基础类型把上述角色边界固定下来。
