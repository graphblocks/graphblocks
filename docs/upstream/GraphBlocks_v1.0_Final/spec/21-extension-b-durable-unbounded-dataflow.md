# Extension B. Durable Unbounded Dataflow

## B.1 위치

대부분의 문서 ingestion은 bounded job과 checkpoint만으로 충분하다. Kafka topic, CDC, continuous sync, unbounded window가 필요한 경우에만 `graphblocks-durable` extension을 사용한다.

## B.2 패키지

```text
graphblocks-durable
graphblocks-kafka
graphblocks-nats
graphblocks-sqs
graphblocks-pubsub
graphblocks-etcd, future
```

## B.3 Source contract

```rust
#[async_trait]
pub trait DurableSource: Send + Sync {
    async fn poll(&self, cursor: Option<SourceCursor>, demand: usize) -> Result<SourceBatch>;
    async fn commit(&self, cursor: SourceCursor) -> Result<()>;
    async fn pause(&self) -> Result<()>;
    async fn resume(&self) -> Result<()>;
}
```

## B.4 Delivery guarantee

```text
best_effort
at_most_once
at_least_once
```

GraphBlocks는 일반적인 distributed sink에 대해 exactly-once를 무조건 주장하지 않는다. Idempotent sink와 transactional source/sink 조합으로 effectively-once 결과를 제공할 수 있다.

## B.5 Checkpoint barrier

```text
source cursors
operator state
pending effect journal
sink commit metadata
plan hash
schema versions
```

Checkpoint commit 순서와 source offset commit 순서를 connector profile별로 명시한다.

## B.6 Event time

```text
event time
processing time
watermark
allowed lateness
trigger
accumulation mode
```

`window(size_ms)`만으로 unbounded aggregation 완료를 결정하지 않는다.

## B.7 Operators

```text
stream.map
stream.filter
stream.flat_map
stream.key_by
stream.window
stream.aggregate
stream.join
stream.batch
stream.sink
```

Core의 `control.reduce`와 extension의 unbounded aggregate를 구분한다.

## B.8 Recovery

```text
restore checkpoint
→ recreate operators
→ restore state
→ seek source cursor
→ reconcile effect journal
→ resume demand
```

Block upgrade 시 state migration schema가 필요하다.

## B.9 Backpressure

Bounded channel, demand, pause capability를 사용한다. Source가 pause를 지원하지 않으면 broker prefetch/partition assignment와 local spill 정책을 선언한다.

## B.10 Durable TCK

```text
source cursor replay
checkpoint atomicity
worker crash recovery
idempotent sink replay
late event/window semantics
state migration
partition ordering
rebalance
poison item/dead-letter
```

