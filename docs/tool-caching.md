# Tool Call Caching Layer

## Overview

The Tool Call Caching Layer is an adaptive caching system for tool call results, inspired by [ToolCacheAgent research](https://openreview.net/forum?id=tX3YcbNa5w). It automatically caches tool call results to reduce latency and resource consumption for repeated operations.

## Features

- **Automatic caching**: Read-style tools are automatically cached based on their arguments
- **TTL-based expiration**: Each tool type has a configurable time-to-live (TTL)
- **Intelligent invalidation**: Stateful operations (write, patch) automatically invalidate cached reads
- **LRU eviction**: Least-recently-used entries are evicted when cache is full
- **Memory bounds**: Configurable memory limit prevents excessive RAM usage
- **Thread-safe**: Concurrent access is properly synchronized
- **Metrics**: Built-in hit rate and performance tracking

## Configuration

### Default TTL Values

```python
DEFAULT_TTL_CONFIG = {
    "read_file": 300,          # 5 minutes
    "search_files": 180,       # 3 minutes
    "web_search": 600,         # 10 minutes
    "web_extract": 1800,       # 30 minutes
    "session_search": 600,     # 10 minutes
    # Stateful tools (not cached by default)
    "terminal": 0,
    "write_file": 0,
    "patch": 0,
    "memory": 0,
    "todo": 0,
}
```

### Custom Configuration

You can customize cache behavior:

```python
from tools.tool_cache import ToolCache

cache = ToolCache(
    max_size=2000,              # Maximum number of entries
    default_ttl=600,            # Default TTL in seconds
    ttl_config={                # Per-tool overrides
        "read_file": 900,       # 15 minutes
        "web_search": 1800,     # 30 minutes
    },
    max_memory_mb=200,          # 200 MB memory limit
)
```

## Usage

### Basic Usage

The cache is automatically integrated into tool execution in `agent/agent_runtime_helpers.py`. No code changes are needed for basic usage.

### Programmatic Usage

```python
from tools.tool_cache import cached_tool_call

def execute_tool():
    # Expensive operation
    return "tool result"

result, cache_hit = cached_tool_call(
    tool_name="read_file",
    tool_args={"path": "/tmp/file.txt"},
    executor=execute_tool,
    effective_task_id="task-123"
)
```

### Cache Statistics

Get cache performance metrics:

```python
from tools.tool_cache import get_cache_report

report = get_cache_report()
print(report)
```

Output:
```
=== Tool Cache Report ===
Entries: 145/1000
Memory: 2.3/100.0 MB
Hit rate: 67.5%
Hits: 312, Misses: 150
Evictions: 5, Invalidations: 23

Top tools by hit count:
  read_file: 245
  search_files: 42
  web_search: 25
```

## Invalidation Rules

The cache automatically invalidates stale data based on tool relationships:

| Source Tool | Invalidates | Reason |
|------------|-------------|--------|
| `write_file` | `read_file` | File content changed |
| `patch` | `read_file` | File modified |
| `terminal` | `read_file` | File system state changed |
| `memory` | `session_search` | Session data modified |

## Performance

Based on ToolCacheAgent research:
- **Latency speed-up**: Up to 1.69x faster for repetitive workflows
- **Cache hit rate**: 50%+ for typical development workflows
- **Memory overhead**: Configurable, typically <100MB

## Testing

Run the test suite:

```bash
python -m pytest tests/tool_cache/test_tool_cache.py -v
```

## Implementation Details

### Cache Key Generation

Cache keys are generated from:
1. Tool name
2. Normalized arguments (sorted keys, JSON-serialized)
3. Optional task ID for isolation

Example:
```python
key = cache.generate_cache_key("read_file", {"path": "/tmp/test"})
# Returns: "a1b2c3d4..." (32-char hash)
```

### LRU Eviction

When the cache is full:
1. Least-recently-used entries are evicted first
2. Memory bounds are enforced
3. Eviction statistics are tracked

### Thread Safety

- All cache operations are protected by `threading.RLock`
- Safe for concurrent tool execution
- No race conditions on cache updates

## Troubleshooting

### Cache Not Working

1. Check if the tool is cacheable:
   ```python
   cache.is_cacheable("tool_name", tool_args)
   ```

2. Verify TTL configuration:
   ```python
   ttl = cache.get_ttl_for_tool("tool_name")
   ```

3. Check cache statistics:
   ```python
   stats = cache.get_stats()
   print(f"Hit rate: {stats['hit_rate']:.1%}")
   ```

### Stale Data

If you're seeing stale cached data:
1. Verify invalidation rules are registered
2. Check if TTL is too long
3. Consider manual cache clearing for critical operations

## Future Enhancements

Potential improvements:
1. ML-based cache plan generation (from ToolCacheAgent paper)
2. Adaptive TTL based on access patterns
3. Predictive pre-caching for likely operations
4. Distributed caching for multi-agent scenarios

## References

- [ToolCacheAgent Paper](https://openreview.net/forum?id=tX3YcbNa5w)
- Implementation: `tools/tool_cache.py`
- Integration: `agent/agent_runtime_helpers.py`
- Tests: `tests/tool_cache/test_tool_cache.py`
