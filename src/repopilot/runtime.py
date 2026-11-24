"""Composition root for CLI and future HTTP/IDE adapters."""

from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from pathlib import Path

from .config import AppConfig, load_config
from .contracts import ReviewReport, RunPhase, RunState, TaskRequest
from .editing import PatchLifecycleService, PatchProposalWorkflow
from .git import GitClient
from .llm import ModelClient, ModelNotConfigured, UnconfiguredModelClient
from .patching import PatchError, PatchRunRecord, UnifiedDiffPatchEngine
from .persistence import PatchRunStore
from .planning import ReadOnlyPlanningWorkflow
from .policy import PermissionPolicy
from .providers import ProviderRegistry
from .repair import RepairWorkflow
from .repo_map import RepoMap
from .report import RunReport, build_report
from .review import ReviewWorkflow
from .sandbox import build_verification_runner
from .tools.base import ToolContext, ToolRegistry
from .tools.repository import (
    GitStatusTool,
    ReadFileTool,
    RepositoryInventoryTool,
    SearchCodeTool,
)
from .verification import (
    VerificationOutcome,
    VerificationWhitelist,
    VerificationWorkflow,
)
from .workflow import WorkflowEngine
from .workspace import RepositoryWorkspace


@dataclass(slots=True)
class RepoPilotRuntime:
    config: AppConfig
    workspace: RepositoryWorkspace
    git: GitClient
    policy: PermissionPolicy
    tools: ToolRegistry
    workflow: WorkflowEngine
    model: ModelClient
    providers: ProviderRegistry

    @property
    def tool_context(self) -> ToolContext:
        return ToolContext(self.workspace, self.git, self.policy)

    def start_task(self, goal: str) -> RunState:
        task = TaskRequest(goal=goal, repository=str(self.workspace.root))
        state = RunState(task=task)
        return self.workflow.transition(state, RunPhase.DISCOVER)

    def plan_task(self, goal: str) -> RunState:
        state = self.start_task(goal)
        planner = ReadOnlyPlanningWorkflow(
            model=self.model,
            tools=self.tools,
            tool_context=self.tool_context,
            workflow=self.workflow,
            settings=self.config.workspace,
        )
        return planner.run(state)

    def propose_task(self, goal: str, store: PatchRunStore) -> PatchRunRecord:
        initial_dirty_paths = tuple(sorted(self.git.dirty_paths()))
        state = self.plan_task(goal)
        if state.phase is not RunPhase.WAIT_APPROVAL:
            raise PatchError(state.error or "P0 规划未完成，不能生成补丁")
        patch_engine = UnifiedDiffPatchEngine(self.workspace, self.git)
        editor = PatchProposalWorkflow(
            model=self.model,
            tools=self.tools,
            tool_context=self.tool_context,
            settings=self.config.workspace,
            patch_engine=patch_engine,
            store=store,
            initial_dirty_paths=initial_dirty_paths,
        )
        return editor.propose(state)

    def patch_lifecycle(self, store: PatchRunStore) -> PatchLifecycleService:
        return PatchLifecycleService(
            patch_engine=UnifiedDiffPatchEngine(self.workspace, self.git),
            policy=self.policy,
            store=store,
        )

    def verification_workflow(self, store: PatchRunStore) -> VerificationWorkflow:
        sandbox = self.config.sandbox
        if sandbox.mode == "remote":
            # 远端服务是动作可用性的权威；本地只按项目类型推断动作名。
            whitelist = VerificationWhitelist.discover(
                self.workspace.root,
                python_executable="python",
                which=lambda name: name,
                module_available=lambda _: True,
            )
        else:
            whitelist = VerificationWhitelist.discover(
                self.workspace.root,
                python_executable="python" if sandbox.mode != "local" else sys.executable,
            )
        return VerificationWorkflow(
            runner=build_verification_runner(
                sandbox,
                self.workspace.root,
                timeout_seconds=self.config.execution.command_timeout_seconds,
                max_output_chars=self.config.execution.max_output_chars,
            ),
            whitelist=whitelist,
            policy=self.policy,
            store=store,
            workflow=self.workflow,
            max_repair_attempts=self.config.execution.max_repair_attempts,
            max_parallel_verifications=self.config.execution.max_parallel_verifications,
        )

    def repo_map(self) -> RepoMap:
        return RepoMap(
            self.workspace.root,
            self.git,
            max_file_bytes=self.config.workspace.max_file_bytes,
        )

    def verify_task(
        self,
        run_id: str,
        store: PatchRunStore,
        *,
        actions: tuple[str, ...] = (),
        force: bool = False,
    ) -> VerificationOutcome:
        return self.verification_workflow(store).verify(
            run_id, actions=actions, force=force, approved=True
        )

    def repair_task(self, run_id: str, store: PatchRunStore) -> PatchRunRecord:
        return RepairWorkflow(
            model=self.model,
            tools=self.tools,
            tool_context=self.tool_context,
            settings=self.config.workspace,
            patch_engine=UnifiedDiffPatchEngine(self.workspace, self.git),
            policy=self.policy,
            store=store,
            workflow=self.workflow,
            max_attempts=self.config.execution.max_repair_attempts,
        ).repair(run_id, approved=True)

    def review_task(self, run_id: str, store: PatchRunStore) -> ReviewReport:
        return ReviewWorkflow(
            model=self.model,
            store=store,
            workflow=self.workflow,
            repo_map=self.repo_map(),
            max_repair_attempts=self.config.execution.max_repair_attempts,
        ).review(run_id)

    def report_task(self, run_id: str, store: PatchRunStore) -> RunReport:
        return build_report(store.load(run_id))

    def resume_task(
        self, run_id: str, store: PatchRunStore
    ) -> tuple[PatchRunRecord, VerificationOutcome | None]:
        """Continue an interrupted run from its checkpoint; finished actions are not replayed."""
        record = store.load(run_id)
        phase = RunState.from_dict(record.state).phase
        if phase is RunPhase.REVIEW:
            self.review_task(run_id, store)
            return store.load(run_id), None
        if phase not in (RunPhase.VERIFY, RunPhase.REPAIR):
            return record, None
        if phase is RunPhase.REPAIR:
            self.repair_task(run_id, store)
        outcome = self.verify_task(run_id, store)
        if outcome.phase is RunPhase.REVIEW:
            self.review_task(run_id, store)
        return store.load(run_id), outcome


