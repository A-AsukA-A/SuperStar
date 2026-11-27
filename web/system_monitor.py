# -*- coding: utf-8 -*-
"""宿主机硬件监控（低开销后台采样）与安全阈值控制。"""

import json
import os
import tempfile
import threading
import time
from typing import Any

import psutil

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT_DIR, "data")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")


def _compute_recommended_max_tasks(cpu_logical: int, mem_total_gb: float) -> int:
    usable_mb = max(256, int(mem_total_gb * 1024) - 320)
    by_mem = max(1, usable_mb // 180)
    by_cpu = max(2, cpu_logical * 3)
    return max(1, min(16, by_cpu, by_mem))


def _sample_hardware_now(non_blocking_cpu: bool = False) -> dict[str, Any]:
    cpu_logical = psutil.cpu_count(logical=True) or 1
    cpu_physical = psutil.cpu_count(logical=False) or cpu_logical
    cpu_percent = round(float(psutil.cpu_percent(interval=None if non_blocking_cpu else 0.1)), 1)

    vm = psutil.virtual_memory()
    mem_total_gb = round(vm.total / (1024**3), 2)
    mem_used_gb = round(vm.used / (1024**3), 2)
    mem_available_gb = round(vm.available / (1024**3), 2)
    mem_percent = round(float(vm.percent), 1)

    disk = psutil.disk_usage(ROOT_DIR)
    disk_total_gb = round(disk.total / (1024**3), 2)
    disk_used_gb = round(disk.used / (1024**3), 2)
    disk_percent = round(float(disk.percent), 1)

    return {
        "cpu_count_logical": cpu_logical,
        "cpu_count_physical": cpu_physical,
        "cpu_percent": cpu_percent,
        "mem_total_gb": mem_total_gb,
        "mem_used_gb": mem_used_gb,
        "mem_available_gb": mem_available_gb,
        "mem_percent": mem_percent,
        "disk_total_gb": disk_total_gb,
        "disk_used_gb": disk_used_gb,
        "disk_percent": disk_percent,
        "recommended_max_containers": _compute_recommended_max_tasks(cpu_logical, mem_total_gb),
        "sampled_at": int(time.time()),
    }


def get_hardware_metrics() -> dict[str, Any]:
    return system_monitor.get_cached_hardware()


class SystemMonitorService:
    """低开销单线程后台硬件采样器。"""

    SAMPLE_INTERVAL_SEC = 2.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        psutil.cpu_percent(interval=None)
        self._hw_cache: dict[str, Any] = _sample_hardware_now(non_blocking_cpu=True)
        self._started = False

    def ensure_started(self) -> None:
        if self._started:
            return
        with self._lock:
            if self._started:
                return
            self._started = True
            t = threading.Thread(target=self._run_loop, name="sys-monitor", daemon=True)
            t.start()

    def _run_loop(self) -> None:
        while True:
            time.sleep(self.SAMPLE_INTERVAL_SEC)
            try:
                hw = _sample_hardware_now(non_blocking_cpu=True)
                with self._lock:
                    self._hw_cache = hw
            except Exception:
                pass

    def get_cached_hardware(self) -> dict[str, Any]:
        self.ensure_started()
        with self._lock:
            return dict(self._hw_cache)


system_monitor = SystemMonitorService()


class SystemSettingsStore:
    """持久化与校验服务器安全阈值配置。"""

    def __init__(self, file_path: str = SETTINGS_PATH) -> None:
        self.file_path = file_path
        self._lock = threading.RLock()

    @staticmethod
    def _default_thresholds(metrics: dict[str, Any]) -> dict[str, Any]:
        return {
            "max_containers": metrics["recommended_max_containers"],
            "max_cpu_percent": 85,
            "max_mem_percent": 85,
        }

    def get_thresholds(self, metrics: dict[str, Any] | None = None) -> dict[str, Any]:
        hw = metrics or get_hardware_metrics()
        defaults = self._default_thresholds(hw)
        with self._lock:
            if not os.path.exists(self.file_path):
                return defaults
            try:
                with open(self.file_path, "r", encoding="utf8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    defaults.update({k: saved[k] for k in defaults if k in saved})
            except (OSError, ValueError):
                pass
        return defaults

    def update_thresholds(self, payload: dict[str, Any]) -> dict[str, Any]:
        hw = get_hardware_metrics()
        current = self.get_thresholds(hw)

        try:
            max_containers = int(payload.get("max_containers", current["max_containers"]))
            max_cpu = float(payload.get("max_cpu_percent", current["max_cpu_percent"]))
            max_mem = float(payload.get("max_mem_percent", current["max_mem_percent"]))
        except (TypeError, ValueError) as exc:
            raise ValueError("阈值参数格式不正确") from exc

        if not (1 <= max_containers <= 100):
            raise ValueError("最大并发任务数必须在 1 ~ 100 之间")
        if not (20 <= max_cpu <= 99):
            raise ValueError("CPU 阈值必须在 20 ~ 99 之间")
        if not (20 <= max_mem <= 99):
            raise ValueError("内存阈值必须在 20 ~ 99 之间")

        updated = {
            "max_containers": max_containers,
            "max_cpu_percent": round(max_cpu, 1),
            "max_mem_percent": round(max_mem, 1),
        }

        with self._lock:
            parent = os.path.dirname(os.path.abspath(self.file_path))
            os.makedirs(parent, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix="settings_", suffix=".tmp", dir=parent)
            try:
                with os.fdopen(fd, "w", encoding="utf8") as f:
                    json.dump(updated, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, self.file_path)
            except Exception:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                raise
        return updated


settings_store = SystemSettingsStore()


def check_safety_guard(running_containers: int = 0) -> dict[str, Any]:
    """检查当前硬件负载与并发任务数是否满足安全阈值。"""
    hw = get_hardware_metrics()
    thresholds = settings_store.get_thresholds(hw)
    reasons: list[str] = []

    if running_containers >= thresholds["max_containers"]:
        reasons.append(f"并发任务已达上限 ({running_containers}/{thresholds['max_containers']})")
    if hw["cpu_percent"] >= thresholds["max_cpu_percent"]:
        reasons.append(f"CPU 占用过高 ({hw['cpu_percent']}% >= {thresholds['max_cpu_percent']}%)")
    if hw["mem_percent"] >= thresholds["max_mem_percent"]:
        reasons.append(f"内存占用过高 ({hw['mem_percent']}% >= {thresholds['max_mem_percent']}%)")

    return {
        "allowed": len(reasons) == 0,
        "reasons": reasons,
        "running_containers": running_containers,
    }


def get_system_overview(running_containers: int = 0) -> dict[str, Any]:
    hw = get_hardware_metrics()
    thresholds = settings_store.get_thresholds(hw)
    guard = check_safety_guard(running_containers=running_containers)
    return {
        "hardware": hw,
        "thresholds": thresholds,
        "runtime": {"mode": "process", "label": "独立进程模式"},
        "safety_guard": guard,
    }
