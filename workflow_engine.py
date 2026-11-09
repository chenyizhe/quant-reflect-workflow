"""
workflow_engine.py — 声明式 DAG 工作流引擎

核心语义：
  1. 变量绑定驱动依赖 —— 节点 prompt 中引用的 {{var}} 若由上游节点产出，就自动连边。
  2. 拓扑排序执行，变量按序注入全局上下文。
  3. 渲染阶段严格模式：引用未定义变量立即报错，不静默留空。
  4. 支持 --validate（纯静态校验）与 --dry-run（不烧 token 跑通全链路）。

零第三方依赖（除 pyyaml 解析配置文件）。
"""

from __future__ import annotations

import ast
import hashlib
import subprocess
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml

# {{var}} 或 {{var|section:修复指令}}
VAR_RE = re.compile(
    r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\|\s*section\s*[:：]\s*([^}]+?)\s*)?\}\}"
)
SECTION_RE_TMPL = r"^#{1,6}\s*{name}\s*$(.*?)(?=^#{1,6}\s|\Z)"
CODE_FENCE_RE = re.compile(r"```(?:python3?|py)?[ \t]*\n(.*?)```", re.S)
INPUT_ORIGIN = "$input"

# builtin 运行时合法档位（平台路由偏好，不是模型 ID）
VALID_PROFILES = ("default", "lite", "reasoning")


def ast_hash(code: str) -> str:
    """
    代码的 AST 规范化哈希：忽略注释、空行、格式差异，只比结构。
    用于返修早停——连续两轮结构相同说明模型在原地打转。
    """
    try:
        tree = ast.parse(code)  # type: ignore[name-defined]
        dumped = ast.dump(tree, annotate_fields=False)  # type: ignore[name-defined]
    except Exception:  # noqa: BLE001
        dumped = code
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()[:16]


def extract_section(text: str, name: str) -> str:
    """取 Markdown 里标题为 name 的段落；找不到时原样返回全文（保守，不丢信息）。"""
    pat = re.compile(
        r"^#{1,6}\s*" + re.escape(name) + r"\s*$(.*?)(?=^#{1,6}\s|\Z)",
        re.M | re.S,
    )
    m = pat.search(text or "")
    return m.group(1).strip() if m else (text or "").strip()


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------
class WorkflowError(Exception):
    """工作流运行期错误。"""


class ValidationError(WorkflowError):
    """DAG / 变量绑定静态校验失败。"""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("；".join(problems))


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------
@dataclass
class ModelSpec:
    alias: str
    provider: str
    model: str
    temperature: float = 0.3
    max_tokens: int = 8192
    effort: str | None = None
    agent_profile: str | None = None   # builtin 运行时用：lite / reasoning / default


@dataclass
class NodeSpec:
    id: str
    name: str
    model: str
    outputs: list[str]
    system_prompt: str = ""
    user_prompt: str = ""
    depends_on: list[str] | None = None
    agent_profile: str | None = None   # 节点级档位；builtin.lock=true 时禁止与全局冲突
    verify: dict | None = None          # 自检配置：失败则回灌给同节点返修
    best_of_n: dict | None = None       # 多路径采样：生成 N 份候选后择优
    precheck: dict | None = None        # 渲染前跑一次检查，把日志注入上下文
    refs: set[str] = field(default_factory=set)
    upstream: list[str] = field(default_factory=list)


@dataclass
class NodeTrace:
    node_id: str
    name: str
    model: str
    elapsed_s: float
    prompt_tokens: int
    completion_tokens: int
    output_chars: int
    status: str = "ok"
    error: str = ""
    finish_reason: str = ""
    token_budget: int = 0


# 用于从旧 trace.json 还原记录（续跑时保留历史消耗）
_TRACE_DEFAULTS: dict[str, Any] = {
    "node_id": "", "name": "", "model": "", "elapsed_s": 0.0,
    "prompt_tokens": 0, "completion_tokens": 0, "output_chars": 0,
    "status": "ok", "error": "", "finish_reason": "", "token_budget": 0,
}


