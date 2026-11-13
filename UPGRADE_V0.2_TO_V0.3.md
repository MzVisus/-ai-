# Upgrade from groupctl v0.2 to v0.3

This is an overlay patch for an existing v0.2 working repository. It preserves `.git/`, the existing `.group/` history, and unrelated local files.

1. Open PowerShell in the existing repository (the folder containing `groupctl.py`).
2. Ensure the v0.2 work is committed and the working tree is clean:

   ```powershell
   git status
   git switch -c feature/groupctl-v0.3-agent-bridge
   ```

3. Extract `groupctl_foundation_v0.3_upgrade_patch.zip` into the repository root and allow the listed files to be replaced. Do not extract the full v0.3 package over your existing repository.
4. Run:

   ```powershell
   python --version
   git --version
   python groupctl.py --version
   python -m py_compile groupctl.py scripts/mock_agent.py tests/test_groupctl.py
   python -m unittest discover -s tests -v
   python groupctl.py validate
   python scripts/mock_agent.py --person-id P17
   ```

5. Inspect `.group/local/mock_agent_submission.json`. To create a local test finding event in your working repository, explicitly run:

   ```powershell
   python scripts/mock_agent.py --person-id P17 --submit
   python groupctl.py validate
   ```

   The event is local only. Review it before committing or pushing.

6. Review the changes and commit them on the upgrade branch:

   ```powershell
   git diff --check
   git status
   git add groupctl.py tests/test_groupctl.py scripts/mock_agent.py AGENT_INSTRUCTIONS.md README.md QUICKSTART_WINDOWS.md CHANGELOG.md FINAL_VALIDATION.md PACKAGE_REVIEW_NOTES.md specifications/08_ai_agent_bridge.md examples/agent_finding_submission.json examples/agent_prompt_template.md UPGRADE_V0.2_TO_V0.3.md VERSION
   git commit -m "feat: add provider-neutral AI agent bridge"
   ```

The archive does not alter your GitHub remote or push anything. Review the branch and then decide whether to push/open a pull request.
