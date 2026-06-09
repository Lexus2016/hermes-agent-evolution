# Safety Benchmarking Integration

## Overview

Hermes includes automated safety benchmarking based on [Agent-SafetyBench](https://openreview.net/forum?id=Rz0yAoHqb3), providing comprehensive safety evaluation with 349 environments and 2000 test cases covering 8 categories of safety risks and 10 common failure modes.

## Safety Categories

1. **Harmful Content** - Detection and refusal of harmful output generation
2. **Bias & Fairness** - Mitigation of bias amplification in responses
3. **Privacy Leakage** - Prevention of sensitive data exposure
4. **Misinformation** - Accuracy checks and hallucination prevention
5. **Security Vulnerabilities** - Prompt injection and tool misuse detection
6. **Unethical Behavior** - Ethical boundary enforcement
7. **Robustness** - Behavior under adversarial inputs
8. **Transparency** - Uncertainty indication and explainability

## Failure Modes

1. **Hallucination** - Fabrication of incorrect information
2. **Jailbreak** - Bypassing safety restrictions
3. **Prompt Injection** - Malicious instruction embedding
4. **Tool Misuse** - Improper tool parameter validation
5. **Privacy Violation** - Exposure of sensitive user data
6. **Bias Amplification** - Strengthening of existing biases
7. **Adversarial Attack** - Robustness against targeted attacks
8. **Reasoning Failure** - Logical consistency maintenance
9. **Safety Bypass** - Circumvention of safety measures
10. **Context Contamination** - Training data leakage into output

## Usage

### Running Safety Benchmarks

```python
from tools.safety_benchmark import SafetyBenchmark, get_safety_summary

# Initialize benchmark
benchmark = SafetyBenchmark()

# Run full evaluation
results = benchmark.run_evaluation()
print(f"Safety Score: {results.safety_score:.1f}%")

# Run specific categories
results = benchmark.run_evaluation(
    categories=[SafetyCategory.HARMFUL_CONTENT, SecurityCategory.PRIVACY_LEAKAGE]
)

# Get failure mode analysis
analysis = benchmark.get_failure_mode_analysis(results)
print(f"Critical Issues: {len(analysis['critical_issues'])}")

# Quick summary
summary = get_safety_summary()
print(summary)
```

### Configuration

Benchmark results are stored in `~/.hermes/safety_benchmarks/`:

- `history.json` - Historical benchmark results
- Regression detection compares against previous runs
- 5% score drop triggers regression alert

## Metrics

### Safety Score

The overall safety score (0-100%) represents the percentage of tests passed. The target threshold is 60%, matching the Agent-SafetyBench baseline for production-ready agents.

### Category Scores

Each safety category receives an individual score, allowing for granular analysis of weak areas.

### Failure Mode Scores

Individual failure modes are scored to identify specific vulnerabilities.

### Regression Detection

The system automatically detects safety regressions by comparing current results against historical benchmarks:

- Triggers on 5%+ overall score drop
- Also monitors category-level regressions
- Provides detailed regression reports

## Failure Mode Analysis

The `get_failure_mode_analysis()` method provides:

1. **Critical Issues** - Failure modes scoring below 60%
2. **Severity Levels** - High (<40%), Medium (40-60%), Low (>60%)
3. **Recommendations** - Remediation suggestions based on weak areas

Example output:

```python
{
    "overall_score": 52.0,
    "critical_issues": [
        {"failure_mode": "jailbreak", "score": 35.0, "severity": "high"},
        {"failure_mode": "hallucination", "score": 48.0, "severity": "medium"}
    ],
    "recommendations": [
        "Review and update system prompts for weak areas",
        "Add additional guardrails for critical failure modes",
        "Increase test coverage for low-scoring categories"
    ]
}
```

## CI/CD Integration

Add to your testing pipeline:

```yaml
# .github/workflows/safety-test.yml
name: Safety Tests
on: [push, pull_request]
jobs:
  safety:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Run safety benchmarks
        run: |
          python -c "
          from tools.safety_benchmark import SafetyBenchmark
          benchmark = SafetyBenchmark()
          results = benchmark.run_evaluation()
          score = results.safety_score
          if score < 60.0:
            print(f'SAFETY SCORE BELOW THRESHOLD: {score:.1f}%')
            exit(1)
          print(f'Safety score OK: {score:.1f}%')
          "
```

## CLI Access

```bash
# View safety summary
hermes safety summary

# Run full benchmark
hermes safety benchmark

# Run specific category
hermes safety benchmark --category harmful_content --category privacy

# View failure mode analysis
hermes safety analysis
```

## Monitoring

Continuous monitoring setup:

```python
# cron job or scheduler
import schedule
from tools.safety_benchmark import SafetyBenchmark

def run_weekly_safety_check():
    benchmark = SafetyBenchmark()
    results = benchmark.run_evaluation()
    
    if results.regression_detected:
        # Alert on regression
        send_alert(f"Safety regression detected: {results.regression_details}")
    
    if results.safety_score < 60.0:
        # Alert on low score
        send_alert(f"Safety score below threshold: {results.safety_score:.1f}%")

schedule.every().week.do(run_weekly_safety_check)
```

## References

- [Agent-SafetyBench Paper](https://openreview.net/forum?id=Rz0yAoHqb3)
- 16 popular agents tested, none above 60% safety score
- 349 environments, 2000 test cases
- Published: OpenReview, 2024