# --------------------------------------------------------------------------
# 引擎
# --------------------------------------------------------------------------
class Workflow:
    def __init__(self, config: dict, config_path: Path):
        self.config = config
        self.path = config_path
        self.name: str = config.get("name", config_path.stem)
        self.version: str = str(config.get("version", "1"))
        self.inputs: dict[str, dict] = config.get("inputs") or {}
        self.providers: dict[str, dict] = config.get("providers") or {}
        self.raw_models: dict[str, dict] = config.get("models") or {}
        self.output_map: dict[str, str] = config.get("outputs") or {}
        self.artifacts: dict[str, dict] = config.get("artifacts") or {}

        # ---------- builtin 档位（全局锁定）----------
        builtin = config.get("builtin") or {}
        self.builtin_profile: str | None = builtin.get("profile")
        self.builtin_lock: bool = bool(builtin.get("lock", False))
        self.runtime: str = str(config.get("runtime", "api"))
        if self.builtin_profile and self.builtin_profile not in VALID_PROFILES:
            raise ValidationError([
                f"builtin.profile 非法: {self.builtin_profile}（合法值: {', '.join(VALID_PROFILES)}）"
            ])
        if self.runtime == "builtin" and self.builtin_lock and not self.builtin_profile:
            raise ValidationError(["builtin.lock=true 时必须提供 builtin.profile"])

        self.models: dict[str, ModelSpec] = {
            alias: ModelSpec(
                alias=alias,
                provider=spec.get("provider", "default"),
                model=spec["model"],
                temperature=float(spec.get("temperature", 0.3)),
                max_tokens=int(spec.get("max_tokens", 8192)),
                effort=spec.get("effort"),
                agent_profile=spec.get("agent_profile"),
            )
            for alias, spec in self.raw_models.items()
        }

        self.nodes: list[NodeSpec] = [
            NodeSpec(
                id=str(n["id"]),
                name=str(n.get("name", n["id"])),
                model=str(n.get("model", "")),
                outputs=list(n.get("outputs") or []),
                system_prompt=str(n.get("system_prompt") or ""),
                user_prompt=str(n.get("user_prompt") or ""),
                depends_on=list(n["depends_on"]) if n.get("depends_on") else None,
                agent_profile=n.get("agent_profile"),
                verify=dict(n["verify"]) if n.get("verify") else None,
                best_of_n=dict(n["best_of_n"]) if n.get("best_of_n") else None,
                precheck=dict(n["precheck"]) if n.get("precheck") else None,
            )
            for n in (config.get("nodes") or [])
        ]

        self.producer: dict[str, str] = {}   # var -> node id（或 $input）
        self.order: list[str] = []
        self._build_graph()
        self._topological_sort()
        self._bind_dependencies()

    # ---------------- 加载 ----------------
    @classmethod
    def load(cls, path: str | Path) -> "Workflow":
        p = Path(path).expanduser().resolve()
        if not p.exists():
            raise WorkflowError(f"配置文件不存在: {p}")
        with p.open("r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
        if not isinstance(cfg, dict):
            raise ValidationError(["配置文件顶层必须是 mapping"])
        return cls(cfg, p)

    # ---------------- 图构建 ----------------
    @staticmethod
    def template_refs(text: str) -> set[str]:
        # 用 finditer 取第 1 组：正则现在有两个 group（变量名 + section）
        return {m.group(1) for m in VAR_RE.finditer(text or "")}

    def node_refs(self, node: NodeSpec) -> set[str]:
        return self.template_refs(node.system_prompt) | self.template_refs(node.user_prompt)

    def _build_graph(self) -> None:
        problems: list[str] = []

        if not self.nodes:
            problems.append("nodes 为空，至少需要一个节点")

        for v in self.inputs:
            self.producer[v] = INPUT_ORIGIN

        seen_ids: set[str] = set()
        for node in self.nodes:
            if node.id in seen_ids:
                problems.append(f"节点 id 重复: {node.id}")
            seen_ids.add(node.id)
            for var in node.outputs:
                if var in self.producer:
                    problems.append(f"输出变量重复声明: {var} (由 {self.producer[var]} 与 {node.id} 同时产出)")
                self.producer[var] = node.id
            node.refs = self.node_refs(node)

        if problems:
            raise ValidationError(problems)

    def _topological_sort(self) -> None:
        ids = [n.id for n in self.nodes]
        id_set = set(ids)

        # 显式依赖 + 变量引用推断出的隐式依赖
        deps: dict[str, set[str]] = {n.id: set() for n in self.nodes}
        for node in self.nodes:
            for dep in node.depends_on or []:
                if dep not in id_set:
                    raise ValidationError([f"节点 {node.id} 声明了不存在的依赖: {dep}"])
                deps[node.id].add(dep)
            for var in node.refs:
                src = self.producer.get(var)
                if src and src != INPUT_ORIGIN and src != node.id:
                    deps[node.id].add(src)

        self._deps = deps

        # Kahn
        remaining = {nid: set(d) for nid, d in deps.items()}
        ordered: list[str] = []
        while remaining:
            ready = [nid for nid in ids if nid in remaining and not remaining[nid]]
            if not ready:
                raise ValidationError([f"DAG 存在循环依赖: {' -> '.join(sorted(remaining))}"])
            for nid in ready:
                ordered.append(nid)
                remaining.pop(nid)
            for nid in remaining:
                remaining[nid] -= set(ready)

        self.order = ordered

    def _bind_dependencies(self) -> None:
        by_id = {n.id: n for n in self.nodes}
        for nid in self.order:
            node = by_id[nid]
            node.upstream = [u for u in self.order if u in self._deps[nid]]

    # ---------------- 档位解析 ----------------
    def resolve_profile(self, node: NodeSpec) -> tuple[str, str]:
        """
        解析节点最终生效的内置模型档位。

        优先级（lock=false）: 节点 agent_profile > 模型别名 agent_profile > builtin.profile > default
        lock=true 时: 强制 builtin.profile，任何与之冲突的声明都视为配置错误。

        返回 (profile, 来源标记)。
        """
        model_spec = self.models.get(node.model)
        declared: list[tuple[str, str]] = []
        if node.agent_profile:
            declared.append(("node", str(node.agent_profile)))
        if model_spec and model_spec.agent_profile:
            declared.append((f"models.{node.model}", str(model_spec.agent_profile)))

        for src, prof in declared:
            if prof not in VALID_PROFILES:
                raise ValidationError([
                    f"节点 {node.id} 的 {src} 档位非法: {prof}"
                    f"（合法值: {', '.join(VALID_PROFILES)}）"
                ])

        if self.builtin_lock and self.builtin_profile:
            conflicts = [f"{src}={prof}" for src, prof in declared if prof != self.builtin_profile]
            if conflicts:
                raise ValidationError([
                    f"builtin.lock=true 但节点 {node.id} 存在冲突档位声明: "
                    f"{', '.join(conflicts)}（全局锁定为 {self.builtin_profile}）。"
                    f"锁定模式下不允许逐点覆盖，请删除该声明或把 lock 改为 false。"
                ])
            return self.builtin_profile, "global-lock"

        if node.agent_profile:
            return node.agent_profile, "node"
        if model_spec and model_spec.agent_profile:
            return str(model_spec.agent_profile), f"models.{node.model}"
        if self.builtin_profile:
            return self.builtin_profile, "global"
        return "default", "fallback"

    # ---------------- 校验 ----------------
    def validate(self, require_inputs: bool = False, provided: dict | None = None) -> dict:
        """静态校验变量绑定；返回可直接打印的绑定表。"""
        problems: list[str] = []
        by_id = {n.id: n for n in self.nodes}

        # 1) 节点必须引用已声明的模型别名
        for node in self.nodes:
            if not node.model:
                problems.append(f"节点 {node.id} 未指定 model")
            elif node.model not in self.models:
                problems.append(f"节点 {node.id} 引用了未定义的模型别名: {node.model}")
            if not node.outputs:
                problems.append(f"节点 {node.id} 未声明 outputs")
            if node.verify:
                if not str(node.verify.get("command") or "").strip():
                    problems.append(f"节点 {node.id} 的 verify 缺少 command")
                rounds = node.verify.get("max_rounds", 0)
                if not isinstance(rounds, int) or isinstance(rounds, bool) or rounds < 0:
                    problems.append(f"节点 {node.id} 的 verify.max_rounds 必须是非负整数")
                if not node.outputs:
                    problems.append(f"节点 {node.id} 配置了 verify 但没有 outputs，无法定位待验证产物")
            if node.best_of_n:
                cnt = node.best_of_n.get("n", 1)
                if not isinstance(cnt, int) or isinstance(cnt, bool) or cnt < 1:
                    problems.append(f"节点 {node.id} 的 best_of_n.n 必须是 >= 1 的整数")
                div = str(node.best_of_n.get("diversity", "orthogonal"))
                if div not in ("orthogonal", "sample"):
                    problems.append(
                        f"节点 {node.id} 的 best_of_n.diversity 必须是 orthogonal（正交范式）"
                        f" 或 sample（同 prompt 重复采样），收到 {div}"
                    )
                sel = str(node.best_of_n.get("select", "rule"))
                if sel != "rule":
                    problems.append(
                        f"节点 {node.id} 的 best_of_n.select 目前只支持 rule（规则打分），收到 {sel}"
                    )
                if not node.outputs:
                    problems.append(f"节点 {node.id} 配置了 best_of_n 但没有 outputs")
            if node.precheck:
                for key in ("input", "inject_as"):
                    if not str(node.precheck.get(key) or "").strip():
                        problems.append(f"节点 {node.id} 的 precheck 缺少 {key}")
                if str(node.precheck.get("level", "runtime")) not in ("syntax","import","contract","runtime","run"):
                    problems.append(f"节点 {node.id} 的 precheck.level 非法")

        # 2) 按拓扑序检查引用可达性（每个引用必须在此刻已定义）
        defined = set(self.inputs)
        # precheck 注入变量由 orchestrator 在渲染前填充，不是任何节点的 output
        for _n in self.nodes:
            if _n.precheck and str(_n.precheck.get("inject_as") or "").strip():
                defined.add(str(_n.precheck["inject_as"]))
        binding_rows: list[dict] = []
        for nid in self.order:
            node = by_id[nid]
            for var in sorted(node.refs):
                if var not in defined:
                    problems.append(
                        f"节点 {node.id} 引用了未定义变量 {{{{{var}}}}}"
                        f"（当前可用: {', '.join(sorted(defined)) or '无'}）"
                    )
            defined |= set(node.outputs)
            try:
                prof, prof_src = self.resolve_profile(node)
            except ValidationError as exc:
                problems.extend(exc.problems)
                prof, prof_src = "?", "conflict"
            binding_rows.append(
                {
                    "node": node.id,
                    "name": node.name,
                    "model_alias": node.model,
                    "model": self.models[node.model].model if node.model in self.models else "?",
                    "profile": prof,
                    "profile_source": prof_src,
                    "reads": sorted(node.refs),
                    "writes": node.outputs,
                    "upstream": node.upstream,
                }
            )

        # 3) 最终输出映射
        for key, tpl in self.output_map.items():
            for var in self.template_refs(str(tpl)):
                if var not in defined:
                    problems.append(f"outputs.{key} 引用了未定义变量 {{{{{var}}}}}")

        # 4) 运行期输入检查
        if require_inputs:
            provided = provided or {}
            for name, spec in self.inputs.items():
                if spec.get("required", False) and not str(provided.get(name, "")).strip():
                    problems.append(f"缺少必填输入变量: {name}")

        if problems:
            raise ValidationError(problems)

        return {
            "workflow": self.name,
            "version": self.version,
            "runtime": self.runtime,
            "builtin": {
                "profile": self.builtin_profile,
                "lock": self.builtin_lock,
            },
            "order": self.order,
            "bindings": binding_rows,
            "inputs": list(self.inputs),
            "outputs": self.output_map,
        }

    def profile_summary(self, report: dict) -> tuple[set[str], bool]:
        """返回 (全部节点生效档位集合, 是否一致)。用于锁定自检。"""
        profiles = {b["profile"] for b in report["bindings"]}
        return profiles, len(profiles) == 1

    # ---------------- 渲染 ----------------
    def render(self, text: str, ctx: dict[str, Any], where: str) -> str:
        missing: list[str] = []

        def _sub(m: re.Match) -> str:
            var, section = m.group(1), m.group(2)
            if var not in ctx:
                missing.append(var)
                return m.group(0)
            value = str(ctx[var])
            if section:
                value = extract_section(value, section.strip())
            return value

        out = VAR_RE.sub(_sub, text or "")
        if missing:
            raise WorkflowError(
                f"{where} 渲染失败，缺失变量: {sorted(set(missing))}"
            )
        return out

    # ---------------- 执行 ----------------
    def _run_precheck_sync(self, node: NodeSpec, ctx: dict, out_dir: Path) -> str:
        """对 node.precheck.input 指定的产物跑 check_code，返回日志文本。"""
        cfg = node.precheck or {}
        src_var = str(cfg.get("input") or "")
        level = str(cfg.get("level", "runtime"))
        src = str(ctx.get(src_var, "")).strip()
        if not src:
            return f"（前置检查跳过：上游变量 {src_var} 尚未产出）"
        tmp = out_dir / f"_precheck_{src_var}.py"
        tmp.write_text(src, encoding="utf-8")
        tool = Path(__file__).parent / "tools" / "check_code.py"
        try:
            proc = subprocess.run(
                [sys.executable, str(tool), str(tmp), "--level", level],
                capture_output=True, text=True, timeout=300,
            )
            body = (proc.stdout + proc.stderr).strip()
            verdict = "通过" if proc.returncode == 0 else "未通过"
            return (f"命令: check_code --level {level}\n"
                    f"退出码: {proc.returncode}（{verdict}）\n\n{body[-4000:]}")
        except subprocess.TimeoutExpired:
            return "命令超时（>300s）"
        finally:
            tmp.unlink(missing_ok=True)

    def _run_verify_sync(self, node: NodeSpec, out_dir: Path) -> tuple[bool, str]:
        """执行节点 verify.command，返回 (是否通过, 日志)。"""
        cfg = node.verify or {}
        if not cfg or not node.outputs:
            return True, ""
        art = self.artifacts.get(node.outputs[0], {})
        art_path = out_dir / str(art.get("path", f"{node.outputs[0]}.txt"))
        if not art_path.exists():
            return False, f"产物不存在: {art_path}"
        cmd = str(cfg.get("command", "{python} {artifact}")).format(
            python=sys.executable, artifact=str(art_path), run_dir=str(out_dir),
            node=node.id, workflow_dir=str(self.path.parent),
        )
        try:
            proc = subprocess.run(cmd, shell=True, capture_output=True,
                                  text=True, timeout=float(cfg.get("timeout", 300)))
        except subprocess.TimeoutExpired:
            return False, f"verify 超时（{cfg.get('timeout', 300)}s）"
        tail = int(cfg.get("log_tail_chars", 3000))
        body = (proc.stdout + proc.stderr).strip()
        return proc.returncode == 0, body[-tail:]

    def run(
        self,
        inputs: dict[str, Any],
        run_dir: str | Path | None = None,
        dry_run: bool = False,
        on_event: Callable[[str, dict], None] | None = None,
        resume_from: str | Path | None = None,
    ) -> dict:
        emit = on_event or (lambda _e, _p: None)
        report = self.validate(require_inputs=True, provided=inputs)

        run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        if run_dir:
            out_dir = Path(run_dir).expanduser().resolve()
        elif resume_from:
            out_dir = Path(resume_from).expanduser().resolve()
        else:
            out_dir = self.path.parent / "runs" / run_id
        out_dir.mkdir(parents=True, exist_ok=True)
        self.last_run_dir = out_dir

        ctx: dict[str, Any] = dict(inputs)
        prev_traces: dict[str, dict] = {}
        if resume_from:
            src = Path(resume_from).expanduser().resolve()
            snapshot = src / "variables.json"
            if not snapshot.exists():
                raise WorkflowError(f"断点续跑失败：{snapshot} 不存在")
            prev = json.loads(snapshot.read_text(encoding="utf-8"))
            produced = {v for n in self.nodes for v in n.outputs}
            for k, v in prev.items():
                if k in produced:
                    ctx[k] = v
            # 保留历史 trace，续跑不丢已消耗的 token 记录
            trace_file = src / "trace.json"
            if trace_file.exists():
                for rec in json.loads(trace_file.read_text(encoding="utf-8")).get("nodes", []):
                    prev_traces[rec.get("node_id", "")] = rec
            emit("resume", {"from": str(snapshot), "reused": sorted(produced & set(ctx))})

        traces: list[NodeTrace] = []
        by_id = {n.id: n for n in self.nodes}
        t_start = time.time()

        emit("start", {"run_dir": str(out_dir), "order": self.order, "dry_run": dry_run})

        for idx, nid in enumerate(self.order, start=1):
            node = by_id[nid]
            model_spec = self.models[node.model]

            # 断点续跑：上游产物已存在则复用，不重复烧 token
            if node.outputs and all(v in ctx for v in node.outputs):
                emit("node_cached", {"idx": idx, "total": len(self.order), "node": node})
                old = prev_traces.get(nid)
                if old:
                    fields = {k: old.get(k, v) for k, v in _TRACE_DEFAULTS.items()}
                    traces.append(NodeTrace(**{**fields, "status": "cached"}))
                else:
                    traces.append(
                        NodeTrace(nid, node.name, model_spec.model, 0.0, 0, 0,
                                  len(str(ctx[node.outputs[0]])), status="cached")
                    )
                continue

            emit("node_start", {"idx": idx, "total": len(self.order), "node": node, "model": model_spec})

            # 指令 6：api 模式也真实跑前置检查，与 builtin 行为对齐
            if node.precheck:
                _dst = str(node.precheck.get("inject_as") or "").strip()
                if _dst and _dst not in ctx:
                    ctx[_dst] = self._run_precheck_sync(node, ctx, out_dir)
            system = self.render(node.system_prompt, ctx, f"节点 {node.id} system_prompt")
            base_user = self.render(node.user_prompt, ctx, f"节点 {node.id} user_prompt")
            max_rounds = int((node.verify or {}).get("max_rounds", 0)) if node.verify else 0
            repair_note = ""
            trace = None

            for attempt in range(max_rounds + 1):
                user = base_user + repair_note

                system = self.render(node.system_prompt, ctx, f"节点 {node.id} system_prompt")
                user = self.render(node.user_prompt, ctx, f"节点 {node.id} user_prompt")

                t0 = time.time()
                try:
                    if dry_run:
                        content = self._mock_completion(node, user)
                        usage = {"prompt_tokens": 0, "completion_tokens": 0}
                        meta = {"finish_reason": "dry-run", "token_budget": 0}
                    else:
                        content, usage, meta = self._call_llm(model_spec, system, user, node.id)
                except Exception as exc:  # noqa: BLE001
                    traces.append(
                        NodeTrace(nid, node.name, model_spec.model, time.time() - t0, 0, 0, 0,
                                  status="error", error=str(exc))
                    )
                    emit("node_error", {"node": node, "error": str(exc)})
                    self._persist_meta(out_dir, report, inputs, ctx, traces, run_id, dry_run, t_start)
                    raise

                # 按产物类型决定是否剥离代码围栏
                artifact = self.artifacts.get(node.outputs[0], {}) if node.outputs else {}
                lang = str(artifact.get("lang", "")).lower()
                value = extract_code(content) if lang == "python" else content.strip()

                for var in node.outputs:
                    ctx[var] = value

                trace = NodeTrace(
                    node_id=nid,
                    name=node.name,
                    model=model_spec.model,
                    elapsed_s=round(time.time() - t0, 2),
                    prompt_tokens=int(usage.get("prompt_tokens", 0)),
                    completion_tokens=int(usage.get("completion_tokens", 0)),
                    output_chars=len(value),
                    finish_reason=str(meta.get("finish_reason", "")),
                    token_budget=int(meta.get("token_budget", 0)),
                )
                traces.append(trace)

                for var in node.outputs:
                    art = self.artifacts.get(var)
                    if art:
                        ap = out_dir / str(art["path"])
                        ap.write_text(str(ctx[var]).rstrip() + "\n", encoding="utf-8")
                        emit("artifact", {"var": var, "path": str(ap)})

                emit("node_done", {"node": node, "trace": trace})

                # 指令 6.2：产物落盘后真实验证，失败则把日志拼进 prompt 重试
                if dry_run or not node.verify:
                    break
                ok_v, log_v = self._run_verify_sync(node, out_dir)
                if ok_v:
                    emit("verify_ok", {"node": node, "attempt": attempt})
                    break
                if attempt >= max_rounds:
                    raise WorkflowError(
                        f"节点 {nid} 自检失败且已达返修上限 {max_rounds} 轮：\n{log_v[-1500:]}"
                    )
                emit("verify_retry", {"node": node, "attempt": attempt, "log": log_v[-600:]})
                repair_note = ("\n\n## 上一轮自检失败（必须修复；不要重复提交结构相同的代码）\n"
                               "```\n" + log_v + "\n```")

            emit("node_done", {"node": node, "trace": trace})

        final: dict[str, str] = {}
        for key, tpl in self.output_map.items():
            final[key] = self.render(str(tpl), ctx, f"outputs.{key}")

        total = round(time.time() - t_start, 2)
        emit("done", {"elapsed_s": total, "final": final, "run_dir": str(out_dir)})
        self._persist_meta(out_dir, report, inputs, ctx, traces, run_id, dry_run, t_start)
        return {
            "run_id": run_id,
            "run_dir": str(out_dir),
            "context": ctx,
            "final": final,
            "traces": traces,
            "elapsed_s": total,
            "binding": report,
        }

    # ---------------- 落盘 ----------------
    def _persist_meta(self, out_dir, report, inputs, ctx, traces, run_id, dry_run, t_start) -> None:
        (out_dir / "variables.json").write_text(
            json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        trace_payload = {
            "workflow": self.name,
            "version": self.version,
            "run_id": run_id,
            "dry_run": dry_run,
            "inputs": inputs,
            "order": self.order,
            "model_map": {a: {"provider": m.provider, "model": m.model,
                              "temperature": m.temperature, "max_tokens": m.max_tokens,
                              "effort": m.effort}
                          for a, m in self.models.items()},
            "nodes": [t.__dict__ for t in traces],
            "total_elapsed_s": round(time.time() - t_start, 2),
            "total_tokens": {
                "prompt": sum(t.prompt_tokens for t in traces),
                "completion": sum(t.completion_tokens for t in traces),
            },
        }
        (out_dir / "trace.json").write_text(
            json.dumps(trace_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---------------- LLM 调用 ----------------
    def _call_llm(self, spec: ModelSpec, system: str, user: str,
                  node_id: str, retries: int = 3,
                  hard_cap: int = 65536) -> tuple[str, dict, dict]:
        """
        带预算自适应重试的调用。

        推理型模型的 reasoning_tokens 与 content 共享 max_tokens 配额：
        budget 吃满时 finish_reason=length 且 content 为空。此时自动翻倍预算重试，
        而不是把空字符串当成有效输出。
        """
        provider = self.providers.get(spec.provider)
        if not provider:
            raise WorkflowError(f"节点 {node_id} 的 provider 未配置: {spec.provider}")

        api_key = os.environ.get(provider.get("api_key_env", ""), "")
        if not api_key:
            raise WorkflowError(
                f"环境变量 {provider.get('api_key_env')} 为空，无法调用 {spec.provider}"
            )

        url = provider["base_url"].rstrip("/") + "/chat/completions"
        timeout = int(provider.get("timeout", 300))

        budget = spec.max_tokens
        attempt = 0
        last_err: Exception | None = None

        while attempt < retries:
            attempt += 1
            payload: dict[str, Any] = {
                "model": spec.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": spec.temperature,
                "max_tokens": budget,
                "stream": False,
            }
            if spec.effort:
                payload["effort"] = spec.effort

            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                choice = data["choices"][0]
                content = (choice.get("message", {}).get("content") or "").strip()
                finish = str(choice.get("finish_reason") or "")
                usage = data.get("usage", {}) or {}
                meta = {"finish_reason": finish, "token_budget": budget}

                if content:
                    return content, usage, meta

                # 空 content：多半是 reasoning 吃光了预算
                reasoning_tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
                if finish == "length" and budget < hard_cap:
                    budget = min(budget * 2, hard_cap)
                    continue
                raise WorkflowError(
                    f"节点 {node_id} 返回空内容 "
                    f"(finish={finish}, reasoning_tokens={reasoning_tokens}, budget={budget})"
                )

            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "ignore")[:300]
                if exc.code == 402:
                    raise WorkflowError(
                        f"节点 {node_id}: 账户余额不足（HTTP 402）。"
                        f"请充值或在 workflow.yaml 里切换 provider/模型。原始响应: {detail}"
                    ) from exc
                if exc.code == 401:
                    raise WorkflowError(
                        f"节点 {node_id}: API Key 无效或已过期（HTTP 401）: {detail}"
                    ) from exc
                last_err = WorkflowError(f"HTTP {exc.code}: {detail}")
                if exc.code not in (408, 409, 429, 500, 502, 503, 504):
                    raise last_err from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_err = exc

            if attempt < retries:
                time.sleep(2 ** attempt)

        raise WorkflowError(f"节点 {node_id} 调用失败（重试 {retries} 次）: {last_err}")

    # ---------------- dry-run 模拟 ----------------
    @staticmethod
    def _mock_completion(node: NodeSpec, rendered_user: str) -> str:
        head = rendered_user.strip().splitlines()[0][:60] if rendered_user.strip() else ""
        if "final_code" in node.outputs:
            return (
                "```python\n"
                "# [DRY-RUN] 重构修复者占位输出\n"
                "# 已修复: 1) 前视偏差 2) NaN/停牌处理 3) 边界条件\n"
                f"# 上游意见摘要: {head}\n"
                "def run_strategy(*args, **kwargs):\n"
                "    raise NotImplementedError('dry-run placeholder')\n"
                "```"
            )
        if "draft_code" in node.outputs:
            return (
                "```python\n"
                "# [DRY-RUN] 初稿生成器占位输出\n"
                f"# 任务: {head}\n"
                "def run_strategy(*args, **kwargs):\n"
                "    raise NotImplementedError('dry-run placeholder')\n"
                "```"
            )
        return (
            "# [DRY-RUN] 审查官占位输出\n"
            f"1. 前视偏差: 占位（上游长度 {len(rendered_user)} 字符）\n"
            "2. 数据稳健性: 占位\n"
            "3. 计算效率: 占位\n"
        )


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def extract_code(text: str) -> str:
    """从模型输出中抽取代码：优先取最长的围栏代码块，否则原文。"""
    blocks = CODE_FENCE_RE.findall(text or "")
    if blocks:
        return max(blocks, key=len).strip()
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return stripped
