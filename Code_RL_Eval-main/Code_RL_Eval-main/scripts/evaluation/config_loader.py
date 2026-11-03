"""
评估配置加载器（精简版）

只管理核心配置：
- Agent 模型和迭代次数
- Verifier 重试次数
"""

import yaml
from pathlib import Path
from typing import Dict, Any, Optional, List

from agents import infer_vendor


class EvaluationConfig:
    """评估配置管理器"""

    DEFAULT_CONFIG = {
        "agent": {
            "vendors": {
                "claude": ["claude-opus-4"],
            },
            "iterations": 1,
            "parallel": 1,
        },
        "verification": {
            "verifier_runs": 1,
        }
    }

    def __init__(self, config_file: Optional[Path] = None, **overrides):
        """
        初始化配置

        Args:
            config_file: 配置文件路径（YAML），如果为 None 则使用默认配置
            **overrides: 命令行参数覆盖的配置（如 models=["claude-opus-4"], iterations=3）
        """
        self.config = self._load_config(config_file)

        # 应用命令行覆盖
        if overrides:
            self._apply_overrides(overrides)

    def _load_config(self, config_file: Optional[Path]) -> Dict:
        """加载配置文件"""
        config = self.DEFAULT_CONFIG.copy()

        if config_file and config_file.exists():
            with open(config_file) as f:
                user_config = yaml.safe_load(f)
                if user_config:
                    config = self._merge_dict(config, user_config)

        return config

    def _merge_dict(self, base: Dict, update: Dict) -> Dict:
        """递归合并字典"""
        result = base.copy()
        for key, value in update.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._merge_dict(result[key], value)
            else:
                result[key] = value
        return result

    def _apply_overrides(self, overrides: Dict):
        """应用命令行参数覆盖"""
        # 特殊处理：models 和 iterations 直接映射到 agent.*
        if "models" in overrides and overrides["models"] is not None:
            # 命令行只给了模型名（无 vendor），按名称推断 vendor 后归组
            vendors: Dict[str, List[str]] = {}
            for name in overrides["models"]:
                vendors.setdefault(infer_vendor(name), []).append(name)
            self.config["agent"]["vendors"] = vendors
            self.config["agent"].pop("models", None)  # 移除旧扁平写法，避免冲突
        if "iterations" in overrides and overrides["iterations"] is not None:
            self.config["agent"]["iterations"] = overrides["iterations"]
        if "parallel" in overrides and overrides["parallel"] is not None:
            self.config["agent"]["parallel"] = overrides["parallel"]
        if "verifier_runs" in overrides and overrides["verifier_runs"] is not None:
            self.config["verification"]["verifier_runs"] = overrides["verifier_runs"]

    # 便捷访问属性
    @property
    def model_specs(self) -> List[Dict[str, str]]:
        """展开为 [{"name": ..., "vendor": ...}, ...]

        兼容两种写法：
        - agent.vendors: {vendor: [name, ...]}（推荐）
        - agent.models: [name, ...]（旧扁平写法，vendor 由名称推断）
        """
        agent = self.config["agent"]
        specs: List[Dict[str, str]] = []
        vendors = agent.get("vendors")
        if vendors:
            for vendor, names in vendors.items():
                for name in (names or []):
                    specs.append({"name": name, "vendor": vendor})
        else:
            for name in agent.get("models", []):
                specs.append({"name": name, "vendor": infer_vendor(name)})
        return specs

    @property
    def models(self) -> List[str]:
        """要评测的模型名列表（供构建工作空间 / 传参给容器内 setup.py 使用）"""
        return [spec["name"] for spec in self.model_specs]

    @property
    def iterations(self) -> int:
        """每个模型的迭代次数"""
        return self.config["agent"]["iterations"]

    @property
    def parallel(self) -> int:
        """重构并行度（步骤 4 同时执行的候选数）"""
        return self.config["agent"].get("parallel", 1)

    @property
    def verifier_runs(self) -> int:
        """verifier 固定运行次数（每次都跑，不做跨次聚合）"""
        return self.config["verification"].get("verifier_runs", 1)


def load_config(config_file: Optional[Path] = None, **overrides) -> EvaluationConfig:
    """
    加载配置的便捷函数

    Args:
        config_file: 配置文件路径（默认使用 scripts/evaluation/config.yaml）
        **overrides: 命令行参数覆盖

    Returns:
        EvaluationConfig 实例

    Examples:
        # 使用默认配置文件
        config = load_config()

        # 使用自定义配置文件
        config = load_config(Path("my_config.yaml"))

        # 命令行参数覆盖
        config = load_config(
            models=["claude-opus-4", "gpt-4"],
            iterations=5
        )
    """
    # 如果没指定配置文件，使用默认位置
    if config_file is None:
        default_config = Path(__file__).parent / "config.yaml"
        if default_config.exists():
            config_file = default_config

    return EvaluationConfig(config_file, **overrides)

