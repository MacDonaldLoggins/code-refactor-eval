#!/usr/bin/env python3
"""
步骤 4: 创建工作空间并执行重构任务

设计思路：
    这是评估流程的核心步骤。脚本会先确保每个待评测模型的工作空间已就绪
    （若不存在则在容器里执行 setup.py 自动创建），再在每个工作空间里调用
    对应 vendor 的 agent 执行重构任务。

    "创建工作空间"原为独立的 04 脚本，现已合并进本脚本，无需再预先创建。

    每个工作空间包含：
    - repo/（代码副本，测试已删除）
    - prompt.md（重构任务描述）
    - agent_session/（工作目录）
    - patches/（保存生成的 patch）

    执行流程：
    1. 确保工作空间存在（缺失或指定 --recreate 时自动创建）
    2. 读取 prompt.md，理解重构任务
    3. 分析代码库，定位需要重构的部分
    4. 执行重构，生成 patch
    5. 保存结果

功能：
    1. 读取容器信息 / 配置（模型、迭代次数）
    2. 自动创建工作空间（在容器里执行 setup.py）
    3. 并行或串行调用 agent
    4. 收集结果（patch、日志、测试结果）

用法：
    python scripts/evaluation/04_run_refactor.py --case manual_tasks/xxx
    python scripts/evaluation/04_run_refactor.py --case manual_tasks/xxx --parallel 3
    python scripts/evaluation/04_run_refactor.py --case manual_tasks/xxx --workspace claude-opus-4__01
    python scripts/evaluation/04_run_refactor.py --case manual_tasks/xxx --models claude-opus-4 --iterations 3
    python scripts/evaluation/04_run_refactor.py --case manual_tasks/xxx --recreate
"""

import sys
import json
import time
import uuid
import random
import argparse
import subprocess
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from agents import infer_vendor, is_supported, build_refactor_command
from config_loader import load_config


