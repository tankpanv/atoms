# Default new-project plan

For a new project without an explicit stack or sequencing requirement, the harness now uses a lightweight demo-first order:

1. Define the smallest interface contract: request/response shapes, errors, loading/empty states, and the explicit simulated-provider boundary.
2. Build the complete first-screen and core interaction preview against that contract. A local adapter may provide visible demo data while external credentials are unavailable.
3. Connect the real FastAPI/PostgreSQL backend, persistence and external providers behind the same contract.
4. Run the real build, service startup and affected core workflow checks.

This does not remove backend work. It prevents database or provider setup from blocking the first usable preview. The backend remains required when the product needs persistence, accounts, cross-device data, server computation, secrets, or external services. Simulated results are labelled and never count as real provider acceptance.
