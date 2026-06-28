# Security Policy

## Supported Versions

Currently supported versions of Hermes Evolution will receive security updates:

| Version | Supported | Security Updates |
|---------|-----------|------------------|
| 0.1.x    | ✅ Yes    | Until 0.2.0 release |
| < 0.1.0  | ❌ No     | N/A |

## Reporting a Vulnerability

### 🔒 Private Disclosure

If you discover a security vulnerability, **please do NOT create a public issue**.

Instead, please:

1. **Send a private email**: [security contact if applicable]
2. **Create a private security advisory**: Use GitHub's "Report a vulnerability" feature
   - Click "Security" → "Report a vulnerability"
   - Provide detailed description
   - Include steps to reproduce if applicable

### What to Include

When reporting a vulnerability, please include:

- **Description**: What the vulnerability is
- **Impact**: Potential impact on users/systems
- **Reproduction**: Steps to reproduce (if applicable)
- **Environment**: Version, OS, Python version
- **Proof of Concept**: If applicable, a safe demonstration
- **Mitigation**: Suggested mitigation (if known)

### Response Timeline

We aim to respond to security reports within:

- **48 hours**: Initial response acknowledging receipt
- **7 days**: Assessment of severity and proposed timeline
- **30 days**: Patch release (for critical/high severity)

### Severity Levels

- **Critical**: Immediate risk, requires urgent patch
- **High**: Significant risk, expedited patch
- **Medium**: Moderate risk, next release
- **Low**: Minor risk, backlog for consideration

## Evolution-Specific Security

### Mode-Related Security

Hermes Evolution has two modes with different security considerations:

#### PUBLIC Mode (Lower Risk)
- Can read and create issues/PRs
- Cannot modify code directly
- Cannot merge changes
- **Risk**: Abuse of issue creation, spam
- **Mitigation**: Rate limiting, content filtering

#### PRIVATE Mode (Higher Risk)
- Can implement and merge changes
- Can self-update
- Can modify repository
- **Risk**: Unauthorized modifications, malicious updates
- **Mitigation**: 
  - Requires private token
  - Extensive validation
  - Human oversight for critical changes
  - Rollback capabilities

### Self-Update Security

Self-update capabilities introduce specific security concerns:

#### Protections in Place

1. **Token Security**: Private tokens stored securely
2. **Validation**: All changes validated before implementation
3. **Testing**: Tests must pass before merge
4. **Rollback**: Automatic rollback on failure
5. **Human Review**: Critical changes require human review

#### User Responsibilities

- **Secure tokens**: Keep GITHUB_PRIVATE_TOKEN secure
- **Monitor changes**: Review automated changes
- **Report issues**: Report unexpected behavior immediately
- **Backup**: Maintain backups before enabling evolution

### Upstream Sync Security

When syncing with upstream Hermes Agent:

1. **Verify source**: Confirm changes come from official upstream
2. **Review changes**: Manual review of upstream changes
3. **Test thoroughly**: Extensive testing after sync
4. **Document**: Document all upstream changes

## Security Best Practices

### For Users

1. **Token Management**:
   - Use separate tokens for PUBLIC and PRIVATE modes
   - Rotate tokens regularly
   - Use minimal required scopes
   - Never commit tokens to repositories

2. **Evolution Mode**:
   - Only enable PRIVATE mode if you're the repository owner
   - Monitor evolution logs regularly
   - Review automated changes before they're merged
   - Keep backups

3. **Updates**:
   - Review changelogs
   - Test updates in non-production environments first
   - Rollback plan before updating

### For Contributors

1. **Code Security**:
   - Follow secure coding practices
   - Validate all inputs
   - Sanitize data from external sources
   - Use secure dependencies

2. **Evolution Features**:
   - Document security implications
   - Include security considerations in PRs
   - Test for security vulnerabilities
   - Avoid introducing new attack vectors

## Security Features

### Built-In Protections

- **Mode detection**: Prevents unauthorized mode operations
- **Input validation**: All inputs validated
- **Rate limiting**: Prevents abuse of APIs
- **Logging**: Comprehensive logging for security monitoring
- **Rollback**: Automatic rollback on failures

### Monitoring

- **Evolution logs**: Monitor for unusual activity
- **GitHub audit logs**: Review repository access
- **Error tracking**: Monitor for security-related errors

## Dependency Security

### Vulnerability Scanning

We regularly scan dependencies for vulnerabilities:

- **Automated scans**: GitHub Dependabot
- **Manual reviews**: Regular dependency reviews
- **Updates**: Prompt updates for vulnerable dependencies

### Reporting Dependency Issues

If you discover a vulnerability in our dependencies:

1. Check if already reported to the dependency maintainer
2. Report to the dependency maintainer
3. Optionally notify us via private channel

## Security Audits

### Past Audits

[Placeholder for past security audits]

### Requesting an Audit

For security researchers interested in auditing Hermes Evolution:

1. Contact us via private channel
2. Describe scope and timeline
3. Agree on disclosure timeline
4. Coordinate disclosure

## Disclosure Policy

### Coordinated Disclosure

We follow responsible disclosure:

1. **Acknowledge**: Confirm receipt of report
2. **Assess**: Determine severity and impact
3. **Fix**: Develop and test fixes
4. **Release**: Coordinate release with reporter
5. **Credit**: Credit reporter (if desired)

### Public Disclosure

- **Timing**: After fix is available
- **Credits**: At reporter's discretion
- **Details**: Sufficient detail for users to assess risk

## Security Contact

For security-related matters:

- **GitHub Security**: Use "Report a vulnerability"
- **Email**: [security email if applicable]
- **PGP Key**: [PGP key if applicable]

## Security Updates

### Subscribe to Updates

To receive security updates:

- **Watch repository**: Enable "Watch for releases"
- **Security advisories**: Enable notifications
- **Dependabot**: Enable Dependabot alerts

### Update Process

When security updates are released:

1. Review advisory for impact
2. Assess if you're affected
3. Update to latest version
4. Monitor for issues

---

**Security is a community effort. Thank you for helping keep Hermes Evolution safe.** 🔒
