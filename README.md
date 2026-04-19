# Method

Method is a FastAPI-based research-planner web app: you submit a research question plus source files, and the server uses the `research-method-designer` skill (via a headless `claude` CLI subprocess) to return a structured research plan in markdown.

## Quickstart

```bash
make install
make test
make dev
```

Then open http://127.0.0.1:8001/api/health to verify the service is up.

## Documentation

- Design spec: [`docs/superpowers/specs/2026-04-19-method-research-planner-design.md`](docs/superpowers/specs/2026-04-19-method-research-planner-design.md)
- Implementation plan: [`docs/superpowers/plans/2026-04-19-method-implementation-plan.md`](docs/superpowers/plans/2026-04-19-method-implementation-plan.md)
- Project-specific harness rules: [`HARNESS.md`](HARNESS.md)
