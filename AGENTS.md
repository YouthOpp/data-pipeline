# YouthOpps repository agent instructions

## Mandatory AI workspace gate

**This is a mandatory requirement for every AI agent and AI-assisted developer, regardless of provider or tool. Direct development in a standalone project checkout is prohibited.** Participation is open to all agents and developers only under these rules.

1. Before making development changes, obtain [YouthOpps/ai-workspace](https://github.com/YouthOpps/ai-workspace) and initialize its registered repositories:

   ```sh
   git clone https://github.com/YouthOpps/ai-workspace.git
   cd ai-workspace
   git submodule update --init --recursive
   ```

   An existing ai-workspace checkout may be reused after checking its instructions and submodule configuration.
2. Read the workspace [AGENTS.md](https://github.com/YouthOpps/ai-workspace/blob/main/AGENTS.md), [workflow](https://github.com/YouthOpps/ai-workspace/blob/main/docs/WORKFLOW.md), and applicable [skills](https://github.com/YouthOpps/ai-workspace/tree/main/skills). Follow them throughout implementation, validation and review; reading this redirect alone is insufficient.
3. Develop only inside the relevant registered submodule of ai-workspace. If started in a standalone project clone, stop before editing and move to the workspace submodule. Do not bypass this gate by copying skills into a standalone checkout. Workspace rules and skills themselves are maintained in ai-workspace.
4. For an assigned project task, preserve existing changes, fetch and verify only the target repository's current `origin/main`, and align its local checkout by creating the issue branch from that revision. Alignment alone requires no commit or push; leave unrelated modules untouched. Submit actual implementation and independent subagent validation as an issue-linked PR to that repository. Never develop directly on `main`, push directly to `main`, merge your own PR, or commit/push workspace gitlink changes as part of project development. Workspace maintenance may update skills, repository instructions, published rule copies and deliberate module revisions through maintenance PRs under the workspace workflow. An independently confirmed unsolvable attempt must be documented on the issue, which stays open; any PR stays unmerged.
5. `data-source` remains publication-only, including its nested checkout under website. Agents must not develop there, create development branches, push commits, or open PRs there. Implement data corrections in data-pipeline and use authorized publication automation. Exceptional manual administrator recovery remains separate.

If the workspace or required skills are unavailable, stop development and report the blocker. Tool access, repository write access, urgency, or missing local instructions do not waive this gate.
