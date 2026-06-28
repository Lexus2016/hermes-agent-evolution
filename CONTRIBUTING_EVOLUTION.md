# Contributing to Hermes Evolution

Thank you for your interest in contributing to Hermes Evolution! This document provides guidelines and instructions for contributing.

## 🎯 Types of Contributions

We welcome contributions in the following areas:

### Evolution Features
- New evolution skills (research, analysis, implementation)
- Upstream sync improvements
- Mode detection enhancements
- Automated testing for evolution

### Core Hermes Agent Features
- Bug fixes
- New tools and skills
- Performance improvements
- Documentation

### Documentation
- Improving existing documentation
- Adding examples and tutorials
- Translation efforts

## 🚀 Getting Started

### 1. Fork and Clone

```bash
# Fork the repository on GitHub
git clone https://github.com/Lexus2016/hermes-agent-evolution.git
cd hermes-agent-evolution
```

### 2. Set Up Development Environment

```bash
# Install dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/
```

### 3. Create a Branch

```bash
git checkout -b feature/your-feature-name
```

## 📝 Development Workflow

### For Bug Fixes and Features

1. **Describe the change** in a new issue (if one doesn't exist)
2. **Create a branch** from `main`
3. **Implement your changes** following code standards
4. **Add tests** for your changes
5. **Update documentation** if needed
6. **Run tests** locally
7. **Submit a pull request**

### For Evolution Features

Evolution features have additional requirements:

1. **Mode consideration**: Specify if your feature works in PUBLIC, PRIVATE, or both modes
2. **Safety checks**: Evolution features must include safety checks and rollback options
3. **Testing**: Evolution features require extensive testing including:
   - Unit tests
   - Integration tests
   - Mode detection tests
4. **Documentation**: Evolution features need detailed documentation including:
   - Purpose and scope
   - Mode requirements
   - Safety considerations
   - Rollback procedures

## 🧪 Testing

### Running Tests

```bash
# Run all tests
pytest tests/

# Run specific test file
pytest tests/test_evolution.py

# Run with coverage
pytest --cov=evolution tests/

# Run specific test
pytest tests/test_evolution.py::test_mode_detection
```

### Writing Tests

- Add tests for new features
- Maintain test coverage above 80%
- Use descriptive test names
- Test both PUBLIC and PRIVATE modes where applicable

Example:

```python
def test_mode_detection_with_private_token():
    """Test that PRIVATE mode is detected when private token is set."""
    os.environ['GITHUB_PRIVATE_TOKEN'] = 'test-token'
    mode = detect_mode()
    assert mode == 'PRIVATE'
```

## 📚 Documentation

### Documentation Standards

- Use clear, concise language
- Provide examples
- Include troubleshooting sections
- Document mode requirements for evolution features
- Keep documentation up-to-date with code changes

### Documentation Locations

- **EVOLUTION_README.md**: Evolution-specific documentation
- **AGENTS.md**: Core Hermes Agent documentation
- **Code comments**: Inline documentation for complex code
- **Docstrings**: Python docstrings for functions and classes

## 🔐 Evolution Mode Considerations

When contributing evolution features:

### PUBLIC Mode Features
- Available to all installations
- Cannot modify code directly
- Can create issues and PRs
- Research and proposal generation

### PRIVATE Mode Features
- Available only to repository owner
- Can implement changes
- Can merge PRs
- Can self-update
- Requires GITHUB_PRIVATE_TOKEN

### Safety Requirements

All evolution features MUST include:

1. **Mode detection**: Check if running in correct mode
2. **Validation**: Validate inputs and state
3. **Rollback**: Provide rollback mechanism
4. **Logging**: Detailed logging for debugging
5. **Testing**: Comprehensive test coverage

## 📋 Pull Request Guidelines

### PR Title Format

Use conventional commit format:

```
feat: add new evolution skill for XYZ
fix: resolve issue with mode detection
docs: update evolution documentation
test: add tests for upstream sync
```

### PR Description

Include in your PR description:

- **Description**: What does this PR do?
- **Type**: Bug fix, feature, docs, etc.
- **Related Issues**: Link to related issues
- **Testing**: How was this tested?
- **Breaking Changes**: Are there breaking changes?
- **Mode**: PUBLIC, PRIVATE, or both

### Review Process

1. **Automated checks**: CI must pass
2. **Code review**: Maintainer review required
3. **Evolution features**: Additional safety review required
4. **Merge**: Squash and merge preferred

## 🎨 Code Style

Follow these guidelines:

- **Python**: PEP 8 compliant
- **Docstrings**: Google style docstrings
- **Naming**: descriptive, lowercase_with_underscore
- **Imports**: Group imports (stdlib, third-party, local)
- **Line length**: Max 100 characters (soft limit 120)

### Formatters

```bash
# Format code
black .

# Lint code
ruff check .

# Type checking
mypy evolution/
```

## 🔄 Sync with Upstream

Hermes Evolution is based on [Hermes Agent](https://github.com/nousresearch/hermes-agent). Periodically, we sync with upstream:

```bash
git fetch upstream
git rebase upstream/main
```

When syncing:
- Resolve conflicts carefully
- Preserve evolution features
- Test thoroughly after sync
- Update documentation if needed

## 🐛 Bug Reports

When reporting bugs:

1. **Search existing issues** first
2. **Use bug report template**
3. **Provide environment details**
4. **Include logs and error messages**
5. **Add reproduction steps**

## 💡 Feature Requests

When proposing features:

1. **Check if already proposed**
2. **Use feature request template**
3. **Describe the problem** you're solving
4. **Propose a solution**
5. **Consider impact** on evolution modes

## 🤝 Code of Conduct

Be respectful, constructive, and collaborative. We're all here to build something amazing.

## 📧 Getting Help

- **GitHub Issues**: Bug reports and feature requests
- **GitHub Discussions**: Questions and ideas
- **Documentation**: Check EVOLUTION_README.md and AGENTS.md

## 🙏 Recognition

Contributors will be recognized in:
- CONTRIBUTORS.md file
- Release notes
- Project documentation

---

**Happy contributing! Let's build the future of self-improving AI together.** 🚀
