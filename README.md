# 就业服务预约积压的到期压力与窗口承接

各就业服务站使用同一服务日日历，时间用本地时刻。业务流水列受理回执、暂停及回流事实，窗口开放表来自排班。所有请求均为演练代号。

## 资料约定

CSV 使用 UTF-8，首行为列名。多值字段以竖线分隔。空单元格表示来源未填。

- policy.csv。义务时限按受理后的服务日计数。hold_days 表示站点在有效暂停开始后按规则保留已通知的预约多少服务日。segments 是完成所需连续半小时段数。 当天受理或暂停的日界，以事件时间所在服务日结束时更新状态；受理当天不计入办理义务。
  字段为 `service`、`limit_days`、`segments`、`qualification`、`can_suspend`、`hold_days`。
- calendar.csv。服务工作日用于计时。窗口另由开放表决定。
  字段为 `date`、`service_day`。
- intakes.csv。一次申请的受理日期和可办理站点。prior 是重办的来源申请，restart 以受理回执为准。
  字段为 `case`、`service`、`station`、`alternates`、`accepted`、`prior`、`restart`。
- events.csv。按时间戳发生的外部业务事实。EV 和 EG 回执可能在同一时间改变多项服务义务。
  字段为 `event`、`case`、`kind`、`at`、`site`、`receipt`。
- slots.csv。窗口连续开放时段。shared_staff 相同代表同一位可支援人员，不能在重叠时段用两次。
  字段为 `slot`、`site`、`day`、`start`、`end`、`qualification`、`shared_staff`、`status`。
- appointments.csv。已经承诺给申请人的时段。场站取消回执另在 events 中。
  字段为 `appointment`、`case`、`slot`、`start`、`end`、`notified`。
- scenarios.csv。经理可审议的积压回流假设，日期和回流下上界用于比较需求压力。
  字段为 `scenario`、`service`、`day`、`resuming_low`、`resuming_high`、`arrivals_low`、`arrivals_high`。

## 检查演练资料

使用 Python 3.11.2 标准库，无需安装第三方依赖。在项目根目录运行下列命令可检查 CSV 读取、资料关联及样例记录完整性。

```bash
python3 -m unittest discover -s tests -v
```
