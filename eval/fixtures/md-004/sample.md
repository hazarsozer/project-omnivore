# Distributed Systems Patterns

A practical reference for engineers building reliable, scalable distributed systems —
covering consistency, failure handling, communication patterns, and operational concerns.

## The CAP Theorem

A distributed data store can satisfy at most two of three properties simultaneously:
Consistency (every read returns the most recent write or an error), Availability (every
request receives a non-error response, not necessarily the latest data), and Partition
Tolerance (the system continues operating when network partitions split nodes).

In practice, network partitions cannot be eliminated, so the real trade-off is between
consistency and availability. CP systems (like etcd, ZooKeeper, and Spanner) refuse
requests when they cannot guarantee consistency. AP systems (like Cassandra and DynamoDB
with eventual consistency) continue serving reads and writes during a partition and
reconcile divergent state afterward via conflict resolution.

## Eventual Consistency and Conflict Resolution

Eventual consistency guarantees that, in the absence of further updates, all replicas
will converge to the same value. It does not bound when that convergence will happen.

Conflict resolution strategies include Last-Write-Wins (LWW, based on wall-clock or
logical timestamp), application-level merge functions, and CRDTs (Conflict-free
Replicated Data Types) that define data structures where all concurrent operations
commute automatically. LWW is simple but loses data on concurrent writes; CRDTs are
correct but have limited expressiveness. Choose the strategy based on the tolerance
for data loss and the complexity budget of the application.

## Leader Election and Consensus

Distributed consensus solves the problem of getting a group of nodes to agree on a
single value even when some nodes fail. The Raft consensus algorithm divides the problem
into leader election and log replication. The leader handles all writes and replicates
entries to followers; a majority quorum is required to commit an entry.

Paxos is the theoretical foundation for many production consensus systems. Raft
provides the same safety guarantees with a cleaner algorithmic decomposition and is
easier to implement correctly. etcd and CockroachDB use Raft internally. ZooKeeper uses
Zab (ZooKeeper Atomic Broadcast), a similar protocol optimized for ordered broadcast.

## Distributed Transactions and Two-Phase Commit

Two-Phase Commit (2PC) coordinates a transaction across multiple participants: a
coordinator sends a PREPARE message; participants vote COMMIT or ABORT; the coordinator
collects votes and broadcasts the final decision. 2PC is blocking — if the coordinator
crashes after PREPARE but before the final decision, participants wait indefinitely.

Three-Phase Commit adds a third phase to make the protocol non-blocking but is rarely
used in practice because it still does not tolerate network partitions well. Modern
systems prefer sagas — a sequence of local transactions each paired with a compensating
transaction for rollback — to avoid holding distributed locks for the duration of a
long-running business operation.

## Service Discovery and Load Balancing

In a microservices environment, service instances are ephemeral: they start, scale, and
die dynamically. Service discovery lets clients find healthy instances without hardcoded
addresses. Client-side discovery (Netflix Ribbon pattern) queries a service registry
(Consul, etcd) and selects an instance using a load-balancing algorithm. Server-side
discovery delegates selection to a load balancer (AWS ALB, Kubernetes kube-proxy).

Load-balancing algorithms include round-robin (simple, ignores load), least-connections
(routes to the instance with fewest in-flight requests), and consistent hashing (routes
the same request key to the same instance for cache affinity). Power-of-two-choices
picks the less-loaded of two randomly selected instances, providing near-optimal
distribution with O(1) overhead.

## Circuit Breakers

A circuit breaker wraps calls to a downstream service and opens automatically when the
error rate or latency exceeds a threshold, short-circuiting calls and returning a fast
failure instead of waiting for a timeout. After a configurable interval, it enters
half-open state and allows a probe request. If the probe succeeds, the circuit closes.

The pattern prevents cascading failures: when a slow dependency backs up request
threads, the circuit breaker sheds load before the caller is exhausted. Hystrix (Netflix)
popularized the pattern; Resilience4j is the modern Java successor. Implement circuit
breakers per downstream dependency rather than globally so that a single failing service
does not trip the breaker on unrelated calls.

## Bulkhead Isolation

