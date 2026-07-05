# Java Runtime dogfood logging

Java Runtime writes structured lifecycle events to Hermes' normal log files.
The default `INFO` level is intended for day-to-day dogfood:

```bash
hermes logs -f
```

Useful event names include:

- `java_runtime.action.start` / `java_runtime.action.finish`: every tool action,
  result, duration, and returned object counts.
- `java_runtime.process.*`: JVM spawn, readiness, exit, timeout, and shutdown.
- `java_runtime.jdwp.connect.*`: debugger connection and negotiated ID sizes.
- `java_runtime.breakpoint.*`: breakpoint creation, wait, timeout, and hit.
- `java_runtime.suspension.*`: suspension invalidation and resume.
- `java_runtime.threads.observed`, `java_runtime.stack.observed`, and
  `java_runtime.variables.observed`: observation counts and completeness.

Warnings and failures can be watched separately:

```bash
hermes logs errors -f
```

For a protocol-level investigation, temporarily set the following in
`~/.hermes/config.yaml`, restart Hermes, and follow the log:

```yaml
logging:
  level: DEBUG
```

```bash
hermes logs -f --level DEBUG
```

`DEBUG` records JDWP command IDs, command sets, error codes, byte counts, and
latency. It does not record protocol payloads. Runtime logs also omit variable
values, application arguments, JVM argument values, and captured console text.
The Java application's own console output remains in the `log_file` returned by
the `run` action and is available through `java_runtime(action="logs")`.
