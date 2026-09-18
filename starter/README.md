# Technical Reflection & Architecture Report

## Customer Support AI Agent: Technical Reflection Report

### Architectural Design & System Decisions

I built this customer support assistant on the Strands agentic framework, running Amazon Nova Lite (`global.amazon.nova-2-lite-v1:0`) inside containerized microservices managed by Amazon Bedrock AgentCore. Rather than bundling all capabilities into a heavy monolithic script, I split the tools across dedicated interfaces depending on latency and safety requirements:

* **Transactional Tools via MCP Gateway:** Order tracking and refund workflows connect to AWS Lambda functions through a Model Context Protocol (MCP) gateway over HTTP using `streamable_http_client`. This separates the reasoning engine from direct database mutations.

* **Knowledge Base Retrieval (RAG):** Product specs and return policies are queried on demand via `bedrock-agent-runtime.retrieve()` against an Amazon Bedrock Knowledge Base. This avoids wasting prompt context window tokens on static reference data.

* **Ephemeral Sandboxes:** Arithmetic discount logic is offloaded to the Bedrock AgentCore Code Interpreter (`code_session`) so the LLM does not perform mental math on currency totals. Web navigation runs inside an isolated `AgentCoreBrowser` session communicating through the Chrome DevTools Protocol (CDP).

---

### Memory Management & Multi-Session Persistence

To prevent prompt bloat while preserving user preferences across separate visits, I avoided re-injecting raw chat transcripts into every prompt. Instead, I tied Bedrock AgentCore's `MemoryClient` into the Strands event lifecycle:

* **Targeted Context Injection (`MessageAddedEvent`):** When a user sends a message, `MemoryHook` queries the customer's namespace (`facts` and `preferences`) and prepends only relevant memories into the prompt context before inference.


* **Asynchronous Indexing (`AfterInvocationEvent`):** After each turn completes, dialogue pairs are sent to `create_event`. AgentCore indexes semantic facts in the background, keeping request response times low.


* **Cross-Session Verification:** In testing, I initialized session `s-A` with customer Jane's preference for brevity. In a completely clean session (`s-B`), the agent correctly addressed the customer by name and maintained the requested concise tone without any prompt re-priming.

---

### Debugging & Engineering Challenges

* **ARM64 Build Constraints:** Bedrock AgentCore strictly mandates `linux/arm64` container images. CodeBuild initially failed during provisioning because the builder compute defaulted to incompatible instances. I configured the build project to use `ARM_CONTAINER` and attached `AmazonEC2ContainerRegistryPowerUser` and `AmazonS3ReadOnlyAccess` so the cloud builder could assemble and push the image.

* **Python 3.14 Async Teardown Conflict:** During browser testing, the invocation crashed with `RuntimeError: Timeout should be used inside a task`. CloudWatch traces showed that while the browser ran successfully, `strands._async` ran `asyncio.run()`, which triggered `loop.shutdown_default_executor()` during cleanup. In Python 3.14 alongside `nest_asyncio`, that method executes `async with timeouts.timeout()`, failing when no task is active on the loop. I resolved this by monkeypatching `shutdown_default_executor` to a safe no-op on the loop.

* **Docker Packaging Collision:** The container build broke during `uv pip install .` with a setuptools `PackageDiscoveryError`. Temporary diagnostic scripts in the root directory tripped setuptools auto-discovery. I cleaned the root workspace and added `py-modules = ["main"]` under `[tool.setuptools]` in `pyproject.toml` to enforce explicit file packaging.

* **IAM Least Privilege:** Attaching explicit inline policies for `bedrock:Retrieve` and `bedrock-agentcore:*` resolved authorization dropbacks when querying vector stores and initializing remote browser workers.

---

### Production Scalability

The agent container instances remain completely stateless; user identity and long-term memory are handled externally by AgentCore's managed storage layer. High-overhead tasks—specifically Chromium browser sessions and Python code sandboxing—run outside the primary ASGI container, preventing memory spikes from impacting agent availability. Operational health is monitored via CloudWatch Logs and AWS X-Ray tracing to track end-to-end tool execution latency and API Gateway throughput.
