# 领域约定

描述游泳多项目报名、恢复占用、接力让渡和检录锁定的事件。

聚合对象包括 `athlete`、`event_entry`、`load_window`、`relay_roster`、`load_plan`、
`checkin_revision`、`result_sheet`。事件类型包括 `ATHLETE_REGISTERED`、
`ENTRY_REGISTERED`、`LOAD_RECORDED`、`MEDICAL_HOLD_SET`、`RELAY_CANDIDATES_LISTED`、
`ROSTER_SUBSTITUTED`、`PLAN_PROPOSED`、`PLAN_CONFIRMED`、`PLAN_DISCARDED`、
`CHECKIN_LOCKED`、`RESULT_CERTIFIED`、`RECORD_DECLARED`。所有发生时间都必须携带时区，
版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `ATHLETE_REGISTERED`：`name`。
- `ENTRY_REGISTERED`：`athlete_id`、`event_code`、`round`、`scheduled_start`、`scheduled_end`；
  可选 `entry_role`（`seed` 或 `alternate`，默认 `seed`）。
- `LOAD_RECORDED`：`source_ref`、`load_value`、`athlete_id`、`load_type`
  （`split`/`warmup`/`recovery` 等）、`window_start`、`window_end`；
  方案确认占用的窗口另带 `allocated_by=plan_id`。
- `MEDICAL_HOLD_SET`：`athlete_id`、`held`、`reason`。
- `RELAY_CANDIDATES_LISTED`：`relay_id`、`candidate_ids`、`event_code`、`round`、
  `scheduled_start`、`scheduled_end`；可选 `initial_slots`（默认与候选一致）。
- `ROSTER_SUBSTITUTED`：`replaced_athlete`、`replacement`、`relay_id`、`slot`
  （个人项目换人为 `relay_id=null`，聚合用 `event_entry`）。
- `PLAN_PROPOSED`：`plan_id`、`basis`（冻结所依据的事件标识序列）、`target`、`changes`、
  `windows`、`evaluation`。
- `PLAN_CONFIRMED`：`plan_id`、`occupied_windows`、`released_arrangements`。
- `PLAN_DISCARDED`：`plan_id`、`reason`。
- `CHECKIN_LOCKED`：`roster_version`、`locked_at`、`event_code`、`round`、`entries`、
  `snapshot_hash`。
- `RESULT_CERTIFIED`：`checkin_revision_id`、`roster_version`、`results`、`snapshot_hash`。
- `RECORD_DECLARED`：`event_code`、`round`、`record_type`、`result_sheet_id`、
  `checkin_revision_id`、`evidence_hash`。

## 核心语义

1. 事件按聚合做乐观版本控制；相同 `event_id` 重试为幂等返回，内容不同则拒绝。
2. 成绩或医疗观察到达后，只重算尚未锁定的安排；`CHECKIN_LOCKED` 形成独立版本，
   此后该项目该轮次的换人、占窗一律拒绝，后续事件只能引用该版本。
3. 角色权限：`medical` 可设置或解除医疗停赛但不能出现在任何换人方案中指定替代者；
   `relay_coach` 只能提议/确认接力方案，不能设置或撤销医疗结论；
   `head_coach` 拥有全部方案与检录权限；`staff` 负责登记报名、负荷与成绩。
4. 方案在冻结快照（`basis`）上推演，评分同时计入相邻项目准备时间、恢复负荷、
   替补可用性与纪录材料完整性；确认时一次性占用全部窗口并原子释放被替代安排。
5. 纪录申报必须引用锁定的检录版本与封存成绩单，证据哈希由锁定名单与当时成绩计算，
   赛后追加数据不会改变已申报证据。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。