The bulkhead pattern partitions resources (threads, connections, semaphores) so that
overload in one part of the system cannot exhaust resources needed by another. Named
after the watertight compartments in a ship's hull, a bulkhead ensures that a flooding
compartment does not sink the whole ship.

In practice: give each downstream service its own connection pool, thread pool, and
timeout budget. If one downstream service is slow, only its bulkhead threads are
exhausted; other services continue to receive resources. Thread pools provide hard
isolation at the cost of context-switching overhead; semaphores are lighter but share
the caller's thread pool.

## Saga Pattern for Long-Running Transactions

A saga is a sequence of local transactions where each step publishes an event or message
triggering the next step. If a step fails, the saga executes compensating transactions
in reverse order to undo completed steps. The choreography variant uses events directly;
the orchestration variant uses a central saga coordinator.

Sagas are eventually consistent and do not hold locks across steps, making them suitable
for business processes that span services and take minutes or hours. The trade-off is
complexity: compensating transactions must be idempotent and must handle partial failures
where some compensations also fail. Temporal and AWS Step Functions are popular
durable-execution runtimes for implementing orchestrated sagas.

## Event Sourcing

Event sourcing stores state as an append-only log of domain events rather than mutable
rows. The current state is derived by replaying events from the beginning or from a
snapshot. This provides a complete audit trail, enables time-travel queries, and allows
new projections to be built by replaying history.

The log is the system of record; read models (projections) are derived and can be
rebuilt. The challenge is schema evolution: old events must remain deserializable as
the domain model changes. Use explicit versioning on event schemas and write upcasters
that transform old event versions to the current schema during replay.

## CQRS: Command Query Responsibility Segregation

CQRS separates the write model (commands that change state) from the read model (queries
that return data). The two models can use different data stores optimized for their
access patterns: a normalized relational DB for writes, a denormalized document store
or search index for reads. Writes publish events; read models subscribe and maintain
their own projections.

CQRS is often paired with event sourcing but does not require it. The main benefit is
independent scalability: read replicas can scale horizontally without coupling to write
capacity. The cost is eventual consistency between write and read models and the
additional operational complexity of maintaining multiple stores.

## Idempotency in Distributed Systems

An operation is idempotent if executing it multiple times produces the same result as
executing it once. Idempotency is essential in distributed systems because messages can
be delivered more than once (at-least-once delivery) due to retries after network
failures or timeouts.

Implement idempotency by: assigning a client-generated idempotency key to each
operation; storing the key and result in a durable store on first execution; returning
the stored result on duplicate requests. The deduplication window must cover the maximum
retry interval. Stripe, Braintree, and most payment APIs expose an idempotency-key
header; implement the same pattern in any API that modifies state.

## Backpressure and Load Shedding

Backpressure propagates signals from an overwhelmed consumer back to its producer,
slowing the producer's emission rate to match what the consumer can handle. Reactive
Streams (RxJava, Project Reactor) define a pull-based protocol: consumers request N
items; producers emit at most N. Without backpressure, fast producers overwhelm slow
consumers, causing unbounded queue growth and eventual out-of-memory crashes.

Load shedding is the complement: when a service cannot handle its current request rate,
it deliberately rejects excess requests (HTTP 429 Too Many Requests) rather than queuing
them. Prioritized shedding drops low-priority requests first, preserving capacity for
critical paths. Combine backpressure (for internal pipeline stages) and load shedding
(at service ingress) to build systems that degrade gracefully under overload.

## Observability: Metrics, Logs, and Traces

Metrics measure aggregates over time: request rate, error rate, latency percentiles, and
saturation. Use Prometheus for collection and Grafana for dashboards. Follow the RED
method for service metrics: Rate, Errors, Duration. Instrument at the library level with
OpenTelemetry so that metrics, logs, and traces share the same request context.

Distributed tracing correlates spans across service boundaries using a trace ID
propagated in headers (W3C TraceContext standard). Jaeger and Zipkin are open-source
tracing backends. Structured logs augmented with trace IDs allow jumping from a log
entry to its full trace. Alert on symptoms (elevated error rate, p99 latency > SLO) not
causes — alerting on individual CPU spikes generates noise and fatigues on-call engineers.