class WorkspaceCreator:
    """Agent 工作空间创建器

    在容器里执行 case 的 setup.py，为每个待评测的模型创建独立工作空间。
    原为独立的脚本，现合并进本脚本（04），供 RefactorRunner 按需调用。
    """

    def __init__(self, case_path: Path, config, container_info: Dict):
        self.case_path = case_path
        self.config = config

        # 从配置读取模型（含 vendor）和迭代次数
        self.model_specs = config.model_specs      # [{"name", "vendor"}, ...]
        self.models = config.models                # 仅模型名，供容器内 setup.py 建目录
        self.iterations = config.iterations

        # 容器信息由调用方（RefactorRunner）传入，避免重复读取
        self.container_id = container_info["container_id"]
        self.container_name = container_info["container_name"]

        # 由本脚本生成工作空间名（唯一命名来源），下发给 setup.py 建目录，
        # 再用同一批名字校验、写入 .workspace_info.json，保证 04/05/06 全程对齐。
        # 命名格式：`模型名__时间戳_4位随机`
        self.workspace_specs = self._generate_workspace_specs()

    def _generate_workspace_specs(self) -> List[Dict]:
        """为每个 (模型, 迭代) 生成一个唯一的工作空间名及元数据"""
        specs: List[Dict] = []
        used = set()
        for spec in self.model_specs:
            model = spec["name"]
            vendor = spec["vendor"]
            for i in range(1, self.iterations + 1):
                while True:
                    timestamp = time.strftime("%Y%m%d_%H%M%S")
                    suffix = f"{random.randint(0, 9999):04d}"
                    name = f"{model}__{timestamp}_{suffix}"
                    if name not in used:
                        used.add(name)
                        break
                specs.append({
                    "name": name,
                    "model": model,
                    "vendor": vendor,
                    "iteration": i,
                    "path": f"/workspace/candidates/{name}",
                })
        return specs

    def _validate(self):
        """验证前置条件"""
        # 检查容器是否在运行
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", self.container_id],
            capture_output=True,
            text=True
        )
        if result.returncode != 0 or result.stdout.strip() != "true":
            raise RuntimeError(
                f"容器未运行: {self.container_id}\n"
                f"请先运行: python scripts/evaluation/02_build_image.py --case {self.case_path}"
            )

        # 检查 setup.py 是否存在
        setup_file = self.case_path / "setup.py"
        if not setup_file.exists():
            raise FileNotFoundError(
                f"未找到 setup.py: {setup_file}\n"
                f"该脚本依赖 case 目录内的 setup.py"
            )

    def create(self) -> Dict:
        """创建工作空间并返回工作空间信息（同时写入 .workspace_info.json）"""
        print("\n" + "-" * 80)
        print(f"创建 Agent 工作空间 - {self.case_path.name}")
        print("-" * 80)

        self._validate()
        print(f"使用容器: {self.container_name} ({self.container_id[:12]})")

        print("\n[1/3] 在容器里执行 setup.py ...")
        self._run_setup_in_container()
        print(f"  ✅ 工作空间创建完成")

        print("\n[2/3] 验证工作空间 ...")
        workspaces = self._verify_workspaces()
        print(f"  ✅ 共创建 {len(workspaces)} 个工作空间")

        print("\n[3/3] 保存工作空间信息 ...")
        info = self._save_workspace_info(workspaces)

        return info

    def _run_setup_in_container(self):
        """在容器里执行 setup.py，下发本脚本生成的工作空间名"""
        names = [s["name"] for s in self.workspace_specs]

        cmd = [
            "docker", "exec", self.container_name,
            "python", "/workspace/setup.py",
            "--case", "/workspace",
            "--names", *names,
        ]

        print(f"    执行: python setup.py --case /workspace --names {' '.join(names)}")

        result = subprocess.run(
            cmd,
            capture_output=False,  # 显示输出
            text=True
        )

        if result.returncode != 0:
            raise RuntimeError(f"setup.py 执行失败，退出码: {result.returncode}")

    def _verify_workspaces(self) -> List[Dict]:
        """验证本脚本生成、并已下发给 setup.py 创建的工作空间是否就绪"""
        workspaces = []

        for spec in self.workspace_specs:
            # 检查工作空间是否存在
            result = subprocess.run(
                ["docker", "exec", self.container_name, "test", "-d", spec["path"]],
                capture_output=True
            )

            if result.returncode == 0:
                print(f"    ✅ {spec['name']}")
                workspaces.append(spec)
            else:
                print(f"    ❌ {spec['name']} 不存在")
                raise RuntimeError(f"工作空间 {spec['name']} 创建失败")

        return workspaces

    def _save_workspace_info(self, workspaces: List[Dict]) -> Dict:
        """保存工作空间信息供后续步骤使用，并返回该信息"""
        info_file = self.case_path / ".workspace_info.json"
        info = {
            "case": self.case_path.name,
            "container_id": self.container_id,
            "container_name": self.container_name,
            "workspaces": workspaces,
            "total": len(workspaces)
        }

        with open(info_file, "w") as f:
            json.dump(info, f, indent=2)

        print(f"  ✅ 工作空间信息已保存: {info_file}")
        print("\n工作空间列表：")
        for ws in workspaces:
            print(f"  - {ws['name']} @ {ws['path']}")

        return info


