Review the following Pull Request for the LarkScout project.

Steps:
1. Fetch PR details: `gh pr view $ARGUMENTS --json title,body,files,additions,deletions`
2. Fetch the diff: `gh pr diff $ARGUMENTS`
3. Review the changes against these criteria:
   - **Correctness**: Does the code do what the PR description says?
   - **API contracts**: Do changes align with SKILL files in `skills/`?
   - **Code quality**: Type hints, docstrings, error handling, no hardcoded values
   - **Tests**: Are there tests for new/changed behavior?
   - **Security**: No secrets, no unsafe eval, no unvalidated user input
   - **Performance**: No obvious N+1, no blocking calls in async context
4. Present your review as:
   - ✅ Approve — if all checks pass
   - ⚠️ Request changes — list each issue with file:line and suggested fix
5. Wait for my decision:
   - If I say approve → `gh pr review $ARGUMENTS --approve -b "<summary>"` then `gh pr merge $ARGUMENTS --squash --auto`
   - If I say reject → `gh pr review $ARGUMENTS --request-changes -b "<summary>"`
   - If I want to discuss → just continue the conversation

PR number: $ARGUMENTS
