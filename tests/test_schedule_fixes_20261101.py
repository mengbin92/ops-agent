"""2026-11-01 审计修复回归：调度网格锚定 / 对账容错 / 状态-级别映射 / logscan 聚类上限。

离线运行：不连模型、主机或 webhook，状态全部落在临时目录。
"""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import core
from agent import checks, logscan, notify, schedule


class ScheduleFixes20261101Tests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="ops-fix-20261101-")
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        state_patch = patch.object(core, "STATE_DIR", self.state)
        state_patch.start()
        self.addCleanup(state_patch.stop)

    # ------------------------------------------------ 1. 网格锚定（防漂移）

    def test_every_seconds_next_run_stays_on_grid_across_slow_runs(self):
        """到期任务慢速跑两轮：next_run 始终落在原网格点，不随领取/执行耗时滑动。"""
        T0, every = 1_800_000_000.0, 120
        schedule._save([dict(id="sch-grid", prompt="只读巡检", every_seconds=every,
                             enabled=True, next_run=T0)], {})
        # 第一轮：领取延迟 35s —— 推进到下一个网格点，而不是"领取时刻+间隔"
        self.assertIsNotNone(schedule._claim_due("sch-grid", T0 + 35))
        self.assertEqual(schedule.list_entries()[0]["next_run"], T0 + 120)
        # 慢执行 200s 后结束：reanchor 不得改写 every_seconds 任务的 next_run
        with patch.object(schedule.time, "time", return_value=T0 + 235):
            schedule._update_entry_state("sch-grid", "run-1", "succeeded", "正常",
                                         reanchor=True)
        self.assertEqual(schedule.list_entries()[0]["next_run"], T0 + 120)
        # 第二轮：迟到 5s 领取，再慢执行 —— 仍在网格上（T0+240），无累积漂移
        self.assertIsNotNone(schedule._claim_due("sch-grid", T0 + 125))
        self.assertEqual(schedule.list_entries()[0]["next_run"], T0 + 240)
        with patch.object(schedule.time, "time", return_value=T0 + 325):
            schedule._update_entry_state("sch-grid", "run-2", "succeeded", "正常",
                                         reanchor=True)
        self.assertEqual(schedule.list_entries()[0]["next_run"], T0 + 240)

    def test_very_late_claim_skips_grid_slots_without_burst(self):
        """停机很久后领取：next_run 直接跳到 now 之后的网格点，不补跑风暴。"""
        T0 = 1_800_000_000.0
        schedule._save([dict(id="sch-late", prompt="只读巡检", every_seconds=120,
                             enabled=True, next_run=T0)], {})
        self.assertIsNotNone(schedule._claim_due("sch-late", T0 + 500))
        self.assertEqual(schedule.list_entries()[0]["next_run"], T0 + 600)

    def test_daily_tasks_still_reanchor_at_finish(self):
        """daily 任务保留结束重锚：按本地日历重算（夏令时正确性），every_seconds 的
        跳过不得波及 daily。"""
        before = time.time() - 10
        schedule._save([dict(id="sch-daily", prompt="只读巡检", daily="03:00",
                             enabled=True, next_run=before)], {})
        schedule._update_entry_state("sch-daily", "run-1", "succeeded", "正常",
                                     reanchor=True)
        item = schedule.list_entries()[0]
        self.assertIn(item["next_run"],
                      (schedule._next_daily("03:00", before),
                       schedule._next_daily("03:00", time.time())))
        self.assertGreater(item["next_run"], time.time())

    # ------------------------------------------------ 2. 对账容错

    def test_reconcile_continues_after_single_write_oserror(self):
        """对账中单条写盘 OSError（磁盘满）：留痕后继续其余记录，daemon 不死。"""
        entry = schedule.add("巡检", every_seconds=60)
        dead = []
        for pid in (99999999, 99999998):
            rec = schedule._begin_run(entry, None)
            rec["pid"] = pid
            schedule._write_run(rec)
            dead.append(rec)
        real_write = schedule._write_run
        calls = []

        def flaky(rec, locked=False):
            calls.append(rec["run_id"])
            if len(calls) == 1:  # 只失败一次（第一条），第二条必须照常落盘
                raise OSError(28, "No space left on device")
            return real_write(rec, locked=locked)

        with patch.object(schedule, "_write_run", side_effect=flaky):
            updated = schedule.reconcile_runs(records=True)
        self.assertEqual(len(calls), 2)      # 两条都尝试了对账
        self.assertEqual(len(updated), 1)    # 第二条成功标为 interrupted
        statuses = sorted(r["status"] for r in schedule.list_runs())
        self.assertEqual(statuses, ["interrupted", "running"])

    def test_daemon_survives_reconcile_oserror(self):
        """reconcile_runs 整体抛 OSError（如锁文件打不开）：daemon 捕获继续本轮执行。"""
        schedule._save([], {})
        with patch.object(schedule, "reconcile_runs",
                          side_effect=OSError(5, "Input/output error")):
            code = schedule.daemon(once=True)
        self.assertEqual(code, 0)

    # ------------------------------------------------ 3. 睡眠贴合到期时刻

    def test_sleep_seconds_follows_short_deltas_but_backs_off_when_overdue(self):
        now = time.time()
        self.assertEqual(schedule._sleep_seconds(), 60.0)  # 无任务
        schedule._save([dict(id="sch-soon", prompt="只读巡检", every_seconds=60,
                             enabled=True, next_run=now + 5)], {})
        self.assertAlmostEqual(schedule._sleep_seconds(), 5.0, delta=1.0)
        items = schedule.list_entries()
        items[0]["next_run"] = now - 100   # 过期（写盘失败式退化）：退避 >= 30s
        schedule._save(items)
        self.assertGreaterEqual(schedule._sleep_seconds(), 30.0)

    # ------------------------------------------------ 4. 状态->级别权威映射

    def test_unmapped_check_status_is_not_notified_as_unreachable(self):
        """词汇表外的状态取安全默认 warning，不再静默映成最吓人的 unreachable。"""
        # checks 能发的状态保持原语义：unknown/error 仍是"不可采集"单独一级
        self.assertEqual(checks.STATUS_TO_SEVERITY["critical"], "critical")
        self.assertEqual(checks.STATUS_TO_SEVERITY["warn"], "warning")
        self.assertEqual(checks.STATUS_TO_SEVERITY["unknown"], "unreachable")
        self.assertEqual(checks.STATUS_TO_SEVERITY["error"], "unreachable")
        rec = {"task_id": "sch-x", "run_id": "run-1", "status": "succeeded",
               "verdict": "正常"}
        signal = {"anomalies": [
            {"id": "web1/resources/disk", "target": "web1", "name": "disk",
             "status": "catastrophic-typo", "line": "[?] 未知状态"}]}
        items = notify._current_items(rec, signal)
        self.assertEqual(items["check:web1/resources/disk"]["severity"], "warning")

    # ------------------------------------------------ 7. logscan 聚类上限

    def test_logscan_caps_distinct_groups_and_notes_truncation(self):
        """全唯一行输入：distinct 聚类封顶，已有聚类继续累计，结果如实标注截断。"""

        def tag(i):
            out = ""
            while True:
                out = chr(97 + i % 26) + out
                i //= 26
                if i == 0:
                    return out

        lines = [f"error unique failure {tag(i)}" for i in range(30)]
        lines += ["error unique failure a"] * 5   # 封顶后重复模式仍要累计
        with patch.object(logscan, "GROUP_LIMIT", 10):
            report = logscan.scan(lines)
            text = logscan.format_report("test 来源", report)
        self.assertEqual(report["group_total"], 10)
        self.assertTrue(report["groups_truncated"])
        self.assertFalse(report["partial"])       # 行都扫完了：不是时间预算截断
        self.assertEqual(report["groups"][0]["count"], 6)  # 重复模式未被丢弃
        self.assertIn("10 组上限", text)
        self.assertIn("未再收录", text)


if __name__ == "__main__":
    unittest.main()