class RefactorRunner:
    """重构任务执行器"""

    def __init__(
        self,
        case_path: Path,
        config,
        parallel: int = 1,
        target_workspace: Optional[str] = None,
        recreate: bool = False,
    ):
        self.case_path = case_path
        self.config = config
        self.parallel = parallel
        self.target_workspace = target_workspace
        self.recreate = recreate

        self.metadata = self._load_metadata()
        self.container_info = self._load_container_info()

        self.container_id = self.container_info["container_id"]
        self.container_name = self.container_info["container_name"]

        # 确保工作空间存在（缺失或指定 --recreate 时自动创建）
        self.workspace_info = self._ensure_workspace_info()

        # 结果目录
        self.results_dir = self.case_path / "results" / "refactor"
        self.results_dir.mkdir(parents=True, exist_ok=True)

    def _load_metadata(self) -> Dict:
        metadata_file = self.case_path / "metadata.json"
        if not metadata_file.exists():
            raise FileNotFoundError(f"未找到 metadata.json: {metadata_file}")
        with open(metadata_file) as f:
            return json.load(f)

    def _load_container_info(self) -> Dict:
        info_file = self.case_path / ".container_info.json"
        if not info_file.exists():
            raise FileNotFoundError(
                f"未找到容器信息: {info_file}\n"
                f"请先运行: python scripts/evaluation/02_build_image.py --case {self.case_path}"
            )
        with open(info_file) as f:
            return json.load(f)

    def _ensure_workspace_info(self) -> Dict:
        """确保工作空间信息就绪：已存在则直接读取，否则自动创建。"""
        info_file = self.case_path / ".workspace_info.json"

        if info_file.exists() and not self.recreate:
            with open(info_file) as f:
                cached = json.load(f)

            # 不盲信缓存：容器可能已被第2步重建，缓存里的工作空间目录未必真实存在。
            # 逐个校验容器内 candidates/<name> 是否在，全部就绪才复用缓存。
            if self._workspaces_exist_in_container(cached):
                print(f"使用已有工作空间信息: {info_file}")
                return cached
            print("⚠️  缓存的工作空间在容器内不存在（容器可能已重建），将重新创建 ...")

        elif self.recreate and info_file.exists():
            print("检测到 --recreate，将重新创建工作空间 ...")
        else:
            print("未找到工作空间信息，自动创建工作空间 ...")

        creator = WorkspaceCreator(
            case_path=self.case_path,
            config=self.config,
            container_info=self.container_info,
        )
        return creator.create()

    def _workspaces_exist_in_container(self, info: Dict) -> bool:
        """校验缓存里记录的工作空间目录是否在当前容器内真实存在。"""
        workspaces = info.get("workspaces", [])
        if not workspaces:
            return False
        for ws in workspaces:
            path = ws.get("path")
            if not path:
                return False
            result = subprocess.run(
                ["docker", "exec", self.container_name, "test", "-d", path],
                capture_output=True,
            )
            if result.returncode != 0:
                return False
        return True

    def run(self) -> int:
        print("=" * 80)
        print(f"步骤 4: 执行重构任务 - {self.case_path.name}")
        print("=" * 80)

        try:
            # 确定要执行的工作空间
            workspaces = self.workspace_info["workspaces"]
            if self.target_workspace:
                workspaces = [ws for ws in workspaces if ws["name"] == self.target_workspace]
                if not workspaces:
                    raise ValueError(f"未找到工作空间: {self.target_workspace}")

            print(f"\n将执行 {len(workspaces)} 个重构任务")
            print(f"并行度: {self.parallel}")
            print(f"容器: {self.container_name}\n")

            # 执行重构任务
            if self.parallel == 1:
                # 串行执行
                results = []
                for ws in workspaces:
                    result = self._run_single_refactor(ws)
                    results.append(result)
            else:
                # 并行执行
                results = self._run_parallel_refactor(workspaces)

            # 统计结果
            success_count = sum(1 for r in results if r.get("status") == "success")
            skipped_count = sum(1 for r in results if r.get("status") == "skipped")
            failed_count = len(results) - success_count - skipped_count
            print("\n" + "=" * 80)
            print("执行完成")
            print("=" * 80)
            print(f"总数: {len(results)}")
            print(f"成功: {success_count}")
            print(f"跳过: {skipped_count}")
            print(f"失败: {failed_count}")

            # 保存所有结果
            self._save_all_results(results)

            # 单个候选 agent 失败不阻塞整条流水线：只要有任一成功候选，就继续往下走
            # （后续步骤会基于成功候选的 patch 评估）。仅当「无任何成功候选」时才判失败。
            if success_count == 0:
                print("\n❌ 没有任何候选重构成功，无法继续后续评估")
                return 1
            if failed_count > 0:
                print(f"\n⚠️  有 {failed_count} 个候选失败，但存在成功候选，继续后续步骤")
            return 0

        except Exception as e:
            print(f"\n❌ 重构任务执行失败: {e}")
            return 1

    def _run_single_refactor(self, workspace: Dict) -> Dict:
        """在单个工作空间里执行重构"""
        ws_name = workspace["name"]
        ws_path = workspace["path"]
        # 兼容旧 workspace_info（无 vendor 字段）：按模型名推断
        vendor = workspace.get("vendor") or infer_vendor(workspace["model"])

        print(f"\n{'=' * 80}")
        print(f"执行重构: {ws_name}  (vendor: {vendor})")
        print(f"{'=' * 80}")

        result = {
            "workspace": ws_name,
            "model": workspace["model"],
            "vendor": vendor,
            "iteration": workspace["iteration"],
            "timestamp": datetime.now().isoformat(),
            "status": "unknown"
        }

        # 目前仅支持 claude（Claude Code CLI），其余 vendor 跳过
        if not is_supported(vendor):
            result["status"] = "skipped"
            result["error"] = f"vendor '{vendor}' 暂不支持，已跳过"
            print(f"  ⏭️  跳过：vendor '{vendor}' 暂不支持")
            return result

        try:
            # 1. 准备重构提示词
            print(f"[1/2] 准备重构提示词 ...")
            # 先用宿主机 case 目录里的最新 prompt.md 覆盖容器内工作区副本，
            # 避免读到镜像构建期烤进去的旧版（single source of truth = case/prompt.md）。
            self._sync_prompt_to_workspace(ws_path)
            refactor_prompt = self._generate_refactor_prompt(ws_path)
            self._write_prompt_to_container(refactor_prompt, f"{ws_path}/refactor_task.md")
            print(f"  ✅ 提示词已写入: {ws_path}/refactor_task.md")

            # 2. 调用对应 vendor 的 agent 执行重构
            print(f"[2/2] 调用 agent 执行重构 ...")
            refactor_result = self._run_claude_refactor(vendor, ws_path)
            result.update(refactor_result)

            if refactor_result.get("status") != "success":
                result["status"] = "failed"
                result["error"] = refactor_result.get("error", "重构失败")
                print(f"  ❌ 重构失败: {result['error']}")
            else:
                result["status"] = "success"
                print(f"  ✅ 重构完成")
                print(f"\n✅ {ws_name} 执行成功")

        except Exception as e:
            result["status"] = "failed"
            result["error"] = str(e)
            print(f"\n❌ {ws_name} 执行失败: {e}")

        return result

    def _run_parallel_refactor(self, workspaces: List[Dict]) -> List[Dict]:
        """并行执行多个重构任务"""
        results = []
        with ThreadPoolExecutor(max_workers=self.parallel) as executor:
            futures = {
                executor.submit(self._run_single_refactor, ws): ws
                for ws in workspaces
            }

            for future in as_completed(futures):
                ws = futures[future]
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    print(f"\n❌ {ws['name']} 执行异常: {e}")
                    results.append({
                        "workspace": ws["name"],
                        "status": "failed",
                        "error": str(e)
                    })

        return results

    def _generate_refactor_prompt(self, ws_path: str) -> str:
        """生成重构任务提示词

        直接内联 prompt.md 的完整任务描述，再追加操作契约（工作目录、patch
        导出路径、结果报告格式），让 agent 在初始上下文中一次拿到完整任务，
        不再依赖"请去读另一个文件"的间接寻址。若 prompt.md 读取失败，回退到
        仅含操作契约的转发壳。
        """
        task_body = self._read_prompt_from_container(f"{ws_path}/prompt.md")

        # 操作契约：流程必需信息（评估依赖 patches/refactor.patch 的确切路径）。
        operating_contract = f"""## 工作目录与交付契约

- 当前目录（代码根，已是独立 git 仓库）：{ws_path}/repo
- 完成重构后，在当前目录执行导出 patch：`git diff > {ws_path}/patches/refactor.patch`
- 创建结果报告：{ws_path}/refactor_result.json

### 结果报告格式
```json
{{
  "status": "success" | "failed",
  "patch_file": "{ws_path}/patches/refactor.patch",
  "files_changed": ["file1.py", "file2.py"],
  "summary": "简要描述你做的重构"
}}
```
"""

        if task_body:
            return f"{task_body.rstrip()}\n\n{operating_contract}"

        # 回退：无法读取 prompt.md 时，至少保证操作契约与文件指引可用。
        return f"""# 重构任务

请阅读 {ws_path}/prompt.md 中的重构任务描述，执行重构。

{operating_contract}"""

    def _sync_prompt_to_workspace(self, ws_path: str):
        """把宿主机 case 目录里的最新 prompt.md 同步进容器工作区。

        镜像在构建期通过 Dockerfile COPY 烤入 prompt.md，容器长期复用时不会
        自动更新；若在宿主机改了 prompt.md 却不重建容器，agent 会拿到旧版任务书。
        这里在跑 agent 之前用 docker cp 覆盖，确保任务书与 case/prompt.md 一致。
        best-effort：源文件不存在或 cp 失败只告警，不中断（回退到容器内既有副本）。
        """
        src = self.case_path / "prompt.md"
        if not src.exists():
            print(f"  ⚠️  跳过 prompt.md 同步（case 内不存在）: {src}")
            return
        result = subprocess.run(
            ["docker", "cp", str(src), f"{self.container_name}:{ws_path}/prompt.md"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print(f"  ✅ 已同步最新 prompt.md 到 {ws_path}/prompt.md")
        else:
            print(f"  ⚠️  prompt.md 同步失败，沿用容器内既有副本: {result.stderr.strip()}")

    def _read_prompt_from_container(self, source_path: str) -> Optional[str]:
        """从容器读取文件正文；失败返回 None。"""
        try:
            result = subprocess.run(
                ["docker", "exec", self.container_name, "cat", source_path],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout
        except Exception as e:
            print(f"  ⚠️  读取 {source_path} 失败，回退到转发壳: {e}")
        return None

    def _write_prompt_to_container(self, prompt: str, target_path: str):
        """将提示词写入容器"""
        subprocess.run(
            ["docker", "exec", "-i", self.container_name, "sh", "-c",
             f"cat > {target_path}"],
            input=prompt.encode(),
            check=True
        )

    def _run_claude_refactor(self, vendor: str, ws_path: str) -> Dict:
        """在工作空间里调用对应 vendor 的 agent 执行重构"""
        # 为本次会话指定固定 UUID，让 Claude Code 的原生轨迹落到可预测的文件名，
        # 跑完后据此精确定位导出，避免依赖 cwd->目录名的脆弱转义，也不怕并行撞车。
        session_id = str(uuid.uuid4())
        cmd = build_refactor_command(vendor, self.container_name, ws_path,
                                     prompt_ref=f"@{ws_path}/refactor_task.md",
                                     session_id=session_id)

        print(f"    执行: cd {ws_path}/repo && claude --dangerously-skip-permissions --session-id {session_id} @{ws_path}/refactor_task.md")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3600  # 1 小时超时
            )

            # 判定成败以「客观产物 git diff」为准，而非 agent 是否自觉写 result.json。
            # agent 常常改了代码却漏写 patch/json，此时不应误判为失败。
            diff_text, diff_err = self._collect_git_diff(ws_path)

            # 脚本兜底：无论 agent 有没有执行 `git diff > patch`，都由脚本落一次 patch，
            # 保证后续步骤能拿到 patches/refactor.patch。
            self._ensure_patch_written(ws_path, diff_text)

            # 导出 Claude Code 原生修复轨迹（best-effort，失败不影响成败判定）。
            trajectory_file = self._export_trajectory(ws_path, session_id)

            # refactor_result.json 降级为「可选补充信息」：有就读，没有也不阻塞。
            agent_report = self._read_agent_report(ws_path)

            has_changes = bool(diff_text.strip())

            outcome: Dict = {
                "claude_output": result.stdout[-2000:],
                "claude_stderr": result.stderr[-2000:],
                "diff_stat": self._diff_stat(ws_path),
                "session_id": session_id,
            }
            if trajectory_file:
                outcome["trajectory_file"] = trajectory_file
            if agent_report is not None:
                outcome["agent_report"] = agent_report

            if has_changes:
                outcome["status"] = "success"
                outcome["patch_file"] = f"{ws_path}/patches/refactor.patch"
            else:
                # 真的没有任何代码改动才算失败
                outcome["status"] = "failed"
                outcome["error"] = "agent 未产生任何代码改动（git diff 为空）"
                if diff_err:
                    outcome["diff_error"] = diff_err
            return outcome

        except subprocess.TimeoutExpired:
            return {"status": "failed", "error": "Claude Code 执行超时（1小时）"}
        except Exception as e:
            return {"status": "failed", "error": str(e)}

    def _collect_git_diff(self, ws_path: str) -> tuple:
        """读取工作空间 repo 的 git diff（含未跟踪文件）。返回 (diff_text, error)。

        策略：
        1. 先尝试 `git diff HEAD`（覆盖工作区+暂存区的未提交变更）
        2. 如果为空，检查是否有新提交（agent 可能提交了代码）
        3. 如果有新提交，生成从 baseline（第一个 commit）到 HEAD 的 diff

        这样可以捕获两种情况：
        - Agent 修改了代码但未提交（工作区/暂存区有变更）
        - Agent 修改并提交了代码（有新的 commit）
        """
        # 首先尝试获取工作区+暂存区的未提交变更
        proc = subprocess.run(
            ["docker", "exec", self.container_name, "sh", "-c",
             f"cd {ws_path}/repo && git add -A -N . >/dev/null 2>&1; git diff HEAD"],
            capture_output=True, text=True
        )
        if proc.returncode != 0:
            return "", proc.stderr[-500:]

        # 如果有未提交的变更，直接返回
        if proc.stdout.strip():
            return proc.stdout, ""

        # 工作区干净，检查是否有新提交（agent 可能提交了代码）
        # 获取 commit 数量，baseline commit 应该只有 1 个
        commit_count_proc = subprocess.run(
            ["docker", "exec", self.container_name, "sh", "-c",
             f"cd {ws_path}/repo && git rev-list --count HEAD"],
            capture_output=True, text=True
        )

        if commit_count_proc.returncode == 0:
            try:
                commit_count = int(commit_count_proc.stdout.strip())
                if commit_count > 1:
                    # 有新提交，生成从第一个 commit（baseline）到 HEAD 的 diff
                    baseline_proc = subprocess.run(
                        ["docker", "exec", self.container_name, "sh", "-c",
                         f"cd {ws_path}/repo && git rev-list --max-parents=0 HEAD"],
                        capture_output=True, text=True
                    )
                    if baseline_proc.returncode == 0:
                        baseline_commit = baseline_proc.stdout.strip()
                        diff_proc = subprocess.run(
                            ["docker", "exec", self.container_name, "sh", "-c",
                             f"cd {ws_path}/repo && git diff {baseline_commit}..HEAD"],
                            capture_output=True, text=True
                        )
                        if diff_proc.returncode == 0:
                            return diff_proc.stdout, ""
            except ValueError:
                pass

        # 都没有，返回空（真的没有任何改动）
        return "", ""

    def _ensure_patch_written(self, ws_path: str, diff_text: str):
        """由脚本兜底把 diff 写入 patches/refactor.patch，不依赖 agent 自觉执行。"""
        subprocess.run(
            ["docker", "exec", "-i", self.container_name, "sh", "-c",
             f"mkdir -p {ws_path}/patches && cat > {ws_path}/patches/refactor.patch"],
            input=diff_text.encode(), capture_output=True
        )

    def _export_trajectory(self, ws_path: str, session_id: str) -> Optional[str]:
        """把 Claude Code 原生会话轨迹拷到工作空间 agent_session/trajectory.jsonl。

        Claude Code 会把整段会话（含 thinking / tool_use / tool_result / 最终
        result）写到 ~/.claude/projects/<cwd转义>/<session_id>.jsonl。我们不去
        推导那串脆弱的转义目录名，而是用固定的 session_id 作为文件名锚点，直接
        在容器里 find 出唯一匹配再 cp 出来。best-effort：找不到只告警、返回 None，
        不影响重构成败判定（成败仍以 git diff 为准）。
        """
        dst = f"{ws_path}/agent_session/trajectory.jsonl"
        proc = subprocess.run(
            ["docker", "exec", self.container_name, "sh", "-c",
             f'src=$(find "$HOME/.claude/projects" -name "{session_id}.jsonl" 2>/dev/null | head -1); '
             f'[ -n "$src" ] || exit 3; '
             f'mkdir -p {ws_path}/agent_session && cp "$src" {dst}'],
            capture_output=True, text=True
        )
        if proc.returncode == 0:
            print(f"  ✅ 修复轨迹已导出: {dst}")
            return dst
        print(f"  ⚠️  未找到会话轨迹（session_id={session_id}），跳过导出")
        return None

    def _diff_stat(self, ws_path: str) -> str:
        """返回 git diff --stat 摘要，便于日志中快速判断改动规模。"""
        proc = subprocess.run(
            ["docker", "exec", self.container_name, "sh", "-c",
             f"cd {ws_path}/repo && git add -A -N . >/dev/null 2>&1; git diff HEAD --stat"],
            capture_output=True, text=True
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def _read_agent_report(self, ws_path: str):
        """尝试读取 agent 自报的 refactor_result.json，读不到返回 None（不阻塞）。"""
        proc = subprocess.run(
            ["docker", "exec", self.container_name, "cat",
             f"{ws_path}/refactor_result.json"],
            capture_output=True, text=True
        )
        if proc.returncode != 0:
            return None
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {"_raw": proc.stdout[-1000:], "_note": "json 解析失败"}

    def _save_all_results(self, results: List[Dict]):
        """保存所有工作空间的结果"""
        for result in results:
            ws_name = result.get("workspace", "unknown")
            output_file = self.results_dir / f"{ws_name}.json"
            with open(output_file, "w") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)

        print(f"\n结果已保存到: {self.results_dir}")


def main():
    parser = argparse.ArgumentParser(
        description="创建工作空间并执行重构任务"
    )
    parser.add_argument("--case", required=True, help="Case 目录路径")
    parser.add_argument("--config", help="配置文件路径（默认使用 scripts/evaluation/config.yaml）")
    parser.add_argument("--models", nargs="+", help="模型列表（覆盖配置文件），如: claude-opus-4 gpt-4")
    parser.add_argument("--iterations", type=int, help="每个模型的迭代次数（覆盖配置文件）")
    parser.add_argument("--parallel", type=int, default=None,
                       help="并行执行数量（覆盖配置文件 agent.parallel；未指定则用配置值，默认 1 串行）")
    parser.add_argument("--workspace", help="只执行指定的工作空间（如 claude-opus-4__01）")
    parser.add_argument("--recreate", action="store_true",
                       help="强制重新创建工作空间（即使已存在）")

    args = parser.parse_args()

    case_path = Path(args.case)
    if not case_path.exists():
        print(f"❌ Case 目录不存在: {case_path}")
        return 1

    # 加载配置（供自动创建工作空间使用）
    config = load_config(
        config_file=Path(args.config) if args.config else None,
        models=args.models,
        iterations=args.iterations,
        parallel=args.parallel
    )

    model_desc = ", ".join(f"{s['name']}({s['vendor']})" for s in config.model_specs)
    print(f"配置: 模型=[{model_desc}], 迭代次数={config.iterations}, 并行度={config.parallel}")

    runner = RefactorRunner(
        case_path=case_path,
        config=config,
        parallel=config.parallel,
        target_workspace=args.workspace,
        recreate=args.recreate,
    )
    return runner.run()


if __name__ == "__main__":
    sys.exit(main())
