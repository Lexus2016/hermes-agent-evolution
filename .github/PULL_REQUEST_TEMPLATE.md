## Pull Request Template

<!-- 
Thank you for contributing to Hermes Evolution! 
Please fill out this template to help reviewers understand your changes.
-->

## 📝 Description
<!-- Describe your changes in detail -->

## 🎯 Type of Change
<!-- Mark the relevant option with an "x" -->

- [ ] 🐛 Bug fix (non-breaking change which fixes an issue)
- [ ] ✨ New feature (non-breaking change which adds functionality)
- [ ] 🔒 Security fix (undisclosed vulnerabilities in [SECURITY.md](https://github.com/Lexus2016/hermes-agent-evolution/blob/main/SECURITY.md) §3.1 scope go to [Security Advisories](https://github.com/Lexus2016/hermes-agent-evolution/security/advisories/new) first, not a public PR)
- [ ] 💥 Breaking change (fix or feature that would cause existing functionality to change)
- [ ] 📚 Documentation update
- [ ] 🎨 Code style update (formatting, local variables)
- [ ] ♻️ Code refactoring (no functional changes)
- [ ] 🧪 Test additions or updates
- [ ] 🎯 New skill (bundled or hub)
- [ ] 🔄 Evolution improvement (automated or manual)

## 🔗 Related Issues/PRs
<!-- Link to related issues or pull requests -->

Fixes #
Related to #

## 🧪 Testing
<!-- Steps to verify this change works. For bugs: reproduction steps + proof that the fix works. -->

1. 
2. 
3. 

### Test Environment
- **Python Version**:
- **OS**:
- **Hermes Evolution Version**:

### Test Results
```
paste test output here
```

## 📊 Impact Analysis
<!-- Describe the impact of your changes -->

- **User Impact**: <!-- How will this affect users? -->
- **Performance Impact**: <!-- Does this change performance? -->
- **Breaking Changes**: <!-- Are there breaking changes? -->
- **Migration Required**: <!-- Do users need to migrate? -->

## ✅ Checklist

<!-- Complete these before requesting review. -->

### Code

- [ ] I've read the [Contributing Guide](https://github.com/Lexus2016/hermes-agent-evolution/blob/main/CONTRIBUTING.md)
- [ ] My commit messages follow [Conventional Commits](https://www.conventionalcommits.org/) (`fix(scope):`, `feat(scope):`, etc.)
- [ ] I searched for [existing PRs](https://github.com/Lexus2016/hermes-agent-evolution/pulls) to make sure this isn't a duplicate
- [ ] My PR contains **only** changes related to this fix/feature (no unrelated commits)
- [ ] I've run `pytest tests/ -q` and all tests pass
- [ ] `python scripts/check` passes (the blocking lint and code-health checks CI runs)
- [ ] I've added tests for my changes (required for bug fixes, strongly encouraged for features)
- [ ] I've tested on my platform: <!-- e.g. Ubuntu 24.04, macOS 15.2, Windows 11 -->

### Documentation & Housekeeping

- [ ] I've updated relevant documentation (README, `docs/`, docstrings) — or N/A
- [ ] I've updated `cli-config.yaml.example` if I added/changed config keys — or N/A
- [ ] I've updated `CONTRIBUTING.md` or `AGENTS.md` if I changed architecture or workflows — or N/A
- [ ] I've considered cross-platform impact (Windows, macOS) per the [compatibility guide](https://github.com/Lexus2016/hermes-agent-evolution/blob/main/CONTRIBUTING.md#cross-platform-compatibility) — or N/A
- [ ] I've updated tool descriptions/schemas if I changed tool behavior — or N/A

## For New Skills

<!-- Only fill this out if you're adding a skill. Delete this section otherwise. -->

- [ ] This skill is **broadly useful** to most users (if bundled) — see [Contributing Guide](https://github.com/Lexus2016/hermes-agent-evolution/blob/main/CONTRIBUTING.md#should-the-skill-be-bundled)
- [ ] SKILL.md follows the [standard format](https://github.com/Lexus2016/hermes-agent-evolution/blob/main/CONTRIBUTING.md#skillmd-format) (frontmatter, trigger conditions, steps, pitfalls)
- [ ] No external dependencies that aren't already available (prefer stdlib, curl, existing Hermes tools)
- [ ] I've tested the skill end-to-end: `hermes --toolsets skills -q "Use the X skill to do Y"`

## 📚 Additional Notes
<!-- Any additional information for reviewers -->

<!-- 
For evolution-related changes:
- Is this from automated evolution research?
- Does this require evolution mode?
- Should this trigger self-update?
-->