def build_runtime(
    repository: str | Path,
    *,
    config_path: str | Path | None = None,
    model_client: ModelClient | None = None,
    model_provider: str | None = None,
    registry: ProviderRegistry | None = None,
) -> RepoPilotRuntime:
    discovered = RepositoryWorkspace.discover(repository)
    explicit = Path(config_path).resolve() if config_path else None
    config = load_config(discovered.root, explicit)
    if model_provider and model_provider != config.model.provider:
        config = replace(
            config,
            model=replace(config.model, provider=model_provider, name="", base_url=""),
        )
    providers = registry or ProviderRegistry.default()
    config = replace(config, model=providers.normalize(config.model))
    workspace = RepositoryWorkspace(
        discovered.root,
        max_file_bytes=config.workspace.max_file_bytes,
    )
    git = GitClient(workspace.root)
    policy = PermissionPolicy(config.permissions)
    tools = ToolRegistry()
    tools.register(ReadFileTool())
    tools.register(GitStatusTool())
    tools.register(RepositoryInventoryTool())
    tools.register(SearchCodeTool())
    if model_client is None:
        try:
            model_client = providers.build(config.model)
        except ModelNotConfigured as exc:
            model_client = UnconfiguredModelClient(str(exc))
    return RepoPilotRuntime(
        config,
        workspace,
        git,
        policy,
        tools,
        WorkflowEngine(),
        model_client,
        providers,
    )
