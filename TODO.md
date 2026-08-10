# TODO

1. Ingest data from public databases and unify node and edge types
	- Identify and prioritize target public databases (for example: ChEMBL, LINCS, Reactome).
	- Determine general node and edge identities which will integrate chosen DBs and future ones seamlessly 
2. Build graph structure.
	- Implement graph construction pipeline and utility helpers.
	- Run sample builds and inspect graph quality.
3. Build API.
	- Define API endpoints and request/response models for core graph queries.
	- Implement service layer methods that execute graph operations.
	- Add input validation, structured errors, and consistent status codes.
	- Document endpoint usage with examples.
