# 游泳多项负荷让渡簿

管理个人项目与接力密集重叠比赛日的多项负荷让渡：按运动员保存报名资格、项目轮次、
实测分段、热身与恢复占用、医疗限制、接力候选、检录节点和教练判断；工作人员可以在
冻结的数据上推演换人方案，确认时一次性占窗并释放被替代安排。

## 它保证什么

- **只重算未锁定安排**：个人成绩或医疗观察随时可登记；`CHECKIN_LOCKED` 后该项目该轮次
  形成独立版本，换人、改阵、占窗一律拒绝。
- **角色边界**：队医（`medical`）可以暂停或放行队员但不能指定替代者；接力教练
  （`relay_coach`）只能处理接力方案，不能出具或撤销医疗结论；主教练（`head_coach`）
  决定个人方案、检录与纪录申报；工作人员（`staff`）登记报名、负荷与成绩。
- **冻结推演**：方案在提议时刻的事件序列（`basis`）上评分，不改动任何现有安排；
  确认时再以最新事实重算，期间被锁定或医疗状态变化都会让方案失效。
- **四维同时比较**：相邻项目准备时间、恢复负荷、替补可用性、纪录材料完整性各 100 分；
  时间重叠、医疗停赛、已检录锁定、一人占两棒为硬阻断，其余为扣分项。
- **原子让渡**：确认一个方案时单批事件占用替代者的热身/恢复窗口、完成换人、
  释放被替代者占用与被消费的替补席位——要么全部生效，要么全部不生效。
- **可还原、可举证**：按比赛日还原实际采用阵容与被放弃方案及原因；`impact_trace`
  展示一次让位怎样影响其他项目；纪录申报的证据哈希只绑定当时锁定的名单版本与封存
  成绩单，赛后追加数据不会改变它，事后篡改名单会使核验失败。

## 目录

- `contracts/domain.schema.json`：对象、事件、角色和载荷字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `src/swim_load/`
  - `contracts.py`：基础契约校验（不改写输入）。
  - `model.py`：角色、时间与哈希工具。
  - `store.py`：追加式事件存储，幂等重试、按聚合乐观版本、批量原子提交。
  - `projection.py`：事件回放为世界状态。
  - `planning.py`：`SubstitutionSpec`、四维评分、冻结推演与连锁影响。
  - `service.py`：应用服务（权限、方案提议/放弃/确认、检录、成绩、纪录）。
  - `history.py`：按比赛日还原、让位影响追溯、纪录证据核验。
- `tests/`：契约、存储、服务场景与读模型测试（含标准比赛日夹具 `scenario.py`）。
- `docs/domain.md`：领域对象与事件语义。

## 快速使用

```python
from swim_load import (
    Actor, EventStore, LoadRelayService, Role, SubstitutionSpec,
)

svc = LoadRelayService(EventStore())
staff, medical, head = Actor("s", Role.STAFF), Actor("m", Role.MEDICAL), Actor("h", Role.HEAD_COACH)

svc.register_athlete(staff, "a1", "一姐", occurred_at="2026-09-26T08:00:00+08:00")
svc.register_entry(staff, "e-f-a1", "a1", "100FR", "final",
                   "2026-09-26T18:00:00+08:00", "2026-09-26T18:05:00+08:00",
                   occurred_at="2026-09-26T08:05:00+08:00")

# 队医只能停赛，不能点替代者；接力教练也无权撤销这条结论
svc.set_medical_hold(medical, "a1", True, "肩部不适", occurred_at="2026-09-26T12:00:00+08:00")

# 在冻结快照上推演（不改变安排），再确认（原子占窗 + 换人 + 释放）
svc.propose_plan(head, "plan-1",
                 SubstitutionSpec("100FR", "final", "a1", "a2", entry_id="e-f-a1"),
                 occurred_at="2026-09-26T12:05:00+08:00")
svc.confirm_plan(head, "plan-1", occurred_at="2026-09-26T12:10:00+08:00")

lock = svc.lock_checkin(head, "100FR", "final", occurred_at="2026-09-26T17:30:00+08:00")
svc.certify_result(staff, "sheet-1", lock["revision_id"],
                   [{"entry_id": "e-f-a1", "mark": "53.21"}],
                   occurred_at="2026-09-26T18:30:00+08:00")
svc.declare_record(head, "rec-1", "100FR", "final", "meet_record",
                   occurred_at="2026-09-26T18:35:00+08:00")
```

事件日志可序列化为 JSONL（`EventStore.save_jsonl` / `load_jsonl`），所有时间必须携带时区。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 样例校验

```bash
PYTHONPATH=src python3 -m swim_load.cli contracts/domain.schema.json data/sample.json
```

样例有效时输出 `valid`；发现问题时逐行给出字段、代码和中文说明，并返回非零状态。
