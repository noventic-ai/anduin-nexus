# TODO

1. Build and test adapters for individual public databases.
	- Identify and prioritize target public databases (for example: ChEMBL, LINCS, Reactome).
	- Define a common adapter interface for query input, response parsing, and error handling.
	- Implement one adapter at a time with minimal, reproducible config examples.
	- Add adapter-level tests for successful queries, empty results, and failure cases.
	- Validate outputs are normalized into a consistent internal schema.
2. Build graph structure.
	- Define node and edge types required by the API use cases.
	- Map normalized adapter outputs into graph entities and relationships.
	- Implement graph construction pipeline and utility helpers.
	- Add integrity checks (duplicate nodes, orphan edges, missing required fields).
	- Run sample builds from real adapter outputs and inspect graph quality.
3. Build API.
	- Define API endpoints and request/response models for core graph queries.
	- Implement service layer methods that execute graph operations.
	- Add input validation, structured errors, and consistent status codes.
	- Add configuration support for runtime options (data sources, limits, logging).
	- Document endpoint usage with examples.
4. Test API with Python queries.
	- Create a Python test script or notebook that exercises each endpoint.
	- Cover happy-path, edge-case, and invalid-input scenarios.
	- Verify response shape, latency expectations, and error messages.
	- Add regression checks for previously fixed bugs.
	- Save representative test queries and expected outputs in assets or tests.
5. Launch server.
	- Finalize runtime configuration (host, port, env vars, logging level).
	- Start the server in a reproducible way (script or documented command).
	- Run smoke tests against live endpoints after startup.
	- Monitor logs for adapter/API exceptions and fix launch blockers.
	- Document startup, shutdown, and basic troubleshooting steps.
